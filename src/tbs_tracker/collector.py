"""Склад собранных офферов: парсер пишет, трекер читает.

Зачем разделять сбор и поиск. Парсинг — это секунды на страницу, капчи, прокси и
регулярные поломки при редизайне. Прогон трекера — это сотни плеч, которые нужно
опросить и сложить в цепочки за один раз. Если парсить внутри прогона, каждый
поиск маршрута превращается в получасовой скрейпинг, который к тому же падает
целиком из-за одного заблокированного домена.

Поэтому оффер живёт в отдельной таблице. Парсер наполняет её по своему
расписанию (и своими темпами, с ретраями и прокси), а трекер на прогоне только
читает готовое — быстро, без сети и без риска. Побочный выигрыш: склад копит
историю цен по каждому рейсу, а не только по итоговым цепочкам.

Дедупликация идёт по «личности рейса» (плечо + перевозчик + номер + вылет +
источник) без цены: повторный сбор обновляет цену, а не плодит строки. Иначе
подорожавший рейс продолжал бы выигрывать по старой строке.

Возраст записи — не метаданные, а часть цены. Собранная минуту назад цена почти
наверняка покупаема (`live`), та же цена десятичасовой давности — всего лишь
наблюдение (`cached`), а совсем старая не отдаётся вовсе.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config, ProviderConfig
from .models import Leg, LinkKind, Mode, PaymentChannel, PriceKind
from .timeutil import parse_dt

DEFAULT_DB_PATH = "data/collector.sqlite3"
DEFAULT_FRESH_MINUTES = 120
DEFAULT_MAX_AGE_MINUTES = 1440

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    offer_key TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    mode TEXT NOT NULL,
    depart_date TEXT,
    depart TEXT,
    arrive TEXT,
    duration_min INTEGER,
    carrier TEXT,
    flight_number TEXT,
    price_rub REAL NOT NULL,
    price_original REAL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    price_kind TEXT NOT NULL DEFAULT 'live',
    payment_channel TEXT NOT NULL DEFAULT 'ru_card',
    transfers INTEGER NOT NULL DEFAULT 0,
    baggage_included INTEGER NOT NULL DEFAULT 0,
    flexible INTEGER NOT NULL DEFAULT 0,
    nights_included INTEGER NOT NULL DEFAULT 0,
    return_flight_included INTEGER NOT NULL DEFAULT 0,
    deep_link TEXT,
    link_kind TEXT NOT NULL DEFAULT 'search',
    booking_ref TEXT,
    source TEXT NOT NULL,
    notes TEXT,
    collected_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_offers_edge ON offers(origin, destination, depart_date);
CREATE INDEX IF NOT EXISTS idx_offers_age ON offers(collected_at);
"""


@dataclass
class CollectorStats:
    total: int
    fresh: int
    oldest_at: str | None
    newest_at: str | None
    by_source: dict[str, int]


@dataclass(frozen=True)
class CollectorPolicy:
    """До какого возраста цене верить.

    `fresh_minutes` — пока цена считается живой (её можно идти покупать),
    `max_age_minutes` — после этого запись не отдаётся и вычищается.
    """

    fresh_minutes: int = DEFAULT_FRESH_MINUTES
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES

    @classmethod
    def from_options(cls, options: ProviderConfig | None) -> "CollectorPolicy":
        if options is None:
            return cls()
        return cls(
            fresh_minutes=int(options.option("fresh_minutes", DEFAULT_FRESH_MINUTES)),
            max_age_minutes=int(options.option("max_age_minutes", DEFAULT_MAX_AGE_MINUTES)),
        )

    @classmethod
    def from_config(cls, config: Config) -> "CollectorPolicy":
        return cls.from_options(config.providers.get("collector"))


def collector_db_path(config: Config, options: ProviderConfig | None = None) -> Path:
    """Где лежит склад: из настроек провайдера, иначе по умолчанию."""
    provider = options or config.providers.get("collector")
    raw = provider.option("db_path") if provider else None
    return config.resolve_path(raw or DEFAULT_DB_PATH)


def offer_key(leg: Leg) -> str:
    """Личность рейса без цены — чтобы повторный сбор обновлял, а не дублировал."""
    return "|".join(
        [
            leg.origin,
            leg.destination,
            leg.mode.value,
            (leg.carrier or "").upper(),
            leg.flight_number or "",
            leg.depart.isoformat() if leg.depart else (leg.depart_date.isoformat() if leg.depart_date else "flex"),
            leg.source,
        ]
    )


class CollectorStore:
    """Тонкая обёртка над таблицей офферов. Одинаково доступна парсеру и трекеру."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CollectorStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- запись
    def put(self, legs: Iterable[Leg], *, collected_at: datetime | None = None) -> int:
        """Записать офферы, вернув число уникальных.

        Одно и то же плечо приходит от источника в нескольких окнах дат, поэтому
        строки сначала сворачиваются по ключу оффера: на складе хранится одна
        запись на рейс, с последней известной ценой.
        """
        stamp = (collected_at or datetime.now(timezone.utc)).isoformat()
        by_key: dict[str, tuple[Any, ...]] = {}
        for leg in legs:
            values = _row_values(leg)
            by_key[str(values[0])] = (*values, stamp)
        rows = list(by_key.values())
        if not rows:
            return 0
        with closing(self.conn.cursor()) as cur:
            cur.executemany(
                """INSERT OR REPLACE INTO offers (
                    offer_key, origin, destination, mode, depart_date, depart, arrive,
                    duration_min, carrier, flight_number, price_rub, price_original,
                    currency, price_kind, payment_channel, transfers, baggage_included,
                    flexible, nights_included, return_flight_included, deep_link,
                    link_kind, booking_ref, source, notes, collected_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        self.conn.commit()
        return len(rows)

    def prune(self, max_age_minutes: int) -> int:
        """Выбросить записи, которым уже нельзя верить даже как наблюдению."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        with closing(self.conn.cursor()) as cur:
            cur.execute("DELETE FROM offers WHERE collected_at < ?", (cutoff,))
            removed = cur.rowcount
        self.conn.commit()
        return max(0, removed)

    # -------------------------------------------------------------- чтение
    def select(
        self,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        *,
        modes: Sequence[Mode] | None = None,
        max_age_minutes: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = [
            "SELECT * FROM offers WHERE origin = ? AND destination = ?",
            "AND (depart_date IS NULL OR depart_date BETWEEN ? AND ?)",
        ]
        params: list[Any] = [origin, destination, date_from.isoformat(), date_to.isoformat()]
        if modes:
            sql.append("AND mode IN (%s)" % ",".join("?" for _ in modes))
            params.extend(m.value for m in modes)
        if max_age_minutes is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
            sql.append("AND collected_at >= ?")
            params.append(cutoff.isoformat())
        sql.append("ORDER BY price_rub ASC")
        with closing(self.conn.cursor()) as cur:
            cur.execute(" ".join(sql), params)
            return [dict(row) for row in cur.fetchall()]

    def stats(self, *, fresh_minutes: int = DEFAULT_FRESH_MINUTES) -> CollectorStats:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=fresh_minutes)).isoformat()
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "SELECT COUNT(*) AS total, MIN(collected_at) AS oldest, MAX(collected_at) AS newest,"
                " SUM(CASE WHEN collected_at >= ? THEN 1 ELSE 0 END) AS fresh FROM offers",
                (cutoff,),
            )
            row = cur.fetchone()
            cur.execute(
                "SELECT source, COUNT(*) AS n FROM offers GROUP BY source ORDER BY n DESC"
            )
            by_source = {r["source"]: r["n"] for r in cur.fetchall()}
        return CollectorStats(
            total=row["total"] or 0,
            fresh=row["fresh"] or 0,
            oldest_at=row["oldest"],
            newest_at=row["newest"],
            by_source=by_source,
        )


def _row_values(leg: Leg) -> tuple[Any, ...]:
    return (
        offer_key(leg),
        leg.origin,
        leg.destination,
        leg.mode.value,
        leg.depart_date.isoformat() if leg.depart_date else None,
        leg.depart.isoformat() if leg.depart else None,
        leg.arrive.isoformat() if leg.arrive else None,
        leg.duration_min,
        leg.carrier,
        leg.flight_number,
        float(leg.price_rub),
        float(leg.price_original) if leg.price_original is not None else None,
        leg.currency,
        leg.price_kind.value,
        leg.payment_channel.value,
        int(leg.transfers or 0),
        int(bool(leg.baggage_included)),
        int(bool(leg.flexible)),
        int(leg.nights_included or 0),
        int(bool(leg.return_flight_included)),
        leg.deep_link,
        leg.link_kind.value,
        leg.booking_ref,
        leg.source,
        leg.notes,
    )


def offer_to_leg(row: dict[str, Any], *, fresh_minutes: int = DEFAULT_FRESH_MINUTES) -> Leg:
    """Собрать плечо из записи склада, понизив тип цены по возрасту записи.

    Свежая запись сохраняет тот тип, с которым её собрали: живая цена остаётся
    живой, оценка наземного плеча — оценкой. Пролежавшая дольше `fresh_minutes`
    перестаёт быть предложением к покупке и становится наблюдением (`cached`),
    потому что за это время рейс мог подорожать или исчезнуть.
    """
    collected = parse_dt(row.get("collected_at"))
    stored_kind = PriceKind(str(row.get("price_kind") or "live"))
    age_min = None
    if collected is not None:
        if collected.tzinfo is None:
            collected = collected.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - collected).total_seconds() / 60.0

    kind = stored_kind
    if age_min is None or age_min > fresh_minutes:
        kind = _downgrade(stored_kind)

    origin = str(row["origin"])
    destination = str(row["destination"])
    notes = row.get("notes")
    if age_min is not None:
        age_note = f"собрано {_fmt_age(age_min)} назад"
        notes = f"{notes}; {age_note}" if notes else age_note

    return Leg(
        origin=origin,
        destination=destination,
        mode=Mode(str(row.get("mode") or "air")),
        price_rub=float(row["price_rub"]),
        price_original=float(row["price_original"]) if row.get("price_original") is not None else None,
        currency=str(row.get("currency") or "RUB").upper(),
        source=str(row.get("source") or "collector"),
        depart=parse_dt(row.get("depart"), origin),
        arrive=parse_dt(row.get("arrive"), destination),
        duration_min=int(row["duration_min"]) if row.get("duration_min") else None,
        carrier=row.get("carrier"),
        flight_number=row.get("flight_number"),
        transfers=int(row.get("transfers") or 0),
        price_kind=kind,
        payment_channel=PaymentChannel(str(row.get("payment_channel") or "ru_card")),
        baggage_included=bool(row.get("baggage_included")),
        flexible=bool(row.get("flexible")),
        nights_included=int(row.get("nights_included") or 0),
        return_flight_included=bool(row.get("return_flight_included")),
        deep_link=row.get("deep_link"),
        link_kind=LinkKind(str(row.get("link_kind") or "search")),
        booking_ref=row.get("booking_ref"),
        observed_at=collected,
        notes=notes,
        raw={},
    )


def _downgrade(kind: PriceKind) -> PriceKind:
    return PriceKind.CACHED if kind == PriceKind.LIVE else kind


def _fmt_age(minutes: float) -> str:
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{int(minutes)} мин"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.0f} ч"
    return f"{hours / 24:.0f} сут"

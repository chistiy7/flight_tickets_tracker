"""SQLite-хранилище наблюдений.

Разовый поиск бесполезен: цена Россия → Тбилиси гуляет в 2-3 раза. Смысл трекера
именно в истории — по ней считаются baseline'ы (минимум, p10, медиана), и уже
относительно них решается, дешёвая ли текущая цена.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Itinerary, Leg

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    providers TEXT,
    requests_used INTEGER DEFAULT 0,
    itineraries INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS itinerary_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    observed_at TEXT NOT NULL,
    itinerary_id TEXT NOT NULL,
    route_key TEXT NOT NULL,
    chain_class TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_at TEXT,
    arrive_at TEXT,
    duration_min INTEGER,
    tickets_rub REAL NOT NULL,
    out_of_pocket_rub REAL NOT NULL,
    generalized_rub REAL NOT NULL,
    price_kind TEXT NOT NULL,
    sources TEXT,
    flags TEXT,
    legs_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_itin_route ON itinerary_observations(route_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_itin_id ON itinerary_observations(itinerary_id);

CREATE TABLE IF NOT EXISTS leg_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    observed_at TEXT NOT NULL,
    route_key TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    mode TEXT NOT NULL,
    carrier TEXT,
    flight_number TEXT,
    depart_at TEXT,
    price_rub REAL NOT NULL,
    currency TEXT,
    price_kind TEXT NOT NULL,
    source TEXT NOT NULL,
    deep_link TEXT
);

CREATE INDEX IF NOT EXISTS idx_leg_route ON leg_observations(route_key, observed_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    route_key TEXT NOT NULL,
    itinerary_id TEXT NOT NULL,
    price_rub REAL NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_route ON alerts(route_key, kind, created_at);
"""


@dataclass
class RouteBaseline:
    route_key: str
    observations: int
    min_rub: float | None
    percentile_rub: float | None
    median_rub: float | None
    last_rub: float | None


class Store:
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

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------- запись
    def start_run(self, providers: Iterable[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, providers) VALUES (?, ?)",
            (_now_iso(), ",".join(sorted(providers))),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, requests_used: int, itineraries: int,
                   notes: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, requests_used = ?, itineraries = ?, notes = ?"
            " WHERE id = ?",
            (_now_iso(), requests_used, itineraries, notes, run_id),
        )
        self.conn.commit()

    def record_legs(self, run_id: int, legs: Iterable[Leg]) -> int:
        rows = [
            (
                run_id,
                _iso(leg.observed_at) or _now_iso(),
                leg.route_key,
                leg.origin,
                leg.destination,
                leg.mode.value,
                leg.carrier,
                leg.flight_number,
                _iso(leg.depart),
                leg.price_rub,
                leg.currency,
                leg.price_kind.value,
                leg.source,
                leg.deep_link,
            )
            for leg in legs
        ]
        self.conn.executemany(
            "INSERT INTO leg_observations (run_id, observed_at, route_key, origin, destination,"
            " mode, carrier, flight_number, depart_at, price_rub, currency, price_kind, source,"
            " deep_link) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def record_itineraries(self, run_id: int, itineraries: Iterable[Itinerary]) -> int:
        rows = []
        for itinerary in itineraries:
            rows.append((
                run_id,
                _now_iso(),
                itinerary.itinerary_id,
                itinerary.route_key,
                itinerary.chain_class,
                itinerary.origin,
                itinerary.destination,
                _iso(itinerary.depart),
                _iso(itinerary.arrive),
                itinerary.total_duration_min,
                itinerary.tickets_rub,
                itinerary.cost.out_of_pocket_rub,
                itinerary.cost.generalized_rub,
                itinerary.price_kind.value,
                ",".join(itinerary.sources),
                ",".join(itinerary.flags),
                json.dumps(
                    [_leg_to_dict(leg) for leg in itinerary.legs], ensure_ascii=False
                ),
            ))
        self.conn.executemany(
            "INSERT INTO itinerary_observations (run_id, observed_at, itinerary_id, route_key,"
            " chain_class, origin, destination, depart_at, arrive_at, duration_min, tickets_rub,"
            " out_of_pocket_rub, generalized_rub, price_kind, sources, flags, legs_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def record_alert(self, *, kind: str, route_key: str, itinerary_id: str,
                     price_rub: float, message: str) -> None:
        self.conn.execute(
            "INSERT INTO alerts (created_at, kind, route_key, itinerary_id, price_rub, message)"
            " VALUES (?,?,?,?,?,?)",
            (_now_iso(), kind, route_key, itinerary_id, price_rub, message),
        )
        self.conn.commit()

    # ------------------------------------------------------------- чтение
    def baseline(self, route_key: str, *, days: int = 60, percentile: float = 10.0,
                 exclude_run_id: int | None = None) -> RouteBaseline:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        params: list[Any] = [route_key, since]
        sql = (
            "SELECT out_of_pocket_rub, observed_at FROM itinerary_observations"
            " WHERE route_key = ? AND observed_at >= ?"
        )
        if exclude_run_id is not None:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY observed_at"
        rows = self.conn.execute(sql, params).fetchall()
        prices = sorted(float(row["out_of_pocket_rub"]) for row in rows)
        if not prices:
            return RouteBaseline(route_key, 0, None, None, None, None)
        return RouteBaseline(
            route_key=route_key,
            observations=len(prices),
            min_rub=prices[0],
            percentile_rub=_percentile(prices, percentile),
            median_rub=_percentile(prices, 50.0),
            last_rub=float(rows[-1]["out_of_pocket_rub"]),
        )

    def last_alert_at(self, route_key: str, kind: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT created_at FROM alerts WHERE route_key = ? AND kind = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (route_key, kind),
        ).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["created_at"])

    def history(self, route_key: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if route_key:
            return self.conn.execute(
                "SELECT * FROM itinerary_observations WHERE route_key = ?"
                " ORDER BY observed_at DESC LIMIT ?",
                (route_key, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM itinerary_observations ORDER BY observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def cheapest_ever(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT route_key, chain_class, MIN(out_of_pocket_rub) AS best_rub,"
            " MIN(observed_at) AS first_seen, MAX(observed_at) AS last_seen,"
            " COUNT(*) AS observations FROM itinerary_observations"
            " GROUP BY route_key, chain_class ORDER BY best_rub LIMIT ?",
            (limit,),
        ).fetchall()

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        raise ValueError("пустая выборка")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (percentile / 100.0) * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _leg_to_dict(leg: Leg) -> dict[str, Any]:
    return {
        "origin": leg.origin,
        "destination": leg.destination,
        "mode": leg.mode.value,
        "carrier": leg.carrier,
        "flight_number": leg.flight_number,
        "depart": _iso(leg.depart),
        "arrive": _iso(leg.arrive),
        "price_rub": leg.price_rub,
        "price_original": leg.price_original,
        "currency": leg.currency,
        "price_kind": leg.price_kind.value,
        "source": leg.source,
        "deep_link": leg.deep_link,
        "notes": leg.notes,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

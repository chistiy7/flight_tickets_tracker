"""Опрос собственного парсера: цены берутся со склада, а не из сети.

Провайдер ничего не парсит сам. Он читает то, что парсер уже собрал: либо
напрямую из файла-склада (`db_path`), либо через HTTP-эндпоинт парсера (`url`),
если тот живёт отдельным сервисом или на другой машине.

Это единственный источник, который не тратит бюджет внешних запросов: склад
свой, дёргать его можно сколько нужно. Зато он единственный, кто обязан следить
за возрастом цены — см. `collector.offer_to_leg`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..collector import CollectorPolicy, CollectorStore, collector_db_path, offer_to_leg
from ..models import Leg, LegQuery, Mode, ProviderResult
from .base import Provider, register

log = logging.getLogger(__name__)


@register
class CollectorProvider(Provider):
    name = "collector"
    modes = (Mode.AIR, Mode.BUS, Mode.TRAIN, Mode.TAXI, Mode.TOUR)

    def unavailable_reason(self) -> str | None:
        if self._url():
            return None
        path = self._db_path()
        if not path.exists():
            return f"склад парсера пуст: нет {path} (соберите данные: tbs-tracker collect)"
        return None

    def _url(self) -> str | None:
        raw = self.options.option("url")
        return str(raw) if raw else None

    def _db_path(self) -> Path:
        return collector_db_path(self.config, self.options)

    def _policy(self) -> CollectorPolicy:
        return CollectorPolicy.from_options(self.options)

    def fetch(self, query: LegQuery) -> ProviderResult:
        if self._url():
            return self._fetch_http(query)
        return self._fetch_sqlite(query)

    # ------------------------------------------------------------- sqlite
    def _fetch_sqlite(self, query: LegQuery) -> ProviderResult:
        policy = self._policy()
        with CollectorStore(self._db_path()) as store:
            rows = store.select(
                query.origin,
                query.destination,
                query.date_from,
                query.date_to,
                modes=query.modes,
                max_age_minutes=policy.max_age_minutes,
            )
        legs = [offer_to_leg(row, fresh_minutes=policy.fresh_minutes) for row in rows]
        return self._result(query, legs, requests_used=0)

    # --------------------------------------------------------------- http
    def _fetch_http(self, query: LegQuery) -> ProviderResult:
        url = self._url()
        assert url is not None
        policy = self._policy()
        headers = {}
        token = self.options.secret("token_env")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = self.http.get_json(
            url,
            params={
                "origin": query.origin,
                "destination": query.destination,
                "date_from": query.date_from.isoformat(),
                "date_to": query.date_to.isoformat(),
                "modes": ",".join(m.value for m in query.modes),
                "passengers": query.passengers,
                "max_age_minutes": policy.max_age_minutes,
            },
            headers=headers or None,
            source=self.name,
            use_cache=False,
            # Склад свой: его опрос не должен съедать квоту внешних API.
            charge_budget=False,
        )
        legs: list[Leg] = []
        for item in _offers(payload):
            row = self._normalize(item, query)
            if row is None:
                continue
            legs.append(offer_to_leg(row, fresh_minutes=policy.fresh_minutes))
        return self._result(query, legs, requests_used=0)

    def _normalize(self, item: dict[str, Any], query: LegQuery) -> dict[str, Any] | None:
        """Привести оффер парсера к строке склада.

        Парсеру разрешено отдавать цену в валюте продажи (`price` + `currency`) —
        пересчёт в рубли не его забота.
        """
        row = dict(item)
        row.setdefault("origin", query.origin)
        row.setdefault("destination", query.destination)
        row.setdefault("source", f"{self.name}:{item.get('parser', 'unknown')}")
        if "collected_at" not in row and "observed_at" in row:
            row["collected_at"] = row.pop("observed_at")
        currency = str(row.get("currency") or "RUB").upper()
        if row.get("price_rub") is None:
            price = row.get("price")
            if price is None:
                return None
            row["price_original"] = float(price)
            row["price_rub"] = self.fx.to_rub(float(price), currency)
        if row.get("depart_date") is None and row.get("depart"):
            row["depart_date"] = str(row["depart"])[:10]
        try:
            mode = Mode(str(row.get("mode") or "air"))
        except ValueError:
            return None
        if mode not in query.modes:
            return None
        return row


def _offers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("offers", "legs", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []

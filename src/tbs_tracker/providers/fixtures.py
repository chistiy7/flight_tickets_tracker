"""Офлайн-провайдер на файле с зафиксированными офферами.

Нужен для трёх вещей: демо без токенов, детерминированные тесты движка маршрутов
и отладка правил стыковок на понятных данных.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Leg, LegQuery, Mode, PaymentChannel, PriceKind, ProviderResult
from ..timeutil import parse_dt
from .base import Provider, now_utc, register


@register
class FixturesProvider(Provider):
    name = "fixtures"
    modes = (Mode.AIR, Mode.BUS, Mode.TRAIN, Mode.TAXI, Mode.TOUR)

    def unavailable_reason(self) -> str | None:
        path = self._path()
        if path is None:
            return "не задан providers.fixtures.path"
        if not path.exists():
            return f"файл фикстур не найден: {path}"
        return None

    def _path(self) -> Path | None:
        raw = self.options.option("path")
        return self.config.resolve_path(raw) if raw else None

    def _load(self) -> dict[str, Any]:
        path = self._path()
        assert path is not None
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    def fetch(self, query: LegQuery) -> ProviderResult:
        data = self._load()
        window = set(query.dates())
        observed = now_utc()
        legs: list[Leg] = []
        for item in data.get("legs") or []:
            if item.get("origin") != query.origin or item.get("destination") != query.destination:
                continue
            mode = Mode(str(item.get("mode", "air")))
            if mode not in query.modes:
                continue
            depart = parse_dt(item.get("depart"), query.origin)
            if depart is not None and depart.date() not in window:
                continue
            currency = str(item.get("currency", "RUB")).upper()
            price = float(item["price"])
            nights = int(item.get("nights") or 0)
            return_included = bool(
                item.get("return_flight_included", mode == Mode.TOUR)
            )
            legs.append(
                Leg(
                    origin=query.origin,
                    destination=query.destination,
                    mode=mode,
                    price_rub=self.fx.to_rub(price, currency),
                    price_original=price,
                    currency=currency,
                    source=f"{self.name}:{item.get('source', 'demo')}",
                    depart=depart,
                    arrive=parse_dt(item.get("arrive"), query.destination),
                    duration_min=item.get("duration_min"),
                    carrier=item.get("carrier"),
                    flight_number=item.get("flight_number"),
                    transfers=int(item.get("transfers") or 0),
                    price_kind=PriceKind(str(item.get("price_kind", "cached"))),
                    payment_channel=PaymentChannel(str(item.get("payment_channel", "ru_card"))),
                    baggage_included=bool(item.get("baggage_included", False)),
                    flexible=bool(item.get("flexible", False)),
                    nights_included=nights,
                    return_flight_included=return_included,
                    deep_link=item.get("deep_link"),
                    observed_at=observed,
                    notes=item.get("notes"),
                    raw={},
                )
            )
        return self._result(query, legs, requests_used=0)

"""Наземные плечи: маршрутки, автобусы, поезда, трансферы.

Без этого слоя цепочки не строятся: самый дешёвый способ попасть в Тбилиси из РФ —
это обычно перелёт во Владикавказ и маршрутка через КПП Верхний Ларс, либо перелёт
в Ереван и автобус. Живых API с ценами на эти перевозки почти нет, поэтому источник
данных — справочник в конфиге (диапазоны цен и длительностей) с опциональным
уточнением фактического расписания через API Яндекс.Расписаний.

Цены помечаются `PriceKind.ESTIMATE`: это ориентир, а не тариф к покупке.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from ..models import Leg, LegQuery, Mode, PaymentChannel, PriceKind, ProviderResult
from ..timeutil import localize
from .base import Provider, now_utc, register

log = logging.getLogger(__name__)

YANDEX_RASP_URL = "https://api.rasp.yandex.net/v3.0/search/"


@register
class GroundProvider(Provider):
    name = "ground"
    modes = (Mode.BUS, Mode.TRAIN, Mode.TAXI)

    def unavailable_reason(self) -> str | None:
        if not self._legs_config():
            return "не задан справочник providers.ground.legs"
        return None

    def _legs_config(self) -> list[dict[str, Any]]:
        return list(self.options.option("legs") or [])

    def fetch(self, query: LegQuery) -> ProviderResult:
        legs: list[Leg] = []
        requests_used = 0
        for spec in self._legs_config():
            if spec.get("origin") != query.origin or spec.get("destination") != query.destination:
                continue
            mode = Mode(str(spec.get("mode", "bus")))
            if mode not in query.modes:
                continue
            schedule, used = self._schedule(spec, query)
            requests_used += used
            legs.extend(self._build_legs(spec, query, mode, schedule))
        return self._result(query, legs, requests_used)

    def _schedule(
        self, spec: dict[str, Any], query: LegQuery
    ) -> tuple[dict[date, list[time]], int]:
        """Времена отправления: из Яндекс.Расписаний, иначе из конфига."""
        departures = [_parse_time(t) for t in (spec.get("departures") or [])]
        departures = [t for t in departures if t is not None]
        fallback = {day: list(departures) for day in query.dates()}

        apikey = self.options.secret("yandex_apikey_env", "YANDEX_RASP_APIKEY")
        yandex_from, yandex_to = spec.get("yandex_from"), spec.get("yandex_to")
        if not (apikey and yandex_from and yandex_to):
            return fallback, 0

        schedule: dict[date, list[time]] = {}
        used = 0
        for day in query.dates():
            try:
                payload = self.http.get_json(
                    YANDEX_RASP_URL,
                    params={
                        "apikey": apikey,
                        "from": yandex_from,
                        "to": yandex_to,
                        "date": day.isoformat(),
                        "transport_types": _yandex_transport(spec.get("mode", "bus")),
                        "limit": 50,
                    },
                    source=self.name,
                )
                used += 1
            except Exception as exc:  # noqa: BLE001 - расписание опционально
                log.warning("Яндекс.Расписания недоступны для %s→%s: %s",
                            spec.get("origin"), spec.get("destination"), exc)
                return fallback, used
            times: list[time] = []
            for segment in (payload or {}).get("segments") or []:
                parsed = _parse_time(str(segment.get("departure") or "")[11:16])
                if parsed:
                    times.append(parsed)
            schedule[day] = times or list(departures)
        return schedule, used

    def _build_legs(
        self,
        spec: dict[str, Any],
        query: LegQuery,
        mode: Mode,
        schedule: dict[date, list[time]],
    ) -> list[Leg]:
        price = float(spec.get("price_rub") or spec.get("price_min_rub") or 0)
        if price <= 0:
            return []
        duration = int(spec.get("duration_min") or 0) or None
        border_buffer = int(spec.get("border_buffer_min") or 0)
        total_duration = (duration or 0) + border_buffer or None
        note = spec.get("note")
        observed = now_utc()
        flexible = bool(spec.get("flexible", not schedule))
        legs: list[Leg] = []

        for day in query.dates():
            times = schedule.get(day) or []
            if flexible and not times:
                legs.append(
                    Leg(
                        origin=query.origin,
                        destination=query.destination,
                        mode=mode,
                        price_rub=price,
                        source=self.name,
                        depart=localize(datetime.combine(day, time(hour=0)), query.origin),
                        duration_min=total_duration,
                        carrier=spec.get("carrier"),
                        price_kind=PriceKind.ESTIMATE,
                        payment_channel=PaymentChannel(str(spec.get("payment_channel", "cash"))),
                        baggage_included=True,
                        flexible=True,
                        deep_link=spec.get("url"),
                        observed_at=observed,
                        notes=note,
                        raw={"price_range": [spec.get("price_min_rub"), spec.get("price_max_rub")]},
                    )
                )
                continue
            for departure in times:
                legs.append(
                    Leg(
                        origin=query.origin,
                        destination=query.destination,
                        mode=mode,
                        price_rub=price,
                        source=self.name,
                        depart=localize(datetime.combine(day, departure), query.origin),
                        duration_min=total_duration,
                        carrier=spec.get("carrier"),
                        price_kind=PriceKind.ESTIMATE,
                        payment_channel=PaymentChannel(str(spec.get("payment_channel", "cash"))),
                        baggage_included=True,
                        flexible=False,
                        deep_link=spec.get("url"),
                        observed_at=observed,
                        notes=note,
                        raw={"price_range": [spec.get("price_min_rub"), spec.get("price_max_rub")]},
                    )
                )
        return legs


def _parse_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _yandex_transport(mode: str) -> str:
    return {"bus": "bus", "train": "train", "taxi": "bus"}.get(str(mode), "bus")

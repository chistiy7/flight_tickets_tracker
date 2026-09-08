"""Прямые сайты авиакомпаний — источник истины по финальной цене.

Метапоиск нужен, чтобы найти кандидатов; сайт перевозчика — чтобы подтвердить цену.
У Азимута, Red Wings и Georgian Airways рублёвые тарифы регулярно ниже, чем в выдаче
агрегаторов, и никакой партнёрский API их не отдаёт.

Провайдер сознательно сделан конфигурируемым, а не «зашитым»: у каждой авиакомпании
свой внутренний JSON календаря цен, он меняется, и держать это в коде бессмысленно.
В конфиге для каждого перевозчика описывается запрос и путь до цены в ответе.

Важно: у большинства перевозчиков автоматический опрос сайта противоречит ToS и
защищён антибот-системами. Поэтому провайдер по умолчанию выключен, работает с
низкой частотой и не обходит защиту. Легальная альтернатива — партнёрский доступ.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ..models import Leg, LegQuery, Mode, PaymentChannel, PriceKind, ProviderResult
from ..timeutil import parse_dt
from .base import Provider, now_utc, register

log = logging.getLogger(__name__)


@register
class AirlineDirectProvider(Provider):
    name = "airline_direct"
    modes = (Mode.AIR,)

    def unavailable_reason(self) -> str | None:
        if not self._airlines():
            return "не заданы providers.airline_direct.airlines"
        return None

    def _airlines(self) -> list[dict[str, Any]]:
        return list(self.options.option("airlines") or [])

    def fetch(self, query: LegQuery) -> ProviderResult:
        legs: list[Leg] = []
        requests_used = 0
        errors: list[str] = []
        for airline in self._airlines():
            if not self._covers(airline, query):
                continue
            request = airline.get("request") or {}
            url_template = request.get("url")
            if not url_template:
                errors.append(f"{airline.get('carrier')}: не задан request.url")
                continue
            for day in query.dates():
                try:
                    payload = self._call(airline, request, query, day)
                    requests_used += 1
                except Exception as exc:  # noqa: BLE001 - один перевозчик не должен ронять остальных
                    errors.append(f"{airline.get('carrier')} {day}: {exc}")
                    continue
                legs.extend(self._parse(airline, payload, query, day))

        result = self._result(query, legs, requests_used)
        if errors and not legs:
            result.error = "; ".join(errors[:3])
        return result

    def _covers(self, airline: dict[str, Any], query: LegQuery) -> bool:
        routes = airline.get("routes") or []
        for route in routes:
            origin, destination = (route[0], route[1]) if isinstance(route, (list, tuple)) else (
                route.get("origin"), route.get("destination")
            )
            if origin == query.origin and destination == query.destination:
                return True
        return False

    def _call(
        self, airline: dict[str, Any], request: dict[str, Any], query: LegQuery, day: date
    ) -> Any:
        placeholders = {
            "origin": query.origin,
            "destination": query.destination,
            "date": day.isoformat(),
            "passengers": query.passengers,
        }
        url = str(request["url"]).format(**placeholders)
        headers = {str(k): str(v).format(**placeholders) for k, v in (request.get("headers") or {}).items()}
        method = str(request.get("method", "GET")).upper()
        if method == "POST":
            body = _format_body(request.get("body") or {}, placeholders)
            return self.http.post_json(url, json_body=body, headers=headers, source=self.name)
        return self.http.get_json(url, headers=headers, source=self.name)

    def _parse(
        self, airline: dict[str, Any], payload: Any, query: LegQuery, day: date
    ) -> list[Leg]:
        spec = airline.get("response") or {}
        items = extract(payload, spec.get("items_path"))
        if items is None:
            return []
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            return []
        currency = str(airline.get("currency", "RUB")).upper()
        observed = now_utc()
        legs: list[Leg] = []
        for item in items:
            price = _to_float(extract(item, spec.get("price_path")))
            if price is None or price <= 0:
                continue
            item_date = extract(item, spec.get("date_path")) if spec.get("date_path") else None
            if item_date and str(item_date)[:10] != day.isoformat():
                continue
            depart = parse_dt(extract(item, spec.get("depart_path")), query.origin) if spec.get("depart_path") else None
            if depart is None:
                depart = parse_dt(f"{day.isoformat()}T{airline.get('default_depart_time', '12:00')}", query.origin)
            arrive = parse_dt(extract(item, spec.get("arrive_path")), query.destination) if spec.get("arrive_path") else None
            legs.append(
                Leg(
                    origin=query.origin,
                    destination=query.destination,
                    mode=Mode.AIR,
                    price_rub=self.fx.to_rub(price, currency),
                    price_original=price,
                    currency=currency,
                    source=f"{self.name}:{airline.get('carrier') or airline.get('name')}",
                    depart=depart,
                    arrive=arrive,
                    duration_min=_to_int(extract(item, spec.get("duration_path"))) if spec.get("duration_path") else None,
                    carrier=airline.get("carrier"),
                    flight_number=_str_or_none(extract(item, spec.get("flight_number_path"))) if spec.get("flight_number_path") else None,
                    transfers=0,
                    price_kind=PriceKind.LIVE,
                    payment_channel=PaymentChannel(str(airline.get("payment_channel", "ru_card"))),
                    baggage_included=bool(airline.get("baggage_included", False)),
                    deep_link=airline.get("booking_url"),
                    observed_at=observed,
                    notes=f"прямая продажа {airline.get('name') or airline.get('carrier')}",
                    raw={},
                )
            )
        return legs


def extract(obj: Any, path: str | None) -> Any:
    """Достать значение по точечному пути: `data.days`, `flights.0.price`.

    Возвращает None, если путь не сходится — источники нестабильны, и падать на
    изменившейся схеме нельзя.
    """
    if path in (None, ""):
        return obj
    current = obj
    for part in str(path).split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if part.isdigit():
                index = int(part)
                current = current[index] if -len(current) <= index < len(current) else None
            else:
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _format_body(body: Any, placeholders: dict[str, Any]) -> Any:
    if isinstance(body, dict):
        return {k: _format_body(v, placeholders) for k, v in body.items()}
    if isinstance(body, list):
        return [_format_body(v, placeholders) for v in body]
    if isinstance(body, str):
        return body.format(**placeholders)
    return body


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None

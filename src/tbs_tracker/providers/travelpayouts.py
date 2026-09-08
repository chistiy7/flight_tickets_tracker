"""Travelpayouts / Aviasales Flight Data API.

Единственный источник с широким покрытием российского рынка, доступный по
самообслуживанию (бесплатный токен). Отдаёт **кэш** чужих поисков (2-7 дней),
поэтому все плечи помечаются как `PriceKind.CACHED` и требуют подтверждения
живым источником перед покупкой.

Документация: https://support.travelpayouts.com/hc/en-us/articles/203956163
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..models import Leg, LegQuery, Mode, PaymentChannel, PriceKind, ProviderResult
from ..timeutil import parse_dt
from .base import Provider, now_utc, register

API_ROOT = "https://api.travelpayouts.com"
AVIASALES_SEARCH_URL = "https://www.aviasales.ru/search"


@register
class TravelpayoutsProvider(Provider):
    name = "travelpayouts"
    modes = (Mode.AIR,)

    def unavailable_reason(self) -> str | None:
        if not self._token():
            env = self.options.option("token_env", "TRAVELPAYOUTS_TOKEN")
            return f"нет токена: задайте переменную окружения {env}"
        return None

    def _token(self) -> str | None:
        return self.options.secret("token_env", "TRAVELPAYOUTS_TOKEN")

    def fetch(self, query: LegQuery) -> ProviderResult:
        token = self._token()
        currency = str(self.options.option("currency", "rub")).lower()
        market = str(self.options.option("market", "ru"))
        limit = int(self.options.option("limit", 100))
        legs: list[Leg] = []
        requests_used = 0

        for departure_at in self._departure_params(query):
            payload = self.http.get_json(
                f"{API_ROOT}/aviasales/v3/prices_for_dates",
                params={
                    "origin": query.origin,
                    "destination": query.destination,
                    "departure_at": departure_at,
                    "currency": currency,
                    "one_way": "true",
                    "direct": "false",
                    "sorting": "price",
                    "market": market,
                    "limit": limit,
                    "page": 1,
                    "token": token,
                },
                headers={"X-Access-Token": token or ""},
                source=self.name,
            )
            requests_used += 1
            legs.extend(self._parse(payload, query, currency))

        return self._result(query, legs, requests_used)

    def _departure_params(self, query: LegQuery) -> list[str]:
        """API умеет и `YYYY-MM`, и `YYYY-MM-DD`.

        Для окна длиннее `day_by_day_threshold` дней запрашиваем месяцами — это
        экономит квоту, а точные даты потом уточняются другими провайдерами.
        """
        threshold = int(self.options.option("day_by_day_threshold", 10))
        dates = query.dates()
        if len(dates) <= threshold:
            return [d.isoformat() for d in dates]
        months: list[str] = []
        for d in dates:
            month = f"{d.year:04d}-{d.month:02d}"
            if month not in months:
                months.append(month)
        return months

    def _parse(self, payload: Any, query: LegQuery, currency: str) -> list[Leg]:
        if not isinstance(payload, dict) or not payload.get("success", True):
            return []
        observed = now_utc()
        window = set(query.dates())
        legs: list[Leg] = []
        for item in payload.get("data") or []:
            depart = parse_dt(item.get("departure_at"), query.origin)
            if depart is None:
                continue
            if depart.date() not in window:
                continue
            price = item.get("price")
            if price is None:
                continue
            link = item.get("link")
            legs.append(
                Leg(
                    origin=item.get("origin") or query.origin,
                    destination=item.get("destination") or query.destination,
                    mode=Mode.AIR,
                    price_rub=self.fx.to_rub(float(price), currency),
                    price_original=float(price),
                    currency=currency.upper(),
                    source=self.name,
                    depart=depart,
                    duration_min=_int_or_none(item.get("duration")),
                    carrier=item.get("airline"),
                    flight_number=_flight_number(item),
                    transfers=int(item.get("transfers") or 0),
                    price_kind=PriceKind.CACHED,
                    payment_channel=PaymentChannel.RU_CARD,
                    baggage_included=False,
                    deep_link=f"{AVIASALES_SEARCH_URL}{link}" if link else None,
                    observed_at=observed,
                    notes="кэш поисков Aviasales, требует подтверждения",
                    raw=item,
                )
            )
        return legs


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flight_number(item: dict[str, Any]) -> str | None:
    airline = item.get("airline")
    number = item.get("flight_number")
    if airline and number:
        return f"{airline}{number}"
    return str(number) if number else None

"""Google Flights через SerpApi.

Используется как арбитр: если кэш Aviasales и Duffel расходятся, «цена как в Google»
помогает понять, какая из них реальна. Живая цена, но без возможности купить —
только ссылка на выдачу.
"""

from __future__ import annotations

from typing import Any

from ..models import Leg, LegQuery, LinkKind, Mode, PaymentChannel, PriceKind, ProviderResult
from ..timeutil import parse_dt
from .base import Provider, now_utc, register

API_URL = "https://serpapi.com/search"


@register
class SerpApiFlightsProvider(Provider):
    name = "serpapi_google_flights"
    modes = (Mode.AIR,)

    def unavailable_reason(self) -> str | None:
        if not self._key():
            env = self.options.option("token_env", "SERPAPI_KEY")
            return f"нет ключа: задайте переменную окружения {env}"
        return None

    def _key(self) -> str | None:
        return self.options.secret("token_env", "SERPAPI_KEY")

    def fetch(self, query: LegQuery) -> ProviderResult:
        currency = str(self.options.option("currency", "RUB")).upper()
        legs: list[Leg] = []
        requests_used = 0
        for day in query.dates():
            payload = self.http.get_json(
                API_URL,
                params={
                    "engine": "google_flights",
                    "departure_id": query.origin,
                    "arrival_id": query.destination,
                    "outbound_date": day.isoformat(),
                    "type": 2,  # one way
                    "adults": query.passengers,
                    "currency": currency,
                    "hl": str(self.options.option("language", "ru")),
                    "gl": str(self.options.option("country", "ru")),
                    "api_key": self._key(),
                },
                source=self.name,
            )
            requests_used += 1
            legs.extend(self._parse(payload, query, currency))
        return self._result(query, legs, requests_used)

    def _parse(self, payload: Any, query: LegQuery, currency: str) -> list[Leg]:
        observed = now_utc()
        groups = (payload or {}).get("best_flights", []) + (payload or {}).get("other_flights", [])
        search_link = ((payload or {}).get("search_metadata") or {}).get("google_flights_url")
        legs: list[Leg] = []
        for item in groups:
            flights = item.get("flights") or []
            if not flights:
                continue
            price = item.get("price")
            if not price:
                continue
            first, last = flights[0], flights[-1]
            legs.append(
                Leg(
                    origin=query.origin,
                    destination=query.destination,
                    mode=Mode.AIR,
                    price_rub=self.fx.to_rub(float(price), currency),
                    price_original=float(price),
                    currency=currency,
                    source=self.name,
                    depart=parse_dt((first.get("departure_airport") or {}).get("time"), query.origin),
                    arrive=parse_dt((last.get("arrival_airport") or {}).get("time"), query.destination),
                    duration_min=_int_or_none(item.get("total_duration")),
                    carrier=first.get("airline"),
                    flight_number=first.get("flight_number"),
                    transfers=max(0, len(flights) - 1),
                    price_kind=PriceKind.LIVE,
                    payment_channel=PaymentChannel.RU_CARD,
                    baggage_included=False,
                    deep_link=search_link,
                    # Google Flights — витрина: рейс там виден, но покупка уходит
                    # к авиакомпании или в OTA.
                    link_kind=LinkKind.SEARCH,
                    observed_at=observed,
                    notes="Google Flights (SerpApi): цена живая, покупка на стороне",
                    raw={"layovers": item.get("layovers")},
                )
            )
        return legs


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

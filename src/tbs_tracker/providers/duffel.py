"""Duffel — живые офферы (NDC/GDS).

Закрывает то, чего нет в российских источниках: Turkish, AJet, Pegasus, Wizz,
flydubai. Российских перевозчиков (Азимут, Red Wings, Победа) в Duffel нет, поэтому
провайдер полезен для плеч «РФ → хаб → Тбилиси» на нероссийских участках.

Оплата — иностранной картой, поэтому плечи помечаются `PaymentChannel.FOREIGN_CARD`.
"""

from __future__ import annotations

from typing import Any

from ..models import Leg, LegQuery, LinkKind, Mode, PaymentChannel, PriceKind, ProviderResult
from ..timeutil import parse_dt
from .base import Provider, now_utc, register

API_URL = "https://api.duffel.com/air/offer_requests"


@register
class DuffelProvider(Provider):
    name = "duffel"
    modes = (Mode.AIR,)

    def unavailable_reason(self) -> str | None:
        if not self._token():
            env = self.options.option("token_env", "DUFFEL_TOKEN")
            return f"нет токена: задайте переменную окружения {env}"
        return None

    def _token(self) -> str | None:
        return self.options.secret("token_env", "DUFFEL_TOKEN")

    def fetch(self, query: LegQuery) -> ProviderResult:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Duffel-Version": str(self.options.option("api_version", "v2")),
            "Content-Type": "application/json",
        }
        cabin = str(self.options.option("cabin_class", "economy"))
        max_connections = int(self.options.option("max_connections", 1))
        legs: list[Leg] = []
        requests_used = 0

        for day in query.dates():
            body = {
                "data": {
                    "slices": [
                        {
                            "origin": query.origin,
                            "destination": query.destination,
                            "departure_date": day.isoformat(),
                        }
                    ],
                    "passengers": [{"type": "adult"} for _ in range(query.passengers)],
                    "cabin_class": cabin,
                    "max_connections": max_connections,
                }
            }
            payload = self.http.post_json(
                API_URL, json_body=body, headers=headers, source=self.name
            )
            requests_used += 1
            legs.extend(self._parse(payload, query))

        return self._result(query, legs, requests_used)

    def _parse(self, payload: Any, query: LegQuery) -> list[Leg]:
        data = (payload or {}).get("data") or {}
        observed = now_utc()
        legs: list[Leg] = []
        for offer in data.get("offers") or []:
            slices = offer.get("slices") or []
            if not slices:
                continue
            segments = slices[0].get("segments") or []
            if not segments:
                continue
            first, last = segments[0], segments[-1]
            currency = str(offer.get("total_currency") or "EUR").upper()
            amount = float(offer.get("total_amount") or 0)
            if amount <= 0:
                continue
            carrier = (offer.get("owner") or {}).get("iata_code")
            baggage = _has_checked_bag(segments)
            offer_id = offer.get("id")
            legs.append(
                Leg(
                    origin=query.origin,
                    destination=query.destination,
                    mode=Mode.AIR,
                    price_rub=self.fx.to_rub(amount, currency),
                    price_original=amount,
                    currency=currency,
                    source=self.name,
                    depart=parse_dt(first.get("departing_at"), query.origin),
                    arrive=parse_dt(last.get("arriving_at"), query.destination),
                    carrier=carrier,
                    flight_number=_flight_number(first),
                    transfers=max(0, len(segments) - 1),
                    price_kind=PriceKind.LIVE,
                    payment_channel=PaymentChannel.FOREIGN_CARD,
                    baggage_included=baggage,
                    # У Duffel нет публичной страницы оффера: бронь создаётся
                    # запросом к API по его идентификатору.
                    deep_link=None,
                    booking_ref=(
                        f"оффер Duffel {offer_id} — бронируется через API, "
                        "офферы живут ~20 минут"
                        if offer_id
                        else "бронирование через API Duffel"
                    ),
                    observed_at=observed,
                    notes="живой оффер Duffel; оплата иностранной картой",
                    raw={"offer_id": offer_id},
                )
            )
        return legs


def _flight_number(segment: dict[str, Any]) -> str | None:
    carrier = (segment.get("marketing_carrier") or {}).get("iata_code")
    number = segment.get("marketing_carrier_flight_number")
    if carrier and number:
        return f"{carrier}{number}"
    return None


def _has_checked_bag(segments: list[dict[str, Any]]) -> bool:
    for segment in segments:
        for passenger in segment.get("passengers") or []:
            for bag in passenger.get("baggages") or []:
                if bag.get("type") == "checked" and int(bag.get("quantity") or 0) > 0:
                    return True
    return False

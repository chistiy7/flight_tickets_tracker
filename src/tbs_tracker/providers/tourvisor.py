"""Tourvisor — поиск туров и «горячие туры».

Российская специфика: чартерный/блочный пакет в Грузию иногда дешевле сухого билета,
потому что туроператор сливает непроданные места. Пакет попадает в выдачу со своей
фактической ценой и конкурирует с билетами напрямую: тур за 20 000 ₽ выгоднее билета
за 25 000 ₽, даже если отель вам не нужен. Почему сравнение идёт по телу тура — в
`tours.py`.

Доступ: JWT из ЛК турагента в заголовке `Authorization: Bearer ...`, разделы
оплачиваются отдельно, лимит 3000 поисков/сутки.
Документация: https://api.tourvisor.ru/search/docs
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from ..models import Leg, LegQuery, Mode, PaymentChannel, PriceKind, ProviderResult, TourOffer
from ..timeutil import parse_dt
from ..tours import describe_package
from .base import Provider, now_utc, register

API_ROOT = "https://api.tourvisor.ru/search"


@register
class TourvisorProvider(Provider):
    name = "tourvisor"
    modes = (Mode.TOUR,)

    def unavailable_reason(self) -> str | None:
        if not self._token():
            env = self.options.option("token_env", "TOURVISOR_JWT")
            return f"нет JWT: задайте переменную окружения {env}"
        if not self.options.option("departure_ids"):
            return "не задан providers.tourvisor.departure_ids (город вылета → id Tourvisor)"
        if not self.options.option("country_id"):
            return "не задан providers.tourvisor.country_id (id Грузии в справочнике)"
        return None

    def _token(self) -> str | None:
        return self.options.secret("token_env", "TOURVISOR_JWT")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _departure_id(self, city: str) -> int | None:
        mapping = self.options.option("departure_ids") or {}
        value = mapping.get(city)
        return int(value) if value is not None else None

    def fetch(self, query: LegQuery) -> ProviderResult:
        tours = self.fetch_tours(query)
        observed = now_utc()
        legs: list[Leg] = []
        for tour in tours:
            if tour.price_rub <= 0:
                continue
            # Цена плеча — цена пакета целиком: столько денег реально уходит, и
            # именно она конкурирует с ценами билетов.
            legs.append(
                Leg(
                    origin=tour.origin,
                    destination=tour.destination,
                    mode=Mode.TOUR,
                    price_rub=tour.price_rub,
                    price_original=tour.price_rub,
                    currency="RUB",
                    source=self.name,
                    depart=parse_dt(tour.depart_date.isoformat(), tour.origin),
                    duration_min=int(self.options.option("assumed_flight_minutes", 210)),
                    carrier=tour.operator,
                    price_kind=PriceKind.CACHED,
                    payment_channel=PaymentChannel.RU_CARD,
                    baggage_included=True,
                    nights_included=tour.nights,
                    return_flight_included=True,
                    deep_link=tour.deep_link,
                    observed_at=observed,
                    notes=describe_package(
                        price_rub=tour.price_rub,
                        nights=tour.nights,
                        hotel=tour.hotel,
                        price_old_rub=tour.price_old_rub,
                    ),
                    raw=tour.raw,
                )
            )
        return self._result(query, legs, requests_used=1)

    def fetch_tours(self, query: LegQuery) -> list[TourOffer]:
        departure_id = self._departure_id(query.origin)
        if departure_id is None:
            return []
        params: dict[str, Any] = {
            "departure": departure_id,
            "country": int(self.options.option("country_id")),
            "nightsMin": int(self.options.option("nights_min", 3)),
            "nightsMax": int(self.options.option("nights_max", 10)),
            "adults": query.passengers,
            "limit": int(self.options.option("limit", 50)),
        }
        if self.options.option("hot_only", True):
            payload = self.http.get_json(
                f"{API_ROOT}/tours/hots", params=params, headers=self._headers(), source=self.name
            )
            items = payload if isinstance(payload, list) else payload.get("data") or []
            return [self._parse_hot(item, query) for item in items if self._is_in_window(item, query)]
        return self._search_tours(params, query)

    def _search_tours(self, params: dict[str, Any], query: LegQuery) -> list[TourOffer]:
        """Синхронная обёртка над асинхронным поиском Tourvisor."""
        params = dict(params)
        params.update({
            "dateFrom": query.date_from.isoformat(),
            "dateTo": query.date_to.isoformat(),
        })
        started = self.http.post_json(
            f"{API_ROOT}/tours/search", json_body=params, headers=self._headers(),
            source=self.name, use_cache=False,
        )
        search_id = (started or {}).get("searchId") or (started or {}).get("id")
        if not search_id:
            return []
        deadline = time.monotonic() + float(self.options.option("search_timeout_sec", 25))
        while time.monotonic() < deadline:
            status = self.http.get_json(
                f"{API_ROOT}/tours/search/{search_id}/status",
                headers=self._headers(), source=self.name, use_cache=False,
            )
            if str((status or {}).get("state", "")).lower() in ("finished", "done", "complete"):
                break
            time.sleep(float(self.options.option("poll_interval_sec", 2)))
        payload = self.http.get_json(
            f"{API_ROOT}/tours/search/{search_id}", headers=self._headers(), source=self.name
        )
        hotels = payload if isinstance(payload, list) else payload.get("data") or []
        offers: list[TourOffer] = []
        for hotel in hotels:
            for tour in hotel.get("tours") or []:
                offers.append(self._parse_search_tour(hotel, tour, query))
        return offers

    def _is_in_window(self, item: dict[str, Any], query: LegQuery) -> bool:
        tour_date = _as_date(item.get("date"))
        if tour_date is None:
            return True
        return query.date_from <= tour_date <= query.date_to

    def _parse_hot(self, item: dict[str, Any], query: LegQuery) -> TourOffer:
        hotel = item.get("hotel") or {}
        operator = item.get("operator") or {}
        currency = str(item.get("currency") or "RUB").upper()
        return TourOffer(
            origin=query.origin,
            destination=self._resolve_destination(hotel, query),
            price_rub=self.fx.to_rub(float(item.get("price") or 0), currency),
            price_old_rub=(
                self.fx.to_rub(float(item["priceOld"]), currency) if item.get("priceOld") else None
            ),
            nights=int(item.get("nights") or 0),
            depart_date=_as_date(item.get("date")) or query.date_from,
            source=self.name,
            hotel=hotel.get("name"),
            hotel_stars=_int_or_none(hotel.get("category")),
            operator=operator.get("russianName") or operator.get("name"),
            meal=(item.get("meal") or {}).get("russianName"),
            deep_link=hotel.get("hotelDescriptionLink"),
            raw={"tourId": item.get("tourId")},
        )

    def _parse_search_tour(
        self, hotel: dict[str, Any], tour: dict[str, Any], query: LegQuery
    ) -> TourOffer:
        operator = tour.get("operator") or {}
        currency = str(tour.get("currency") or hotel.get("currency") or "RUB").upper()
        return TourOffer(
            origin=query.origin,
            destination=self._resolve_destination(hotel, query),
            price_rub=self.fx.to_rub(float(tour.get("price") or 0), currency),
            nights=int(tour.get("nights") or 0),
            depart_date=_as_date(tour.get("date")) or query.date_from,
            source=self.name,
            hotel=hotel.get("name"),
            hotel_stars=_int_or_none(hotel.get("category")),
            operator=operator.get("russianName") or operator.get("name"),
            meal=(tour.get("meal") or {}).get("russianName"),
            deep_link=hotel.get("hotelDescriptionLink"),
            raw={"tourId": tour.get("id"), "isCharter": tour.get("isCharter")},
        )

    def _resolve_destination(self, hotel: dict[str, Any], query: LegQuery) -> str:
        """Регион отеля → код города входа; по умолчанию цель поиска."""
        mapping = self.options.option("region_to_city") or {}
        region = (hotel.get("region") or {}).get("name")
        if region and region in mapping:
            return str(mapping[region])
        return query.destination


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return date.fromisoformat(text) if fmt == "%Y-%m-%d" else _strptime_date(text, fmt)
        except ValueError:
            continue
    return None


def _strptime_date(text: str, fmt: str) -> date:
    from datetime import datetime

    return datetime.strptime(text, fmt).date()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

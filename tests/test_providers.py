"""Парсинг ответов источников. Сеть не используется — ответы подставляются."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tbs_tracker.config import Config, ProviderConfig
from tbs_tracker.fx import CurrencyConverter
from tbs_tracker.httpclient import HttpClient, RequestBudget, RequestBudgetExceeded
from tbs_tracker.models import LegQuery, Mode, PaymentChannel, PriceKind
from tbs_tracker.providers.airline_direct import AirlineDirectProvider, extract
from tbs_tracker.providers.duffel import DuffelProvider
from tbs_tracker.providers.ground import GroundProvider
from tbs_tracker.providers.serpapi_flights import SerpApiFlightsProvider
from tbs_tracker.providers.tourvisor import TourvisorProvider
from tbs_tracker.providers.travelpayouts import TravelpayoutsProvider


class FakeHttp:
    """Подменяет HttpClient: отдаёт заготовленные ответы и пишет вызовы."""

    def __init__(self, response=None, *, queue=None):
        self.response = response
        self.queue = list(queue) if queue is not None else None
        self.calls = []

    def get_json(self, url, *, params=None, headers=None, source="http", use_cache=True):
        self.calls.append(("GET", url, params, headers))
        return self._next(url)

    def post_json(self, url, *, json_body=None, headers=None, source="http", use_cache=True):
        self.calls.append(("POST", url, json_body, headers))
        return self._next(url)

    def _next(self, url):
        if self.queue:
            return self.queue.pop(0)
        return self.response


@pytest.fixture
def query() -> LegQuery:
    return LegQuery("MOW", "TBS", date(2026, 10, 6), date(2026, 10, 6))


def _provider(cls, base_config, options, http, name=None):
    provider_config = ProviderConfig(name=name or cls.name, enabled=True, options=options)
    fx = CurrencyConverter(base_config.runtime.fx)
    return cls(base_config, provider_config, http, fx)


def test_travelpayouts_parses_cached_prices(base_config, query, monkeypatch):
    monkeypatch.setenv("TP_TOKEN", "secret")
    payload = {
        "success": True,
        "data": [
            {
                "origin": "MOW", "destination": "TBS", "price": 13806,
                "departure_at": "2026-10-06T17:00:00+03:00", "duration": 195,
                "airline": "WZ", "flight_number": 565, "transfers": 0,
                "link": "/MOW0610TBS1?t=abc",
            },
            # Вне окна дат — должно отсеяться.
            {
                "origin": "MOW", "destination": "TBS", "price": 9000,
                "departure_at": "2026-11-06T17:00:00+03:00", "airline": "A4",
            },
        ],
    }
    http = FakeHttp(payload)
    provider = _provider(TravelpayoutsProvider, base_config,
                         {"token_env": "TP_TOKEN", "currency": "rub"}, http)
    result = provider.safe_fetch(query)
    assert result.ok and len(result.legs) == 1
    leg = result.legs[0]
    assert leg.price_rub == 13806
    assert leg.carrier == "WZ" and leg.flight_number == "WZ565"
    assert leg.price_kind == PriceKind.CACHED
    assert leg.deep_link.endswith("/MOW0610TBS1?t=abc")
    assert http.calls[0][2]["origin"] == "MOW"


def test_travelpayouts_switches_to_month_queries_on_wide_window(base_config, monkeypatch):
    monkeypatch.setenv("TP_TOKEN", "secret")
    http = FakeHttp({"success": True, "data": []})
    provider = _provider(TravelpayoutsProvider, base_config, {"token_env": "TP_TOKEN"}, http)
    wide = LegQuery("MOW", "TBS", date(2026, 10, 1), date(2026, 11, 15))
    provider.safe_fetch(wide)
    requested = [call[2]["departure_at"] for call in http.calls]
    assert requested == ["2026-10", "2026-11"]


def test_travelpayouts_skips_without_token(base_config, query, monkeypatch):
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    provider = _provider(TravelpayoutsProvider, base_config, {}, FakeHttp({}))
    result = provider.safe_fetch(query)
    assert result.skipped_reason and "TRAVELPAYOUTS_TOKEN" in result.skipped_reason
    assert result.legs == []


def test_duffel_parses_live_offer_in_eur(base_config, query, monkeypatch):
    monkeypatch.setenv("DUFFEL", "tok")
    payload = {
        "data": {
            "offers": [
                {
                    "id": "off_1", "total_amount": "118.00", "total_currency": "EUR",
                    "owner": {"iata_code": "TK"},
                    "slices": [
                        {
                            "segments": [
                                {
                                    "departing_at": "2026-10-06T15:00:00",
                                    "arriving_at": "2026-10-06T18:20:00",
                                    "marketing_carrier": {"iata_code": "TK"},
                                    "marketing_carrier_flight_number": "378",
                                    "passengers": [
                                        {"baggages": [{"type": "checked", "quantity": 1}]}
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    }
    provider = _provider(DuffelProvider, base_config, {"token_env": "DUFFEL"}, FakeHttp(payload))
    result = provider.safe_fetch(query)
    assert len(result.legs) == 1
    leg = result.legs[0]
    rate = base_config.runtime.fx.static_rates["EUR"]
    assert leg.price_rub == pytest.approx(118.0 * rate)
    assert leg.price_kind == PriceKind.LIVE
    assert leg.payment_channel == PaymentChannel.FOREIGN_CARD
    assert leg.baggage_included is True
    assert leg.flight_number == "TK378"


def test_serpapi_parses_best_and_other_flights(base_config, query, monkeypatch):
    monkeypatch.setenv("SERP", "key")
    payload = {
        "best_flights": [
            {
                "price": 12500, "total_duration": 195,
                "flights": [
                    {
                        "airline": "Azimuth", "flight_number": "A4 7007",
                        "departure_airport": {"time": "2026-10-06 08:40"},
                        "arrival_airport": {"time": "2026-10-06 12:55"},
                    }
                ],
            }
        ],
        "other_flights": [
            {"price": 19000, "flights": [{"airline": "TK",
                                          "departure_airport": {"time": "2026-10-06 15:00"},
                                          "arrival_airport": {"time": "2026-10-07 01:00"}}]}
        ],
        "search_metadata": {"google_flights_url": "https://google.com/travel/flights/x"},
    }
    provider = _provider(SerpApiFlightsProvider, base_config, {"token_env": "SERP"},
                         FakeHttp(payload))
    result = provider.safe_fetch(query)
    assert len(result.legs) == 2
    assert result.legs[0].price_rub == 12500
    assert result.legs[0].duration_min == 195
    assert all(leg.deep_link for leg in result.legs)


def test_tourvisor_hot_tour_becomes_flight_equivalent(base_config, query, monkeypatch):
    monkeypatch.setenv("TV", "jwt")
    payload = [
        {
            "price": 30500, "priceOld": 41000, "nights": 7, "currency": "RUB",
            "date": "2026-10-06", "tourId": "t1",
            "hotel": {"name": "Tbilisi Inn", "category": 3,
                      "region": {"name": "Тбилиси"}},
            "operator": {"russianName": "Anex"},
            "meal": {"russianName": "завтраки"},
        }
    ]
    options = {
        "token_env": "TV", "country_id": 35, "departure_ids": {"MOW": 1},
        "hot_only": True, "region_to_city": {"Тбилиси": "TBS"},
    }
    provider = _provider(TourvisorProvider, base_config, options, FakeHttp(payload))
    tour_query = LegQuery("MOW", "TBS", date(2026, 10, 6), date(2026, 10, 6), modes=(Mode.TOUR,))
    result = provider.safe_fetch(tour_query)
    assert len(result.legs) == 1
    leg = result.legs[0]
    # 30500 - 7 ночей × 3000 ₽ = 9500 ₽ «эквивалента билета».
    assert leg.price_rub == pytest.approx(9500)
    assert leg.mode == Mode.TOUR
    assert "30500" in leg.notes


def test_tourvisor_skips_without_dictionaries(base_config, monkeypatch):
    monkeypatch.setenv("TV", "jwt")
    provider = _provider(TourvisorProvider, base_config, {"token_env": "TV"}, FakeHttp([]))
    result = provider.safe_fetch(
        LegQuery("MOW", "TBS", date(2026, 10, 6), date(2026, 10, 6), modes=(Mode.TOUR,))
    )
    assert result.skipped_reason and "departure_ids" in result.skipped_reason


def test_airline_direct_extracts_by_configured_paths(base_config, query):
    payload = {"data": {"days": [
        {"date": "2026-10-06", "minPrice": 11990, "departure": "2026-10-06T08:40",
         "flightNumber": "A4 7007"},
        {"date": "2026-10-07", "minPrice": 9990},
    ]}}
    options = {
        "airlines": [
            {
                "carrier": "A4", "name": "Азимут", "currency": "RUB",
                "routes": [["MOW", "TBS"]],
                "booking_url": "https://azimuth.aero/",
                "request": {"url": "https://x.invalid/c?from={origin}&to={destination}&d={date}"},
                "response": {
                    "items_path": "data.days", "price_path": "minPrice",
                    "date_path": "date", "depart_path": "departure",
                    "flight_number_path": "flightNumber",
                },
            }
        ]
    }
    provider = _provider(AirlineDirectProvider, base_config, options, FakeHttp(payload))
    result = provider.safe_fetch(query)
    assert len(result.legs) == 1  # второй день отфильтрован по дате
    leg = result.legs[0]
    assert leg.price_rub == 11990
    assert leg.price_kind == PriceKind.LIVE
    assert leg.source == "airline_direct:A4"
    assert leg.deep_link == "https://azimuth.aero/"


def test_airline_direct_survives_changed_schema(base_config, query):
    options = {
        "airlines": [
            {
                "carrier": "WZ", "routes": [["MOW", "TBS"]],
                "request": {"url": "https://x.invalid/f?d={date}"},
                "response": {"items_path": "fares.list", "price_path": "total"},
            }
        ]
    }
    provider = _provider(AirlineDirectProvider, base_config, options,
                         FakeHttp({"unexpected": "shape"}))
    result = provider.safe_fetch(query)
    assert result.legs == []
    assert result.error is None  # смена схемы не должна выглядеть как сбой


def test_extract_handles_lists_and_missing_keys():
    obj = {"a": [{"b": 1}, {"b": 2}]}
    assert extract(obj, "a.1.b") == 2
    assert extract(obj, "a.9.b") is None
    assert extract(obj, "nope.deep") is None
    assert extract(obj, None) is obj


def test_ground_provider_uses_static_schedule(base_config):
    options = {
        "legs": [
            {
                "origin": "EVN", "destination": "TBS", "mode": "bus", "price_rub": 1600,
                "duration_min": 360, "departures": ["08:00", "22:00"],
                "note": "автобус",
            }
        ]
    }
    provider = _provider(GroundProvider, base_config, options, FakeHttp({}))
    result = provider.safe_fetch(
        LegQuery("EVN", "TBS", date(2026, 10, 6), date(2026, 10, 7),
                 modes=(Mode.BUS, Mode.TAXI))
    )
    assert len(result.legs) == 4  # 2 дня × 2 отправления
    assert {leg.depart.strftime("%H:%M") for leg in result.legs} == {"08:00", "22:00"}
    assert all(leg.price_kind == PriceKind.ESTIMATE for leg in result.legs)
    assert result.requests_used == 0


def test_ground_provider_border_leg_adds_buffer_to_duration(base_config):
    options = {
        "legs": [
            {
                "origin": "OGZ", "destination": "TBS", "mode": "bus", "price_rub": 1500,
                "duration_min": 240, "border_buffer_min": 120, "flexible": True,
            }
        ]
    }
    provider = _provider(GroundProvider, base_config, options, FakeHttp({}))
    result = provider.safe_fetch(
        LegQuery("OGZ", "TBS", date(2026, 10, 6), date(2026, 10, 6), modes=(Mode.BUS,))
    )
    assert len(result.legs) == 1
    assert result.legs[0].duration_min == 360
    assert result.legs[0].flexible is True


def test_request_budget_blocks_runaway_polling(tmp_path: Path):
    budget = RequestBudget(limit=2)
    http = HttpClient(cache_dir=None, budget=budget, retries=1)
    http.session = None  # запросы не должны дойти до сети
    budget.charge("x")
    budget.charge("x")
    with pytest.raises(RequestBudgetExceeded):
        budget.charge("x")
    assert budget.remaining == 0


def test_currency_converter_requires_known_rate(base_config):
    fx = CurrencyConverter(base_config.runtime.fx)
    assert fx.to_rub(10, "GEL") == pytest.approx(10 * base_config.runtime.fx.static_rates["GEL"])
    assert fx.to_rub(100, "RUB") == 100
    with pytest.raises(KeyError):
        fx.to_rub(1, "XYZ")


def test_config_rejects_unknown_fields(tmp_path: Path):
    from tbs_tracker.config import ConfigError

    with pytest.raises(ConfigError):
        Config.from_dict({"search": {"origns": ["MOW"]}}, base_dir=tmp_path)

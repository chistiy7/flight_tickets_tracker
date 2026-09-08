"""Сравнение пакетного тура с авиабилетом.

Правило: сравниваем по фактической цене. Тур за 20 000 ₽ выгоднее билета за 25 000 ₽,
и включённое проживание не должно ни маскировать эту разницу, ни выдумывать её там,
где пакет реально дороже.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import TZ_MOW, make_leg
from tbs_tracker.models import Itinerary, Mode
from tbs_tracker.routing.rules import classify, flags_for
from tbs_tracker.routing.search import compute_cost
from tbs_tracker.tours import accommodation_credit_rub, bundle_credit_rub


def _tour_leg(price: float, *, nights: int, config) -> object:
    leg = make_leg(
        "MOW", "TBS", price=price, mode=Mode.TOUR, carrier="Anex", baggage=True,
        depart=datetime(2026, 10, 6, 9, 0, tzinfo=TZ_MOW), duration_min=210,
    )
    leg.nights_included = nights
    leg.return_flight_included = True
    leg.bundle_credit_rub = bundle_credit_rub(
        nights_included=nights, return_included=True, costs=config.costs
    )
    return leg


def _itinerary(legs, config) -> Itinerary:
    itinerary = Itinerary(legs=list(legs), chain_class=classify(list(legs)))
    itinerary.cost = compute_cost(list(legs), config)
    itinerary.flags = flags_for(list(legs), itinerary.layovers_min(), config.connections)
    return itinerary


def test_cheaper_tour_beats_pricier_ticket(base_config):
    base_config.costs.trip_nights = 0
    base_config.costs.need_baggage = False

    tour = _itinerary([_tour_leg(20000, nights=7, config=base_config)], base_config)
    ticket = _itinerary(
        [make_leg("MOW", "TBS", price=25000, baggage=True,
                  depart=datetime(2026, 10, 6, 8, 40, tzinfo=TZ_MOW), duration_min=200)],
        base_config,
    )

    assert tour.cost.out_of_pocket_rub == 20000
    assert ticket.cost.out_of_pocket_rub == 25000
    assert tour.cost.out_of_pocket_rub < ticket.cost.out_of_pocket_rub
    assert tour.cost.generalized_rub < ticket.cost.generalized_rub
    assert tour.chain_class == "E"
    assert {"package_tour", "hotel_included", "return_included"} <= set(tour.flags)


def test_by_default_included_extras_are_ignored(base_config):
    """По умолчанию за цену пакета считаем, что получаем только билет в одну сторону."""
    base_config.costs.trip_nights = 0
    base_config.costs.return_flight_value_rub = 0

    tour = _itinerary([_tour_leg(20000, nights=7, config=base_config)], base_config)

    assert tour.cost.bundle_credit_rub == 0
    assert tour.cost.value_rub == tour.cost.out_of_pocket_rub == 20000
    # При этом видно, что в цену входит больше: выгода не потеряна, а не оцифрована.
    assert {"hotel_included", "return_included"} <= set(tour.flags)
    assert tour.legs[0].nights_included == 7


def test_return_flight_credit_is_opt_in(base_config):
    base_config.costs.trip_nights = 0
    base_config.costs.return_flight_value_rub = 13000

    tour = _itinerary([_tour_leg(20000, nights=7, config=base_config)], base_config)

    assert tour.cost.out_of_pocket_rub == 20000
    assert tour.cost.applied_credit_rub == 13000
    assert tour.cost.value_rub == 7000


def test_bundle_credit_sums_hotel_and_return(base_config):
    base_config.costs.trip_nights = 2
    base_config.costs.tour_accommodation_rub_per_night = 3000
    base_config.costs.return_flight_value_rub = 13000

    credit = bundle_credit_rub(nights_included=7, return_included=True, costs=base_config.costs)
    assert credit == pytest.approx(2 * 3000 + 13000)

    # Плечо без обратного перелёта получает только «отельную» часть.
    credit_one_way = bundle_credit_rub(
        nights_included=7, return_included=False, costs=base_config.costs
    )
    assert credit_one_way == pytest.approx(6000)


def test_accommodation_is_a_bonus_not_a_discount(base_config):
    """Зачёт проживания меняет «полезную» стоимость, но не сумму к оплате."""
    base_config.costs.trip_nights = 4
    base_config.costs.tour_accommodation_rub_per_night = 3000

    tour = _itinerary([_tour_leg(20000, nights=7, config=base_config)], base_config)

    assert tour.cost.out_of_pocket_rub == 20000  # платим ровно цену пакета
    assert tour.cost.applied_credit_rub == 12000  # 4 нужные ночи × 3000 ₽
    assert tour.cost.value_rub == 8000


def test_credit_never_makes_price_negative(base_config):
    base_config.costs.trip_nights = 7
    base_config.costs.tour_accommodation_rub_per_night = 3000

    tour = _itinerary([_tour_leg(12000, nights=7, config=base_config)], base_config)

    assert tour.cost.out_of_pocket_rub == 12000
    assert tour.cost.value_rub == 0
    assert tour.cost.generalized_rub >= 0


def test_expensive_tour_loses_to_ticket(base_config):
    """Обратная проверка: дорогой пакет не должен выигрывать за счёт отеля."""
    base_config.costs.trip_nights = 0
    base_config.costs.need_baggage = False

    tour = _itinerary([_tour_leg(34900, nights=7, config=base_config)], base_config)
    ticket = _itinerary(
        [make_leg("MOW", "TBS", price=13806, baggage=True,
                  depart=datetime(2026, 10, 7, 17, 0, tzinfo=TZ_MOW), duration_min=195)],
        base_config,
    )
    assert ticket.cost.generalized_rub < tour.cost.generalized_rub


def test_tour_with_needed_hotel_can_overtake_cheaper_ticket(base_config):
    """Если отель всё равно нужен, пакет обгоняет более дешёвый билет — но по «полезной» цене."""
    base_config.costs.trip_nights = 5
    base_config.costs.need_baggage = False

    tour = _itinerary([_tour_leg(20000, nights=7, config=base_config)], base_config)
    ticket = _itinerary(
        [make_leg("MOW", "TBS", price=13806, baggage=True,
                  depart=datetime(2026, 10, 7, 17, 0, tzinfo=TZ_MOW), duration_min=195)],
        base_config,
    )

    # К оплате тур дороже, и трекер это не скрывает...
    assert tour.cost.out_of_pocket_rub > ticket.cost.out_of_pocket_rub
    # ...но с учётом пяти оплаченных ночей он выгоднее.
    assert tour.cost.value_rub == pytest.approx(5000)
    assert tour.cost.generalized_rub < ticket.cost.generalized_rub


def test_credit_requires_trip_nights(base_config):
    base_config.costs.trip_nights = 0
    assert accommodation_credit_rub(7, base_config.costs) == 0
    base_config.costs.trip_nights = 2
    assert accommodation_credit_rub(7, base_config.costs) == pytest.approx(6000)
    assert accommodation_credit_rub(1, base_config.costs) == pytest.approx(3000)
    assert accommodation_credit_rub(0, base_config.costs) == 0

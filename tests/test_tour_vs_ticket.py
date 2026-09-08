"""Сравнение пакетного тура с авиабилетом.

Правило: в сравнении участвует тело тура. Цена пакета встаёт в общий ряд с ценами
билетов как есть — включённые отель и обратный перелёт её не уменьшают и не
увеличивают. Тур за 20 000 ₽ выгоднее билета за 25 000 ₽, тур за 34 900 ₽ проигрывает
билету за 13 806 ₽, и никакой бонус этого не переворачивает.
"""

from __future__ import annotations

from datetime import datetime

from conftest import TZ_MOW, make_leg
from tbs_tracker.models import Itinerary, Mode
from tbs_tracker.routing.rules import classify, flags_for
from tbs_tracker.routing.search import compute_cost


def _tour_leg(price: float, *, nights: int) -> object:
    leg = make_leg(
        "MOW", "TBS", price=price, mode=Mode.TOUR, carrier="Anex", baggage=True,
        depart=datetime(2026, 10, 6, 9, 0, tzinfo=TZ_MOW), duration_min=210,
    )
    leg.nights_included = nights
    leg.return_flight_included = True
    return leg


def _itinerary(legs, config) -> Itinerary:
    itinerary = Itinerary(legs=list(legs), chain_class=classify(list(legs)))
    itinerary.cost = compute_cost(list(legs), config)
    itinerary.flags = flags_for(list(legs), itinerary.layovers_min(), config.connections)
    return itinerary


def _ticket(price: float, config) -> Itinerary:
    return _itinerary(
        [make_leg("MOW", "TBS", price=price, baggage=True,
                  depart=datetime(2026, 10, 7, 17, 0, tzinfo=TZ_MOW), duration_min=195)],
        config,
    )


def test_cheaper_tour_beats_pricier_ticket(base_config):
    base_config.costs.need_baggage = False

    tour = _itinerary([_tour_leg(20000, nights=7)], base_config)
    ticket = _ticket(25000, base_config)

    assert tour.cost.out_of_pocket_rub == 20000
    assert ticket.cost.out_of_pocket_rub == 25000
    assert tour.cost.generalized_rub < ticket.cost.generalized_rub
    assert tour.chain_class == "E"
    assert {"package_tour", "hotel_included", "return_included"} <= set(tour.flags)


def test_tour_price_is_taken_as_is(base_config):
    """Ни отель, ни обратный перелёт не меняют цену, по которой сравнивается пакет."""
    base_config.costs.need_baggage = False

    cheap_nights = _itinerary([_tour_leg(20000, nights=2)], base_config)
    many_nights = _itinerary([_tour_leg(20000, nights=14)], base_config)

    assert cheap_nights.cost.out_of_pocket_rub == many_nights.cost.out_of_pocket_rub == 20000
    assert cheap_nights.cost.generalized_rub == many_nights.cost.generalized_rub


def test_included_extras_are_visible_but_priceless(base_config):
    """Состав пакета виден во флагах, но в деньгах не участвует."""
    tour = _itinerary([_tour_leg(20000, nights=7)], base_config)

    assert tour.legs[0].nights_included == 7
    assert tour.legs[0].return_flight_included
    assert {"hotel_included", "return_included"} <= set(tour.flags)
    assert tour.cost.out_of_pocket_rub == 20000


def test_expensive_tour_loses_to_ticket(base_config):
    """Дорогой пакет не должен выигрывать за счёт того, что в нём есть отель."""
    base_config.costs.need_baggage = False

    tour = _itinerary([_tour_leg(34900, nights=7)], base_config)
    ticket = _ticket(13806, base_config)

    assert ticket.cost.out_of_pocket_rub < tour.cost.out_of_pocket_rub
    assert ticket.cost.generalized_rub < tour.cost.generalized_rub


def test_costs_config_has_no_tour_bonus_keys():
    """Настроек, оценивающих содержимое тура, быть не должно."""
    from tbs_tracker.config import CostsConfig

    fields = set(CostsConfig.__dataclass_fields__)
    assert not fields & {
        "trip_nights",
        "tour_accommodation_rub_per_night",
        "return_flight_value_rub",
    }

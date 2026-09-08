from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import TZ_MOW, TZ_TBS, make_leg
from tbs_tracker.geo import detour_ratio
from tbs_tracker.models import Mode
from tbs_tracker.routing.graph import edge_modes, enumerate_paths, leg_queries
from tbs_tracker.routing.rules import can_connect, classify, effective_departure
from tbs_tracker.routing.search import (
    assemble_itineraries,
    compute_cost,
    pareto_front,
)


def test_paths_include_direct_hub_and_ground_corridors(base_config):
    paths = {" → ".join(p.nodes) for p in enumerate_paths(base_config)}
    assert "MOW → TBS" in paths
    assert "MOW → OGZ → TBS" in paths
    assert "MOW → EVN → TBS" in paths
    assert "LED → OGZ → TBS" in paths


def test_paths_reject_absurd_detours(base_config):
    base_config.search.detour_factor = 1.05
    paths = [" → ".join(p.nodes) for p in enumerate_paths(base_config)]
    assert all(detour_ratio(p.split(" → ")) <= 1.05 for p in paths)
    assert "MOW → IST → TBS" not in paths


def test_hub_to_hub_is_not_enumerated(base_config):
    paths = [p.nodes for p in enumerate_paths(base_config)]
    assert not any(nodes[1:3] == ("EVN", "IST") for nodes in paths if len(nodes) > 3)


def test_edge_modes_ground_corridor_and_tour():
    assert Mode.BUS in edge_modes("OGZ", "TBS")
    assert Mode.TRAIN in edge_modes("KUT", "TBS")
    assert Mode.TOUR in edge_modes("MOW", "TBS", allow_tour=True)
    assert Mode.TOUR not in edge_modes("MOW", "TBS")


def test_leg_queries_extend_window_for_later_legs(base_config):
    paths = [p for p in enumerate_paths(base_config) if p.nodes == ("MOW", "OGZ", "TBS")]
    queries = {(q.origin, q.destination): q for q in leg_queries(paths, base_config)}
    first = queries[("MOW", "OGZ")]
    second = queries[("OGZ", "TBS")]
    assert second.date_to > first.date_to


def test_self_transfer_needs_minimum_gap(base_config):
    arrive = datetime(2026, 10, 5, 12, 0, tzinfo=TZ_MOW)
    first = make_leg("MOW", "EVN", price=9000, depart=arrive - timedelta(hours=3), arrive=arrive)
    too_tight = make_leg("EVN", "TBS", price=6000, depart=arrive + timedelta(minutes=60))
    ok = make_leg("EVN", "TBS", price=6000, depart=arrive + timedelta(minutes=240))
    assert not can_connect(first, too_tight, base_config.connections)
    assert can_connect(first, ok, base_config.connections)


def test_flexible_ground_leg_departs_after_buffer(base_config):
    arrive = datetime(2026, 10, 5, 12, 5, tzinfo=TZ_MOW)
    flight = make_leg("MOW", "OGZ", price=6500, depart=arrive - timedelta(hours=2, minutes=45),
                      arrive=arrive)
    bus = make_leg("OGZ", "TBS", price=1500, mode=Mode.BUS, flexible=True, duration_min=360,
                   depart=datetime(2026, 10, 5, 0, 0, tzinfo=TZ_MOW))
    assert can_connect(flight, bus, base_config.connections)
    fixed = effective_departure(flight, bus, base_config.connections)
    # Плечо пересекает наземную границу, поэтому буфер берётся не минимальный
    # наземный, а пограничный: очереди на Верхнем Ларсе — норма.
    assert fixed.depart == arrive + timedelta(minutes=base_config.connections.min_border_min)
    assert fixed.arrive == fixed.depart + timedelta(minutes=360)


def test_layover_over_max_is_rejected(base_config):
    arrive = datetime(2026, 10, 5, 12, 0, tzinfo=TZ_MOW)
    first = make_leg("MOW", "IST", price=13000, depart=arrive - timedelta(hours=3), arrive=arrive)
    late = make_leg("IST", "TBS", price=9000, depart=arrive + timedelta(hours=30))
    assert not can_connect(first, late, base_config.connections)


def test_classify_chain_classes():
    direct = [make_leg("MOW", "TBS", price=13800)]
    with_transfer = [make_leg("MOW", "TBS", price=13800)]
    with_transfer[0].transfers = 1
    ground = [
        make_leg("MOW", "OGZ", price=6500),
        make_leg("OGZ", "TBS", price=1500, mode=Mode.BUS),
    ]
    self_transfer = [
        make_leg("MOW", "IST", price=13000),
        make_leg("IST", "TBS", price=9000),
    ]
    tour = [make_leg("MOW", "TBS", price=9500, mode=Mode.TOUR)]
    assert classify(direct) == "A"
    assert classify(with_transfer) == "B"
    assert classify(ground) == "D"
    assert classify(self_transfer) == "C"
    assert classify(tour) == "E"


def test_cost_includes_baggage_transfer_and_risk(base_config):
    depart = datetime(2026, 10, 5, 9, 20, tzinfo=TZ_MOW)
    flight = make_leg("MOW", "OGZ", price=6500, depart=depart, duration_min=165)
    bus = make_leg(
        "OGZ", "TBS", price=1500, mode=Mode.BUS, baggage=True,
        depart=depart + timedelta(hours=4), duration_min=360,
    )
    cost = compute_cost([flight, bus], base_config)
    assert cost.tickets_rub == 8000
    # Пересадка авиа → наземное плечо: трансфер до автовокзала.
    assert cost.transfers_rub == base_config.costs.airport_transfer_rub
    # Багаж считается только для авиаплеча без включённого багажа.
    assert cost.baggage_rub == base_config.costs.baggage_rub
    assert cost.risk_rub > 0
    assert cost.out_of_pocket_rub == pytest.approx(8000 + 600 + 3000)
    assert cost.generalized_rub > cost.out_of_pocket_rub


def test_overnight_layover_is_charged(base_config):
    arrive = datetime(2026, 10, 5, 9, 35, tzinfo=TZ_MOW)
    first = make_leg("MOW", "AER", price=4200, depart=arrive - timedelta(hours=2, minutes=25),
                     arrive=arrive)
    second = make_leg("AER", "TBS", price=6891, depart=datetime(2026, 10, 6, 6, 30, tzinfo=TZ_MOW),
                      duration_min=85)
    cost = compute_cost([first, second], base_config)
    assert cost.overnight_rub == base_config.costs.overnight_rub


def test_assemble_prefers_cheap_ground_chain(base_config):
    depart = datetime(2026, 10, 5, 9, 20, tzinfo=TZ_MOW)
    legs_by_edge = {
        ("MOW", "TBS"): [
            make_leg("MOW", "TBS", price=13806, depart=depart, duration_min=195)
        ],
        ("MOW", "OGZ"): [
            make_leg("MOW", "OGZ", price=6500, depart=depart, duration_min=165)
        ],
        ("OGZ", "TBS"): [
            make_leg("OGZ", "TBS", price=1500, mode=Mode.BUS, flexible=True, duration_min=360,
                     depart=datetime(2026, 10, 5, 0, 0, tzinfo=TZ_MOW), baggage=True)
        ],
    }
    paths = [p for p in enumerate_paths(base_config)
             if p.nodes in (("MOW", "TBS"), ("MOW", "OGZ", "TBS"))]
    itineraries, stats = assemble_itineraries(paths, legs_by_edge, base_config)
    assert itineraries, stats.filtered_out
    best = itineraries[0]
    assert best.path == ["MOW", "OGZ", "TBS"]
    assert best.chain_class == "D"
    assert best.tickets_rub == 8000
    assert "ground_leg" in best.flags


def test_pareto_front_keeps_faster_but_pricier(base_config):
    depart = datetime(2026, 10, 5, 8, 0, tzinfo=TZ_MOW)
    cheap_slow = make_leg("MOW", "TBS", price=8000, depart=depart, duration_min=900)
    pricey_fast = make_leg("MOW", "TBS", price=14000, depart=depart, duration_min=195)
    legs_by_edge = {("MOW", "TBS"): [cheap_slow, pricey_fast]}
    paths = [p for p in enumerate_paths(base_config) if p.nodes == ("MOW", "TBS")]
    itineraries, _ = assemble_itineraries(paths, legs_by_edge, base_config)
    front = pareto_front(itineraries)
    assert len(front) == 2
    assert front[0].tickets_rub < front[1].tickets_rub
    assert front[0].total_duration_min > front[1].total_duration_min


def test_timezone_aware_duration_across_borders(base_config):
    # Москва (UTC+3) → Тбилиси (UTC+4): 3 часа в пути дают +4 часа по местному времени.
    depart = datetime(2026, 10, 6, 8, 40, tzinfo=TZ_MOW)
    arrive = datetime(2026, 10, 6, 12, 40, tzinfo=TZ_TBS)
    leg = make_leg("MOW", "TBS", price=14154, depart=depart, arrive=arrive)
    assert leg.duration_min == 180

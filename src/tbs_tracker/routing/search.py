"""Сборка цепочек из плеч, расчёт полной стоимости и ранжирование."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from ..config import Config
from ..geo import crosses_border, place
from ..models import CostBreakdown, Itinerary, Leg, Mode
from .graph import RoutePath
from .rules import can_connect, classify, effective_departure, flags_for, min_connection_min

log = logging.getLogger(__name__)

#: Сколько частичных цепочек держать на каждом шаге DP. Ограничение нужно,
#: иначе на широких окнах дат число комбинаций растёт экспоненциально.
MAX_STATES_PER_STEP = 400


@dataclass
class SearchStats:
    paths_considered: int = 0
    legs_available: int = 0
    itineraries_built: int = 0
    filtered_out: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.filtered_out[reason] = self.filtered_out.get(reason, 0) + 1


def assemble_itineraries(
    paths: list[RoutePath],
    legs_by_edge: dict[tuple[str, str], list[Leg]],
    config: Config,
) -> tuple[list[Itinerary], SearchStats]:
    stats = SearchStats(paths_considered=len(paths))
    stats.legs_available = sum(len(v) for v in legs_by_edge.values())
    itineraries: dict[str, Itinerary] = {}

    for path in paths:
        for chain in _chains_for_path(path, legs_by_edge, config, stats):
            itinerary = _build_itinerary(chain, config)
            reason = _reject_reason(itinerary, config)
            if reason:
                stats.reject(reason)
                continue
            existing = itineraries.get(itinerary.itinerary_id)
            if existing is None or (
                itinerary.cost.generalized_rub < existing.cost.generalized_rub
            ):
                itineraries[itinerary.itinerary_id] = itinerary

    result = sorted(itineraries.values(), key=lambda it: it.cost.generalized_rub)
    stats.itineraries_built = len(result)
    return result, stats


def _chains_for_path(
    path: RoutePath,
    legs_by_edge: dict[tuple[str, str], list[Leg]],
    config: Config,
    stats: SearchStats,
) -> list[list[Leg]]:
    edges = path.legs
    first_legs = legs_by_edge.get(edges[0], [])
    if not first_legs:
        return []

    states: list[list[Leg]] = [[leg] for leg in first_legs]
    for edge in edges[1:]:
        candidates = legs_by_edge.get(edge, [])
        if not candidates:
            return []
        next_states: list[list[Leg]] = []
        for chain in states:
            prev = chain[-1]
            for candidate in candidates:
                if not can_connect(prev, candidate, config.connections):
                    continue
                leg = effective_departure(prev, candidate, config.connections)
                next_states.append(chain + [leg])
        states = _prune(next_states)
        if not states:
            stats.reject(f"нет стыковки на {edge[0]}→{edge[1]}")
            return []
    return states


def _prune(states: list[list[Leg]]) -> list[list[Leg]]:
    """Оставить Pareto-оптимальные префиксы по (накопленная цена, время прилёта).

    Дешёвый, но поздно прилетающий префикс может не состыковаться дальше,
    поэтому нельзя оставлять только минимум по цене.
    """
    scored: list[tuple[float, float, list[Leg]]] = []
    for chain in states:
        price = sum(leg.price_rub for leg in chain)
        arrive = chain[-1].arrive
        arrive_ts = arrive.timestamp() if arrive else float("inf")
        scored.append((price, arrive_ts, chain))
    scored.sort(key=lambda item: (item[0], item[1]))

    kept: list[tuple[float, float, list[Leg]]] = []
    best_arrival = float("inf")
    for price, arrive_ts, chain in scored:
        if arrive_ts < best_arrival:
            best_arrival = arrive_ts
            kept.append((price, arrive_ts, chain))
        if len(kept) >= MAX_STATES_PER_STEP:
            break
    return [chain for _, _, chain in kept]


def compute_cost(legs: list[Leg], config: Config) -> CostBreakdown:
    costs = config.costs
    conn = config.connections
    breakdown = CostBreakdown(tickets_rub=sum(leg.price_rub for leg in legs))

    for prev, nxt in zip(legs, legs[1:]):
        # Пересадка между разными видами транспорта или сменой аэропорта — это
        # реальные деньги на трансфер, которые наивное сравнение теряет.
        if prev.mode.is_ground != nxt.mode.is_ground:
            breakdown.transfers_rub += costs.airport_transfer_rub
        elif prev.mode == Mode.AIR and nxt.mode == Mode.AIR:
            required = min_connection_min(prev, nxt, conn)
            if required >= conn.min_airport_change_min:
                breakdown.transfers_rub += costs.airport_transfer_rub
        if nxt.mode.is_ground and crosses_border(nxt.origin, nxt.destination):
            breakdown.transfers_rub += costs.border_transfer_rub

        gap_min = _gap_min(prev, nxt, conn)
        if gap_min >= conn.overnight_threshold_min:
            breakdown.overnight_rub += costs.overnight_rub

        breakdown.risk_rub += _connection_risk(prev, nxt, gap_min, config)

    if costs.need_baggage:
        for leg in legs:
            if leg.mode == Mode.AIR and not leg.baggage_included:
                breakdown.baggage_rub += costs.baggage_rub

    # Проживание, уже включённое в пакет, не снижает его цену, а идёт отдельным
    # зачётом — и только на те ночи, которые вам всё равно нужны.
    breakdown.accommodation_credit_rub = sum(leg.accommodation_credit_rub for leg in legs)

    duration_min = _total_duration_min(legs)
    breakdown.time_cost_rub = (duration_min / 60.0) * costs.time_value_rub_per_hour
    return breakdown


def _connection_risk(prev: Leg, nxt: Leg, gap_min: float, config: Config) -> float:
    """Ожидаемая цена срыва стыковки на двух отдельных билетах.

    Чем ближе стыковка к минимально допустимой, тем выше вероятность потерять
    второе плечо целиком (перевозчик его не перебронирует).
    """
    costs = config.costs
    required = min_connection_min(prev, nxt, config.connections)
    if gap_min <= 0:
        tightness = 1.0
    else:
        tightness = max(0.3, min(1.0, required / gap_min))
    prob = costs.self_transfer_failure_prob * tightness
    if nxt.flexible:
        # Маршрутки уходят несколько раз в день — потеря плеча почти невозможна.
        prob *= 0.25
    return prob * costs.rebooking_cost_rub


def _gap_min(prev: Leg, nxt: Leg, conn) -> float:
    if prev.arrive and nxt.depart:
        return (nxt.depart - prev.arrive).total_seconds() / 60
    return float(min_connection_min(prev, nxt, conn))


def _total_duration_min(legs: list[Leg]) -> int:
    start, end = legs[0].depart, legs[-1].arrive
    if start and end:
        return int((end - start).total_seconds() // 60)
    return sum(leg.duration_min or 0 for leg in legs)


def _build_itinerary(legs: list[Leg], config: Config) -> Itinerary:
    itinerary = Itinerary(legs=list(legs), chain_class=classify(legs))
    itinerary.cost = compute_cost(legs, config)
    itinerary.flags = flags_for(legs, itinerary.layovers_min(), config.connections)
    return itinerary


def _reject_reason(itinerary: Itinerary, config: Config) -> str | None:
    filters = config.filters
    price = itinerary.tickets_rub
    if price < filters.sanity_min_price_rub:
        return "цена ниже порога правдоподобия"
    if price > filters.sanity_max_price_rub:
        return "цена выше порога правдоподобия"
    if itinerary.total_duration_min > filters.max_total_duration_hours * 60:
        return "слишком длинный маршрут"
    if not filters.allow_foreign_card_only and "requires_foreign_card" in itinerary.flags:
        return "требует иностранной карты"
    if not filters.allow_cached_prices and itinerary.price_kind.value != "live":
        return "цена не живая"
    excluded = {c.upper() for c in filters.exclude_carriers}
    if excluded and any((leg.carrier or "").upper() in excluded for leg in itinerary.legs):
        return "исключённый перевозчик"
    if itinerary.destination not in config.search.destinations:
        return "маршрут не заканчивается в целевом городе"
    return None


def pareto_front(itineraries: list[Itinerary]) -> list[Itinerary]:
    """Варианты, которые нельзя улучшить одновременно по цене и по времени."""
    ordered = sorted(
        itineraries, key=lambda it: (it.cost.out_of_pocket_rub, it.total_duration_min)
    )
    front: list[Itinerary] = []
    best_duration = float("inf")
    for itinerary in ordered:
        if itinerary.total_duration_min < best_duration:
            best_duration = itinerary.total_duration_min
            front.append(itinerary)
    return front


def cheapest_by_class(itineraries: list[Itinerary]) -> dict[str, Itinerary]:
    best: dict[str, Itinerary] = {}
    for itinerary in itineraries:
        current = best.get(itinerary.chain_class)
        if current is None or itinerary.cost.generalized_rub < current.cost.generalized_rub:
            best[itinerary.chain_class] = itinerary
    return best

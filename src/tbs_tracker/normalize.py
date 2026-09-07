"""Нормализация офферов: отсечение мусора, дедупликация, группировка по плечам."""

from __future__ import annotations

import logging
from collections import defaultdict

from .config import Config
from .models import Leg, Mode

log = logging.getLogger(__name__)


def sanity_ok(leg: Leg, config: Config) -> bool:
    filters = config.filters
    if leg.price_rub <= 0:
        return False
    # Наземные плечи легально дешёвые, порог правдоподобия к ним не применяем.
    if leg.mode == Mode.AIR and leg.price_rub < filters.sanity_min_price_rub:
        return False
    if leg.price_rub > filters.sanity_max_price_rub:
        return False
    if leg.depart is None and not leg.flexible:
        return False
    return True


def dedupe(legs: list[Leg]) -> list[Leg]:
    """Один и тот же рейс приходит из нескольких источников — оставляем лучший.

    «Лучший» = самая низкая цена, а при равной цене — более достоверный тип цены
    (live лучше cached), потому что именно её можно купить.
    """
    kind_rank = {"live": 0, "cached": 1, "estimate": 2}
    best: dict[tuple, Leg] = {}
    for leg in legs:
        bucket_key = (
            leg.origin,
            leg.destination,
            leg.mode.value,
            (leg.carrier or "").upper(),
            leg.flight_number or "",
            leg.depart.replace(second=0, microsecond=0).isoformat() if leg.depart else "flex",
            # Цены в пределах 100 ₽ считаем одной и той же.
            round(leg.price_rub / 100.0),
        )
        current = best.get(bucket_key)
        if current is None:
            best[bucket_key] = leg
            continue
        better = (leg.price_rub, kind_rank.get(leg.price_kind.value, 3)) < (
            current.price_rub,
            kind_rank.get(current.price_kind.value, 3),
        )
        if better:
            best[bucket_key] = leg
    return list(best.values())


def group_by_edge(
    legs: list[Leg], config: Config
) -> dict[tuple[str, str], list[Leg]]:
    """Отфильтровать, дедуплицировать и разложить плечи по парам городов."""
    grouped: dict[tuple[str, str], list[Leg]] = defaultdict(list)
    dropped = 0
    for leg in legs:
        if not sanity_ok(leg, config):
            dropped += 1
            continue
        grouped[(leg.origin, leg.destination)].append(leg)
    if dropped:
        log.info("отброшено %d неправдоподобных офферов", dropped)

    result: dict[tuple[str, str], list[Leg]] = {}
    keep = int(config.search.top_n) * 8
    for edge, edge_legs in grouped.items():
        unique = dedupe(edge_legs)
        unique.sort(key=lambda leg: (leg.price_rub, _sort_time(leg)))
        # На одно плечо держим ограниченный пул кандидатов: дальше их всё равно
        # отсечёт Pareto-прунинг, а комбинаторика растёт быстро.
        result[edge] = unique[:keep]
    return result


def _sort_time(leg: Leg) -> float:
    return leg.depart.timestamp() if leg.depart else float("inf")

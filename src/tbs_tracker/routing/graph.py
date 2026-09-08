"""Топология маршрутов: какие цепочки городов вообще имеет смысл считать.

Перечисление путей делается ДО запросов к источникам — это главный способ не сжечь
квоту: вместо полного перебора «все города × все хабы × все даты» остаются десятки
осмысленных плеч.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..config import Config
from ..geo import (
    GEORGIA_ENTRY_POINTS,
    KNOWN_DIRECT_TO_GEORGIA,
    detour_ratio,
    distance_km,
    place,
)
from ..models import LegQuery, Mode

#: Пары, где наземное плечо — основной способ передвижения.
GROUND_CORRIDORS: frozenset[frozenset[str]] = frozenset({
    frozenset({"OGZ", "TBS"}),
    frozenset({"MRV", "TBS"}),
    frozenset({"EVN", "TBS"}),
    frozenset({"GYD", "TBS"}),
    frozenset({"KUT", "TBS"}),
    frozenset({"BUS", "TBS"}),
    frozenset({"BUS", "KUT"}),
})

#: Пары с осмысленным железнодорожным сообщением.
TRAIN_CORRIDORS: frozenset[frozenset[str]] = frozenset({
    frozenset({"KUT", "TBS"}),
    frozenset({"BUS", "TBS"}),
})

#: Минимальное расстояние, при котором перелёт вообще имеет смысл.
MIN_AIR_DISTANCE_KM = 200.0


@dataclass(frozen=True)
class RoutePath:
    """Кандидат-цепочка городов, например ('MOW', 'OGZ', 'TBS')."""

    nodes: tuple[str, ...]

    @property
    def legs(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.nodes, self.nodes[1:]))

    @property
    def leg_count(self) -> int:
        return len(self.nodes) - 1

    def __str__(self) -> str:
        return " → ".join(self.nodes)


def edge_modes(origin: str, destination: str, *, allow_tour: bool = False) -> tuple[Mode, ...]:
    """Какими способами можно проехать это плечо."""
    modes: list[Mode] = []
    pair = frozenset({origin, destination})
    a, b = place(origin), place(destination)

    if a.airports and b.airports and distance_km(origin, destination) >= MIN_AIR_DISTANCE_KM:
        modes.append(Mode.AIR)
    # Наземное плечо через границу берём только там, где коридор реально работает:
    # «близко по прямой» ничего не значит, если между городами нет перехода.
    same_country_short = a.country == b.country and distance_km(origin, destination) <= 700
    if pair in GROUND_CORRIDORS or same_country_short:
        modes.append(Mode.BUS)
        modes.append(Mode.TAXI)
    if pair in TRAIN_CORRIDORS:
        modes.append(Mode.TRAIN)
    if allow_tour and a.country == "RU" and b.country == "GE":
        modes.append(Mode.TOUR)
    return tuple(dict.fromkeys(modes))


def _edge_allowed(origin: str, destination: str, position: int, config: Config) -> bool:
    """Разрешено ли плечо на данной позиции в цепочке."""
    if origin == destination:
        return False
    a, b = place(origin), place(destination)

    # Финальное плечо всегда ведёт в цель.
    if b.code in config.search.destinations:
        return True
    # Первое плечо: подвоз внутри РФ до опорного города вылета.
    if position == 0 and a.country == "RU" and b.country == "RU":
        return b.code in config.search.origins
    # Из РФ — только в разрешённый хаб или в точку входа в Грузию.
    if a.country == "RU":
        return b.code in config.search.hubs or b.code in GEORGIA_ENTRY_POINTS
    # Из хаба — в точку входа в Грузию (хаб→хаб не рассматриваем: цена почти
    # никогда не оправдывает удлинение цепочки).
    if b.code in GEORGIA_ENTRY_POINTS:
        return True
    return False


def enumerate_paths(config: Config) -> list[RoutePath]:
    """Все допустимые цепочки городов от origins до destinations."""
    search = config.search
    targets = set(search.destinations)
    allowed_nodes = set(search.origins) | set(search.hubs) | set(search.entry_points) | targets
    results: list[RoutePath] = []
    seen: set[tuple[str, ...]] = set()

    def walk(nodes: tuple[str, ...]) -> None:
        current = nodes[-1]
        if current in targets and len(nodes) > 1:
            if nodes not in seen and detour_ratio(list(nodes)) <= search.detour_factor:
                seen.add(nodes)
                results.append(RoutePath(nodes))
            # Цель достигнута — дальше не идём.
            return
        if len(nodes) - 1 >= search.max_legs:
            return
        for candidate in sorted(allowed_nodes):
            if candidate in nodes:
                continue
            if not _edge_allowed(current, candidate, len(nodes) - 1, config):
                continue
            if not edge_modes(current, candidate, allow_tour=True):
                continue
            walk(nodes + (candidate,))

    for origin in search.origins:
        walk((origin,))

    results.sort(key=lambda p: (p.leg_count, detour_ratio(list(p.nodes))))
    return results


def leg_queries(paths: list[RoutePath], config: Config) -> list[LegQuery]:
    """Уникальные запросы цен по плечам всех кандидатов.

    Для плеч после первого окно дат расширяется вперёд: пересадка может
    переносить вылет на следующий день (а с ночной стыковкой — на сутки+).
    """
    slack_days = max(1, config.connections.max_layover_min // (60 * 24) + 1)
    queries: dict[str, LegQuery] = {}
    for path in paths:
        for position, (origin, destination) in enumerate(path.legs):
            modes = edge_modes(origin, destination, allow_tour=(position == 0))
            if not modes:
                continue
            for window in config.search.windows:
                date_from = window.date_from
                date_to = window.date_to + timedelta(days=slack_days * position)
                if position:
                    date_from = window.date_from
                query = LegQuery(
                    origin=origin,
                    destination=destination,
                    date_from=date_from,
                    date_to=date_to,
                    modes=modes,
                    passengers=config.search.passengers,
                )
                queries.setdefault(query.key, query)
    return sorted(queries.values(), key=_query_priority)


def _query_priority(query: LegQuery) -> tuple[int, float]:
    """Сначала опрашиваем плечи, которые чаще всего дают минимум цены.

    Порядок важен из-за бюджета запросов: если он кончится, кончится на
    маловажных плечах, а не на прямых рейсах и наземном коридоре.
    """
    pair = frozenset({query.origin, query.destination})
    known_direct = any(
        (route.origin, route.destination) == (query.origin, query.destination)
        for route in KNOWN_DIRECT_TO_GEORGIA
    )
    if known_direct:
        rank = 0
    elif pair in GROUND_CORRIDORS:
        rank = 1
    elif place(query.destination).country == "GE":
        rank = 2
    else:
        rank = 3
    return rank, distance_km(query.origin, query.destination)

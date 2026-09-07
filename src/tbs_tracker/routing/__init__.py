"""Построение логистических цепочек: топология, стыковки, ранжирование."""

from .graph import RoutePath, edge_modes, enumerate_paths, leg_queries
from .rules import can_connect, classify, flags_for, min_connection_min
from .search import (
    SearchStats,
    assemble_itineraries,
    cheapest_by_class,
    compute_cost,
    pareto_front,
)

__all__ = [
    "RoutePath",
    "SearchStats",
    "assemble_itineraries",
    "can_connect",
    "cheapest_by_class",
    "classify",
    "compute_cost",
    "edge_modes",
    "enumerate_paths",
    "flags_for",
    "leg_queries",
    "min_connection_min",
    "pareto_front",
]

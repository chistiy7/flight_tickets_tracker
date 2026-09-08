"""Трекер дешёвых способов добраться из России в Тбилиси.

Пакет собирает цены из нескольких источников (агрегаторы, прямые сайты авиакомпаний,
горячие туры, наземный транспорт), склеивает из них логистические цепочки с учётом
реальных стыковок и полной стоимости поездки, ведёт историю цен и алертит на падения.

Список источников — docs/sources.md, логика маршрутов — docs/routing.md.
"""

from .collector import CollectorStore, offer_to_leg
from .config import Config
from .models import CostBreakdown, Itinerary, Leg, LegQuery, Mode, PriceKind
from .pipeline import CollectReport, RunReport, collect_offers, run_tracker

__version__ = "0.1.0"

__all__ = [
    "CollectReport",
    "CollectorStore",
    "Config",
    "CostBreakdown",
    "Itinerary",
    "Leg",
    "LegQuery",
    "Mode",
    "PriceKind",
    "RunReport",
    "collect_offers",
    "offer_to_leg",
    "run_tracker",
    "__version__",
]

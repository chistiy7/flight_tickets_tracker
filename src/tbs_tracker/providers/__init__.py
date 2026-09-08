"""Провайдеры цен. Импорт модулей регистрирует их в реестре."""

from .base import Provider, ProviderResult, available_providers, build_providers, register
from . import (
    airline_direct,
    collector,
    duffel,
    fixtures,
    ground,
    serpapi_flights,
    tourvisor,
    travelpayouts,
)

__all__ = [
    "Provider",
    "ProviderResult",
    "available_providers",
    "build_providers",
    "register",
    "airline_direct",
    "collector",
    "duffel",
    "fixtures",
    "ground",
    "serpapi_flights",
    "tourvisor",
    "travelpayouts",
]

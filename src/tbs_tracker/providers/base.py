"""Базовый контракт провайдера цен и реестр провайдеров.

Провайдер отвечает на вопрос «сколько стоит это плечо в этом окне дат» и обязан
деградировать мягко: нет токена — вернуть `skipped_reason`, упал API — вернуть
`error`, но не ломать прогон целиком.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable, Iterable

from ..config import Config, ProviderConfig
from ..fx import CurrencyConverter
from ..httpclient import HttpClient, RequestBudgetExceeded
from ..models import Leg, LegQuery, Mode, ProviderResult, TourOffer

log = logging.getLogger(__name__)


class Provider(ABC):
    #: Уникальное имя, оно же ключ в config.providers.
    name: str = "base"
    #: Какие режимы передвижения умеет искать провайдер.
    modes: tuple[Mode, ...] = (Mode.AIR,)
    #: Тип цены, который источник отдаёт по своей природе.
    price_kind_default = None

    def __init__(
        self,
        config: Config,
        provider_config: ProviderConfig,
        http: HttpClient,
        fx: CurrencyConverter,
    ) -> None:
        self.config = config
        self.options = provider_config
        self.http = http
        self.fx = fx

    def supports(self, query: LegQuery) -> bool:
        return any(mode in self.modes for mode in query.modes)

    def unavailable_reason(self) -> str | None:
        """Почему провайдер не может работать (нет токена и т. п.)."""
        return None

    @abstractmethod
    def fetch(self, query: LegQuery) -> ProviderResult:
        """Вернуть плечи по запросу. Исключения наружу не выпускать."""

    def fetch_tours(self, query: LegQuery) -> list[TourOffer]:
        return []

    # ------------------------------------------------------------- helpers
    def _result(self, query: LegQuery, legs: Iterable[Leg], requests_used: int = 0) -> ProviderResult:
        return ProviderResult(
            provider=self.name, query=query, legs=list(legs), requests_used=requests_used
        )

    def _skipped(self, query: LegQuery, reason: str) -> ProviderResult:
        return ProviderResult(provider=self.name, query=query, skipped_reason=reason)

    def _failed(self, query: LegQuery, error: str) -> ProviderResult:
        return ProviderResult(provider=self.name, query=query, error=error)

    def safe_fetch(self, query: LegQuery) -> ProviderResult:
        reason = self.unavailable_reason()
        if reason:
            return self._skipped(query, reason)
        if not self.supports(query):
            return self._skipped(query, "режим передвижения не поддерживается провайдером")
        try:
            return self.fetch(query)
        except RequestBudgetExceeded:
            # Бюджет — это не сбой источника: запросы кончились, и это нормальный
            # исход для широких окон дат.
            return self._skipped(query, "исчерпан бюджет запросов на прогон")
        except Exception as exc:  # noqa: BLE001 - изолируем сбой источника
            log.warning("провайдер %s упал на %s: %s", self.name, query.key, exc)
            return self._failed(query, str(exc))


_REGISTRY: dict[str, Callable[..., Provider]] = {}


def register(provider_cls: type[Provider]) -> type[Provider]:
    _REGISTRY[provider_cls.name] = provider_cls
    return provider_cls


def available_providers() -> dict[str, type[Provider]]:
    return dict(_REGISTRY)  # type: ignore[return-value]


def build_providers(
    config: Config, http: HttpClient, fx: CurrencyConverter
) -> list[Provider]:
    providers: list[Provider] = []
    for name, provider_config in config.providers.items():
        if not provider_config.enabled:
            continue
        factory = _REGISTRY.get(name)
        if factory is None:
            log.warning("в конфиге включён неизвестный провайдер '%s' — пропускаем", name)
            continue
        providers.append(factory(config, provider_config, http, fx))
    return providers


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

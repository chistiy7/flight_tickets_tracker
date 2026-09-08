"""Конвейер прогона: топология → цены по плечам → цепочки → история → алерты."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .alerts import Alert, Notifier, evaluate
from .collector import CollectorPolicy, CollectorStats, CollectorStore, collector_db_path
from .config import Config
from .fx import CurrencyConverter
from .httpclient import HttpClient, RequestBudget
from .models import Itinerary, Leg, LegQuery, ProviderResult
from .normalize import group_by_edge
from .providers import build_providers
from .providers.base import Provider
from .providers.collector import CollectorProvider
from .routing.graph import RoutePath, enumerate_paths, leg_queries
from .routing.search import SearchStats, assemble_itineraries
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    started_at: datetime
    paths: list[RoutePath]
    queries: list[LegQuery]
    provider_results: list[ProviderResult] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    itineraries: list[Itinerary] = field(default_factory=list)
    stats: SearchStats = field(default_factory=SearchStats)
    alerts: list[Alert] = field(default_factory=list)
    requests_used: int = 0
    run_id: int | None = None
    #: Готовый нотификатор — чтобы вызывающий код мог отправить алерты сам
    #: (например, после печати отчёта).
    notifier: Notifier | None = None

    @property
    def best(self) -> Itinerary | None:
        return self.itineraries[0] if self.itineraries else None


def build_runtime(config: Config) -> tuple[HttpClient, CurrencyConverter, list[Provider]]:
    budget = RequestBudget(limit=config.runtime.max_requests_per_run)
    http = HttpClient(
        cache_dir=config.resolve_path(config.runtime.cache_dir),
        cache_ttl_minutes=config.runtime.cache_ttl_minutes,
        timeout_sec=config.runtime.request_timeout_sec,
        budget=budget,
    )
    fx = CurrencyConverter(config.runtime.fx, http)
    providers = build_providers(config, http, fx)
    return http, fx, providers


def provider_status(providers: list[Provider]) -> dict[str, str | None]:
    return {provider.name: provider.unavailable_reason() for provider in providers}


def fetch_legs(
    providers: list[Provider], queries: list[LegQuery], config: Config
) -> tuple[list[ProviderResult], list[Leg]]:
    tasks: list[tuple[Provider, LegQuery]] = [
        (provider, query)
        for query in queries
        for provider in providers
        if provider.supports(query)
    ]
    if not tasks:
        return [], []

    results: list[ProviderResult] = []
    with ThreadPoolExecutor(max_workers=max(1, config.runtime.max_workers)) as pool:
        for result in pool.map(lambda item: item[0].safe_fetch(item[1]), tasks):
            results.append(result)

    legs = [leg for result in results for leg in result.legs]
    return results, legs


@dataclass
class CollectReport:
    """Итог сеанса сбора: что положили на склад и в каком он состоянии."""

    started_at: datetime
    db_path: Path
    queries: list[LegQuery]
    provider_results: list[ProviderResult] = field(default_factory=list)
    stored: int = 0
    pruned: int = 0
    requests_used: int = 0
    stats: CollectorStats | None = None


def collect_offers(config: Config, *, prune: bool = True) -> CollectReport:
    """Сеанс сбора: опросить источники и сложить офферы на склад.

    Цепочки здесь не собираются и алерты не считаются — это работа прогона.
    Сбор нарочно отделён: он медленный, частично ломается и запускается по своему
    расписанию, а прогон должен оставаться быстрым и повторяемым.
    """
    started = datetime.now(timezone.utc)
    http, _fx, providers = build_runtime(config)
    # Сам себя склад не опрашивает: иначе прочитанные вчера цены записывались бы
    # обратно как собранные сегодня и никогда не старели.
    parsers = [p for p in providers if p.name != CollectorProvider.name]
    if not parsers:
        log.warning("нет ни одного источника для сбора — включите провайдеры в конфиге")

    paths = enumerate_paths(config)
    queries = leg_queries(paths, config)
    provider_results, legs = fetch_legs(parsers, queries, config)

    policy = CollectorPolicy.from_config(config)
    db_path = collector_db_path(config)
    with CollectorStore(db_path) as store:
        stored = store.put(legs, collected_at=datetime.now(timezone.utc))
        pruned = store.prune(policy.max_age_minutes) if prune else 0
        stats = store.stats(fresh_minutes=policy.fresh_minutes)

    log.info("на склад записано %d офферов, вычищено %d", stored, pruned)
    return CollectReport(
        started_at=started,
        db_path=db_path,
        queries=queries,
        provider_results=provider_results,
        stored=stored,
        pruned=pruned,
        requests_used=http.budget.used if http.budget else 0,
        stats=stats,
    )


def run_tracker(
    config: Config,
    *,
    store: Store | None = None,
    notify: bool = True,
    persist: bool = True,
) -> RunReport:
    started = datetime.now(timezone.utc)
    http, _fx, providers = build_runtime(config)

    if not providers:
        log.warning("не включён ни один провайдер — смотрите секцию providers в конфиге")

    paths = enumerate_paths(config)
    queries = leg_queries(paths, config)
    log.info("топология: %d путей, %d запросов по плечам", len(paths), len(queries))

    provider_results, raw_legs = fetch_legs(providers, queries, config)
    legs_by_edge = group_by_edge(raw_legs, config)
    log.info(
        "получено %d офферов на %d плечах", len(raw_legs), len(legs_by_edge)
    )

    itineraries, stats = assemble_itineraries(paths, legs_by_edge, config)

    report = RunReport(
        started_at=started,
        paths=paths,
        queries=queries,
        provider_results=provider_results,
        legs=raw_legs,
        itineraries=itineraries,
        stats=stats,
        requests_used=http.budget.used if http.budget else 0,
    )

    owns_store = store is None
    if persist:
        store = store or Store(config.resolve_path(config.runtime.db_path))
        try:
            run_id = store.start_run(p.name for p in providers)
            report.run_id = run_id
            store.record_legs(run_id, raw_legs)
            store.record_itineraries(run_id, itineraries)
            report.alerts = evaluate(itineraries, store, config, run_id=run_id)
            store.finish_run(
                run_id,
                requests_used=report.requests_used,
                itineraries=len(itineraries),
                notes=f"paths={len(paths)} queries={len(queries)}",
            )
        finally:
            if owns_store:
                store.close()

    report.notifier = Notifier(config, http)
    if notify and report.alerts:
        report.notifier.send(report.alerts)

    return report

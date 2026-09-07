"""Текстовые и JSON-отчёты."""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from .config import Config
from .models import Itinerary, ProviderResult
from .routing.search import SearchStats, cheapest_by_class, pareto_front
from .timeutil import fmt_dt, fmt_duration

CLASS_TITLES = {
    "A": "прямой рейс",
    "B": "одна авиастыковка (единый билет)",
    "C": "self-transfer (два билета)",
    "D": "с наземным плечом",
    "E": "пакетный тур",
    "F": "open-jaw",
    "?": "не классифицирован",
}


def unique_by_route(itineraries: Sequence[Itinerary]) -> list[Itinerary]:
    """Лучший вариант на каждый маршрут.

    Без этого обзорная таблица забивается одним и тем же маршрутом на разные даты
    и не показывает альтернативы.
    """
    best: dict[str, Itinerary] = {}
    for itinerary in itineraries:
        current = best.get(itinerary.route_key)
        if current is None or itinerary.cost.generalized_rub < current.cost.generalized_rub:
            best[itinerary.route_key] = itinerary
    return sorted(best.values(), key=lambda it: it.cost.generalized_rub)


def render_table(
    itineraries: Sequence[Itinerary], limit: int = 15, *, one_per_route: bool = True
) -> str:
    if not itineraries:
        return "Ничего не найдено."
    if one_per_route:
        itineraries = unique_by_route(itineraries)
    header = (
        f"{'Маршрут':<26}{'Вылет':<13}{'В пути':<10}{'Билеты':>10}"
        f"{'Итого':>10}{'Кл':>4}  Источники / флаги"
    )
    lines = [header, "-" * len(header)]
    for itinerary in itineraries[:limit]:
        route = " → ".join(itinerary.path)
        extras = ", ".join(itinerary.sources)
        if itinerary.flags:
            extras = f"{extras} | {', '.join(itinerary.flags)}"
        lines.append(
            f"{route:<26}{fmt_dt(itinerary.depart):<13}"
            f"{fmt_duration(itinerary.total_duration_min):<10}"
            f"{itinerary.tickets_rub:>10.0f}"
            f"{itinerary.cost.out_of_pocket_rub:>10.0f}"
            f"{itinerary.chain_class:>4}  {extras}"
        )
    return "\n".join(lines)


def render_details(itinerary: Itinerary) -> str:
    lines = [
        f"{' → '.join(itinerary.path)} | класс {itinerary.chain_class} "
        f"({CLASS_TITLES.get(itinerary.chain_class, '')})",
        f"вылет {fmt_dt(itinerary.depart)}, прилёт {fmt_dt(itinerary.arrive)}, "
        f"в пути {fmt_duration(itinerary.total_duration_min)}",
    ]
    for leg in itinerary.legs:
        lines.append(f"  • {leg.describe()} [{leg.source}, {leg.price_kind.value}]")
        if leg.notes:
            lines.append(f"      {leg.notes}")
    gaps = itinerary.layovers_min()
    if gaps:
        lines.append("  стыковки: " + ", ".join(fmt_duration(g) for g in gaps))
    cost = itinerary.cost.as_dict()
    lines.append(
        "  стоимость: билеты {tickets_rub:.0f}₽ + трансферы {transfers_rub:.0f}₽ "
        "+ багаж {baggage_rub:.0f}₽ + ночёвки {overnight_rub:.0f}₽ "
        "= {out_of_pocket_rub:.0f}₽; с риском и временем {generalized_rub:.0f}₽".format(**cost)
    )
    if itinerary.flags:
        lines.append(f"  флаги: {', '.join(itinerary.flags)}")
    return "\n".join(lines)


def render_run_report(
    itineraries: Sequence[Itinerary],
    stats: SearchStats,
    provider_results: Sequence[ProviderResult],
    *,
    limit: int = 15,
    details: int = 3,
) -> str:
    blocks = [render_table(itineraries, limit)]

    best_by_class = cheapest_by_class(list(itineraries))
    if best_by_class:
        lines = ["", "Лучшее по классам цепочек:"]
        for chain_class in sorted(best_by_class):
            itinerary = best_by_class[chain_class]
            lines.append(
                f"  {chain_class} ({CLASS_TITLES.get(chain_class, '')}): "
                f"{' → '.join(itinerary.path)} — {itinerary.cost.out_of_pocket_rub:.0f}₽, "
                f"{fmt_duration(itinerary.total_duration_min)}"
            )
        blocks.append("\n".join(lines))

    front = pareto_front(list(itineraries))
    if len(front) > 1:
        lines = ["", "Pareto-фронт (цена / время):"]
        for itinerary in front[:8]:
            lines.append(
                f"  {itinerary.cost.out_of_pocket_rub:>8.0f}₽  "
                f"{fmt_duration(itinerary.total_duration_min):<10} "
                f"{' → '.join(itinerary.path)}"
            )
        blocks.append("\n".join(lines))

    if details and itineraries:
        lines = ["", "Детали лучших вариантов:"]
        for itinerary in unique_by_route(itineraries)[:details]:
            lines.append(render_details(itinerary))
            lines.append("")
        blocks.append("\n".join(lines))

    blocks.append(render_provider_summary(provider_results))
    blocks.append(render_stats(stats))
    return "\n".join(blocks)


def render_provider_summary(results: Iterable[ProviderResult]) -> str:
    per_provider: dict[str, dict[str, Any]] = {}
    for result in results:
        entry = per_provider.setdefault(
            result.provider,
            {"legs": 0, "requests": 0, "errors": [], "skipped": set()},
        )
        entry["legs"] += len(result.legs)
        entry["requests"] += result.requests_used
        if result.error:
            entry["errors"].append(result.error)
        if result.skipped_reason:
            entry["skipped"].add(result.skipped_reason)

    lines = ["", "Источники:"]
    if not per_provider:
        lines.append("  ни один источник не опрошен")
    for name in sorted(per_provider):
        entry = per_provider[name]
        status = f"плеч {entry['legs']}, запросов {entry['requests']}"
        if entry["skipped"]:
            status += f"; пропущен: {'; '.join(sorted(entry['skipped']))}"
        if entry["errors"]:
            status += f"; ошибок {len(entry['errors'])}: {entry['errors'][0]}"
        lines.append(f"  {name}: {status}")
    return "\n".join(lines)


def render_stats(stats: SearchStats) -> str:
    lines = [
        "",
        f"Топология: путей {stats.paths_considered}, плеч с ценами {stats.legs_available}, "
        f"цепочек собрано {stats.itineraries_built}",
    ]
    if stats.filtered_out:
        top = sorted(stats.filtered_out.items(), key=lambda kv: -kv[1])[:6]
        lines.append("Отсечено: " + ", ".join(f"{reason} ×{count}" for reason, count in top))
    return "\n".join(lines)


def render_json(itineraries: Sequence[Itinerary]) -> str:
    payload = [
        {
            "route": itinerary.path,
            "chain_class": itinerary.chain_class,
            "depart": itinerary.depart.isoformat() if itinerary.depart else None,
            "arrive": itinerary.arrive.isoformat() if itinerary.arrive else None,
            "duration_min": itinerary.total_duration_min,
            "tickets_rub": round(itinerary.tickets_rub, 2),
            "cost": itinerary.cost.as_dict(),
            "price_kind": itinerary.price_kind.value,
            "flags": itinerary.flags,
            "sources": itinerary.sources,
            "legs": [
                {
                    "origin": leg.origin,
                    "destination": leg.destination,
                    "mode": leg.mode.value,
                    "carrier": leg.carrier,
                    "flight_number": leg.flight_number,
                    "depart": leg.depart.isoformat() if leg.depart else None,
                    "arrive": leg.arrive.isoformat() if leg.arrive else None,
                    "price_rub": round(leg.price_rub, 2),
                    "price_original": leg.price_original,
                    "currency": leg.currency,
                    "price_kind": leg.price_kind.value,
                    "source": leg.source,
                    "deep_link": leg.deep_link,
                    "notes": leg.notes,
                }
                for leg in itinerary.legs
            ],
        }
        for itinerary in itineraries
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_source_catalog(config: Config, provider_status: dict[str, str | None]) -> str:
    """Каталог источников цен со статусом подключения."""
    from .providers.base import available_providers

    lines = ["Провайдеры цен (детали и полный список источников — docs/sources.md):", ""]
    for name, cls in sorted(available_providers().items()):
        cfg = config.providers.get(name)
        enabled = "включён" if cfg and cfg.enabled else "выключен"
        modes = ", ".join(m.value for m in cls.modes)
        reason = provider_status.get(name)
        status = "готов" if reason is None else reason
        if not (cfg and cfg.enabled):
            status = "—"
        lines.append(f"  {name:<26} {enabled:<10} режимы: {modes:<24} {status}")
    return "\n".join(lines)

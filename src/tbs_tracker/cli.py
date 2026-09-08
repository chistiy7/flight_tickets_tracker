"""CLI трекера.

    tbs-tracker run      — полный прогон: опрос источников, сборка цепочек, алерты
    tbs-tracker collect  — сбор цен на склад офферов, без сборки цепочек
    tbs-tracker routes   — топология маршрутов и план запросов, без обращения к сети
    tbs-tracker sources  — какие источники подключены и чего им не хватает
    tbs-tracker history  — история наблюдений и рекорды по маршрутам
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .collector import CollectorPolicy, CollectorStats, CollectorStore, collector_db_path
from .config import Config, ConfigError
from .geo import PLACES, detour_ratio, distance_km
from .pipeline import build_runtime, collect_offers, provider_status, run_tracker
from .report import render_json, render_provider_summary, render_run_report, render_source_catalog
from .routing.graph import edge_modes, enumerate_paths, leg_queries
from .store import Store
from .timeutil import fmt_duration

DEFAULT_CONFIG = "config/config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbs-tracker",
        description="Трекер самых дешёвых способов добраться из России в Тбилиси",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="путь к YAML-конфигу")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="подробные логи")
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="прогон: цены, цепочки, алерты")
    run_cmd.add_argument("--limit", type=int, default=None, help="сколько вариантов показать")
    run_cmd.add_argument(
        "--details",
        type=int,
        default=3,
        help="для скольких вариантов расписать плечи и ссылки на покупку",
    )
    run_cmd.add_argument("--json", action="store_true", help="вывести результат в JSON")
    run_cmd.add_argument("--no-alerts", action="store_true", help="не отправлять уведомления")
    run_cmd.add_argument("--no-store", action="store_true", help="не писать историю в БД")

    collect_cmd = sub.add_parser("collect", help="собрать цены на склад офферов")
    collect_cmd.add_argument(
        "--status", action="store_true", help="только показать состояние склада, не собирать"
    )
    collect_cmd.add_argument(
        "--keep-stale", action="store_true", help="не вычищать записи старше max_age_minutes"
    )

    routes_cmd = sub.add_parser("routes", help="показать топологию маршрутов без запросов")
    routes_cmd.add_argument("--limit", type=int, default=40)
    routes_cmd.add_argument("--queries", action="store_true", help="показать план запросов")

    sub.add_parser("sources", help="статус подключённых источников цен")

    history_cmd = sub.add_parser("history", help="история наблюдений")
    history_cmd.add_argument("--route", default=None, help="фильтр по маршруту, напр. MOW>OGZ>TBS")
    history_cmd.add_argument("--limit", type=int, default=20)
    history_cmd.add_argument("--best", action="store_true", help="рекорды по маршрутам")

    sub.add_parser("places", help="справочник городов и их роль в логистике")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "places":
        return _cmd_places()

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.command == "run":
        return _cmd_run(config, args)
    if args.command == "collect":
        return _cmd_collect(config, args)
    if args.command == "routes":
        return _cmd_routes(config, args)
    if args.command == "sources":
        return _cmd_sources(config)
    if args.command == "history":
        return _cmd_history(config, args)
    parser.error(f"неизвестная команда {args.command}")
    return 2


def _cmd_run(config: Config, args: argparse.Namespace) -> int:
    # Уведомления отправляем после печати отчёта, чтобы консольный вывод не
    # начинался с алертов.
    report = run_tracker(config, notify=False, persist=not args.no_store)
    limit = args.limit or config.search.top_n
    if args.json:
        print(render_json(report.itineraries[:limit]))
    else:
        print(
            render_run_report(
                report.itineraries,
                report.stats,
                report.provider_results,
                limit=limit,
                details=args.details,
            )
        )
        print(f"\nЗапросов израсходовано: {report.requests_used}")
    if not args.no_alerts and report.alerts and report.notifier:
        report.notifier.send(report.alerts)
    return 0 if report.itineraries else 1


def _cmd_collect(config: Config, args: argparse.Namespace) -> int:
    policy = CollectorPolicy.from_config(config)
    db_path = collector_db_path(config)

    if args.status:
        if not db_path.exists():
            print(f"Склад ещё не создан: {db_path}")
            return 1
        with CollectorStore(db_path) as store:
            _print_store_state(db_path, store.stats(fresh_minutes=policy.fresh_minutes), policy)
        return 0

    report = collect_offers(config, prune=not args.keep_stale)
    print(f"Запросов по плечам: {len(report.queries)}, израсходовано запросов: "
          f"{report.requests_used}")
    print(render_provider_summary(report.provider_results))
    print(f"\nЗаписано офферов: {report.stored}"
          + (f", вычищено устаревших: {report.pruned}" if report.pruned else ""))
    if report.stats:
        _print_store_state(report.db_path, report.stats, policy)
    return 0 if report.stored else 1


def _print_store_state(db_path: Path, stats: CollectorStats, policy: CollectorPolicy) -> None:
    print(f"\nСклад: {db_path}")
    print(f"  офферов всего: {stats.total}, свежих (до {policy.fresh_minutes} мин): {stats.fresh}")
    if stats.newest_at:
        print(f"  собрано: с {stats.oldest_at[:16]} по {stats.newest_at[:16]}")
    for source, count in stats.by_source.items():
        print(f"  {source}: {count}")


def _cmd_routes(config: Config, args: argparse.Namespace) -> int:
    paths = enumerate_paths(config)
    print(f"Кандидатов-цепочек: {len(paths)} (max_legs={config.search.max_legs}, "
          f"detour_factor={config.search.detour_factor})\n")
    for path in paths[: args.limit]:
        modes = " | ".join(
            f"{a}→{b}: " + "/".join(
                m.value
                for m in edge_modes(
                    a, b, allow_tour=(i == 0), allow_ground=config.search.include_ground
                )
            )
            for i, (a, b) in enumerate(path.legs)
        )
        print(f"  {str(path):<34} крюк ×{detour_ratio(list(path.nodes)):.2f}  {modes}")
    if args.queries:
        queries = leg_queries(paths, config)
        print(f"\nПлан запросов: {len(queries)}")
        for query in queries:
            print(
                f"  {query.origin}→{query.destination} "
                f"{query.date_from}..{query.date_to} "
                f"[{'/'.join(m.value for m in query.modes)}] "
                f"~{distance_km(query.origin, query.destination):.0f} км"
            )
    return 0


def _cmd_sources(config: Config) -> int:
    _http, _fx, providers = build_runtime(config)
    print(render_source_catalog(config, provider_status(providers)))
    if not providers:
        print("\nНи один провайдер не включён: включите нужные в секции providers конфига.")
    return 0


def _cmd_history(config: Config, args: argparse.Namespace) -> int:
    db_path = config.resolve_path(config.runtime.db_path)
    if not Path(db_path).exists():
        print("История пуста: ещё не было ни одного прогона.")
        return 1
    with Store(db_path) as store:
        if args.best:
            rows = store.cheapest_ever(limit=args.limit)
            print(f"{'Маршрут':<30}{'Кл':>3}{'Рекорд, ₽':>12}{'Набл.':>8}  Последнее наблюдение")
            for row in rows:
                print(
                    f"{row['route_key']:<30}{row['chain_class']:>3}"
                    f"{row['best_rub']:>12.0f}{row['observations']:>8}  {row['last_seen'][:16]}"
                )
            return 0
        rows = store.history(args.route, limit=args.limit)
        print(f"{'Когда':<18}{'Маршрут':<30}{'Кл':>3}{'Билеты':>10}{'Итого':>10}{'В пути':>10}")
        for row in rows:
            print(
                f"{row['observed_at'][:16]:<18}{row['route_key']:<30}{row['chain_class']:>3}"
                f"{row['tickets_rub']:>10.0f}{row['out_of_pocket_rub']:>10.0f}"
                f"{fmt_duration(row['duration_min']):>10}"
            )
    return 0


def _cmd_places() -> int:
    print(f"{'Код':<6}{'Город':<28}{'Стр':<5}{'Роль':<9}Почему в списке")
    for place in PLACES.values():
        print(f"{place.code:<6}{place.name:<28}{place.country:<5}{place.role:<9}{place.rationale}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

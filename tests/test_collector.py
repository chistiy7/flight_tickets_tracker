"""Схема «парсер собирает — трекер опрашивает»: склад офферов и его провайдер."""

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tbs_tracker.cli import main
from tbs_tracker.collector import (
    CollectorPolicy,
    CollectorStore,
    collector_db_path,
    offer_key,
    offer_to_leg,
)
from tbs_tracker.config import Config, ProviderConfig
from tbs_tracker.fx import CurrencyConverter
from tbs_tracker.httpclient import RequestBudget
from tbs_tracker.models import LegQuery, LinkKind, Mode, PriceKind
from tbs_tracker.pipeline import collect_offers, run_tracker
from tbs_tracker.providers.collector import CollectorProvider

from conftest import TZ_MOW, make_leg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def demo_config(tmp_path: Path) -> Config:
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "fixtures").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "demo.yaml", tmp_path / "config" / "demo.yaml")
    shutil.copy(
        ROOT / "data" / "fixtures" / "demo_offers.json",
        tmp_path / "data" / "fixtures" / "demo_offers.json",
    )
    return Config.load(tmp_path / "config" / "demo.yaml")


@pytest.fixture
def offline_config(demo_config: Config) -> Config:
    """Тот же проект, но живых источников нет — только склад парсера.

    Конфиг перечитывается заново, чтобы прогон по складу не делил объект с
    прогоном по источникам.
    """
    config = Config.load(demo_config.base_dir / "config" / "demo.yaml")
    config.providers["fixtures"].enabled = False
    config.providers["collector"] = ProviderConfig(
        name="collector", enabled=True, options={"db_path": "data/collector.sqlite3"}
    )
    return config


def _store(config: Config) -> CollectorStore:
    return CollectorStore(collector_db_path(config))


def _provider(config: Config, options: dict, http=None) -> CollectorProvider:
    provider_config = ProviderConfig(name="collector", enabled=True, options=options)
    fx = CurrencyConverter(config.runtime.fx)
    return CollectorProvider(config, provider_config, http, fx)


def test_collected_offers_feed_a_run_without_live_sources(demo_config, offline_config):
    """Прогон целиком на складе даёт те же цепочки, что прогон по источникам."""
    live = run_tracker(demo_config, notify=False, persist=False)
    report = collect_offers(demo_config)
    assert report.stored
    assert report.db_path.exists()

    from_store = run_tracker(offline_config, notify=False, persist=False)

    assert {r.provider for r in from_store.provider_results} == {"collector"}
    assert from_store.requests_used == 0
    assert from_store.itineraries
    assert from_store.itineraries[0].path == live.itineraries[0].path
    assert from_store.itineraries[0].tickets_rub == pytest.approx(live.itineraries[0].tickets_rub)


def test_collect_does_not_query_its_own_store(demo_config, offline_config):
    """Иначе прочитанные цены записывались бы обратно и никогда не старели."""
    collect_offers(demo_config)
    report = collect_offers(offline_config)

    assert "collector" not in {r.provider for r in report.provider_results}
    assert report.stored == 0


def test_repeated_collect_updates_price_instead_of_duplicating(demo_config):
    depart = datetime(2026, 10, 6, 9, 0, tzinfo=TZ_MOW)
    first = make_leg("MOW", "TBS", price=14000, depart=depart, carrier="A4", source="parser:azimuth")
    first.flight_number = "A4451"
    cheaper = make_leg("MOW", "TBS", price=11500, depart=depart, carrier="A4", source="parser:azimuth")
    cheaper.flight_number = "A4451"
    assert offer_key(first) == offer_key(cheaper)

    with _store(demo_config) as store:
        store.put([first])
        store.put([cheaper])
        rows = store.select("MOW", "TBS", date(2026, 10, 5), date(2026, 10, 9))

    assert len(rows) == 1
    assert rows[0]["price_rub"] == 11500


def test_fresh_offer_stays_live_and_stale_one_becomes_observation():
    row = {
        "origin": "MOW",
        "destination": "TBS",
        "mode": "air",
        "price_rub": 12000,
        "source": "parser:azimuth",
        "price_kind": "live",
        "depart": "2026-10-06T09:00:00+03:00",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    assert offer_to_leg(row, fresh_minutes=120).price_kind == PriceKind.LIVE

    row["collected_at"] = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    stale = offer_to_leg(row, fresh_minutes=120)
    assert stale.price_kind == PriceKind.CACHED
    assert "собрано 9 ч назад" in (stale.notes or "")


def test_estimate_never_pretends_to_be_live():
    """Оценка наземного плеча не становится живой ценой от того, что свежая."""
    row = {
        "origin": "OGZ",
        "destination": "TBS",
        "mode": "bus",
        "price_rub": 1500,
        "source": "parser:avtovokzal",
        "price_kind": "estimate",
        "flexible": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    assert offer_to_leg(row).price_kind == PriceKind.ESTIMATE


def test_offers_past_max_age_are_not_served(demo_config):
    old = make_leg(
        "MOW", "TBS", price=9000, depart=datetime(2026, 10, 6, 9, 0, tzinfo=TZ_MOW),
        source="parser:azimuth",
    )
    with _store(demo_config) as store:
        store.put([old], collected_at=datetime.now(timezone.utc) - timedelta(days=3))

    provider = _provider(demo_config, {"max_age_minutes": 1440})
    result = provider.safe_fetch(LegQuery("MOW", "TBS", date(2026, 10, 5), date(2026, 10, 9)))
    assert result.legs == []

    with _store(demo_config) as store:
        assert store.prune(1440) == 1


def test_provider_asks_to_run_collect_when_store_is_missing(demo_config):
    provider = _provider(demo_config, {"db_path": "data/absent.sqlite3"})
    result = provider.safe_fetch(LegQuery("MOW", "TBS", date(2026, 10, 5), date(2026, 10, 9)))
    assert result.skipped_reason and "tbs-tracker collect" in result.skipped_reason


def test_http_parser_is_queried_without_spending_api_budget(demo_config):
    """Свой парсер за HTTP — не внешний API, квоту он расходовать не должен."""

    class FakeHttp:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.budget = RequestBudget(limit=0)

        def get_json(self, url, *, params=None, headers=None, source="http",
                     use_cache=True, charge_budget=True):
            self.calls.append({"url": url, "params": params, "charge_budget": charge_budget})
            if charge_budget:
                self.budget.charge(source)
            return {
                "offers": [
                    {
                        "mode": "air",
                        "price": 129.0,
                        "currency": "EUR",
                        "carrier": "PC",
                        "flight_number": "PC1234",
                        "depart": "2026-10-06T07:20:00+03:00",
                        "duration_min": 190,
                        "deep_link": "https://www.flypgs.com/booking?x=1",
                        "link_kind": "booking",
                        "parser": "flypgs",
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                    },
                    # Не тот режим передвижения — не наше плечо.
                    {"mode": "bus", "price_rub": 800, "collected_at": "2026-10-06T00:00:00+00:00"},
                ]
            }

    http = FakeHttp()
    provider = _provider(demo_config, {"url": "http://127.0.0.1:8899/prices"}, http)
    result = provider.safe_fetch(LegQuery("MOW", "TBS", date(2026, 10, 5), date(2026, 10, 9)))

    assert http.calls[0]["charge_budget"] is False
    assert http.budget.used == 0
    assert http.calls[0]["params"]["date_from"] == "2026-10-05"

    assert len(result.legs) == 1
    leg = result.legs[0]
    rate = demo_config.runtime.fx.static_rates["EUR"]
    assert leg.price_rub == pytest.approx(129.0 * rate)
    assert leg.source == "collector:flypgs"
    assert leg.price_kind == PriceKind.LIVE
    assert leg.link_kind == LinkKind.BOOKING
    assert leg.where_to_buy().startswith("купить: https://www.flypgs.com/booking")


def test_policy_comes_from_config(demo_config):
    demo_config.providers["collector"] = ProviderConfig(
        name="collector", enabled=True, options={"fresh_minutes": 30, "max_age_minutes": 720}
    )
    policy = CollectorPolicy.from_config(demo_config)
    assert (policy.fresh_minutes, policy.max_age_minutes) == (30, 720)


def test_cli_collect_then_run_on_store(demo_config, capsys, monkeypatch):
    monkeypatch.chdir(demo_config.base_dir)
    config_path = demo_config.base_dir / "config" / "demo.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["providers"]["collector"] = {"enabled": True, "db_path": "data/collector.sqlite3"}
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    assert main(["-c", "config/demo.yaml", "collect"]) == 0
    out = capsys.readouterr().out
    assert "Записано офферов:" in out
    assert "Склад:" in out

    assert main(["-c", "config/demo.yaml", "collect", "--status"]) == 0
    assert "офферов всего:" in capsys.readouterr().out

    assert main(["-c", "config/demo.yaml", "run", "--no-alerts", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "collector:" in out
    assert "AER → TBS" in out


def test_store_stats_count_fresh_offers(demo_config):
    fresh = make_leg("AER", "TBS", price=8000, depart=datetime(2026, 10, 6, 8, 0, tzinfo=TZ_MOW),
                     source="parser:azimuth")
    old = make_leg("MOW", "TBS", price=9000, depart=datetime(2026, 10, 7, 8, 0, tzinfo=TZ_MOW),
                   source="parser:tutu")
    with _store(demo_config) as store:
        store.put([fresh])
        store.put([old], collected_at=datetime.now(timezone.utc) - timedelta(hours=5))
        stats = store.stats(fresh_minutes=120)

    assert stats.total == 2
    assert stats.fresh == 1
    assert stats.by_source == {"parser:azimuth": 1, "parser:tutu": 1}


def test_modes_filter_applies_to_store_queries(demo_config):
    bus = make_leg("OGZ", "TBS", price=1500, mode=Mode.BUS, flexible=True,
                   depart=datetime(2026, 10, 6, 8, 0, tzinfo=TZ_MOW), source="parser:avtovokzal")
    with _store(demo_config) as store:
        store.put([bus])
        air_only = store.select("OGZ", "TBS", date(2026, 10, 5), date(2026, 10, 9),
                                modes=[Mode.AIR])
        with_bus = store.select("OGZ", "TBS", date(2026, 10, 5), date(2026, 10, 9),
                                modes=[Mode.AIR, Mode.BUS])

    assert air_only == []
    assert len(with_bus) == 1

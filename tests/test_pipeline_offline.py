"""Сквозной прогон на фикстурах: без сети, детерминированно."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tbs_tracker.cli import main
from tbs_tracker.config import Config
from tbs_tracker.normalize import dedupe, group_by_edge
from tbs_tracker.models import Mode, PriceKind
from tbs_tracker.pipeline import run_tracker
from tbs_tracker.store import Store

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


def test_offline_run_builds_chains_and_history(demo_config):
    report = run_tracker(demo_config, notify=False)

    assert report.itineraries, report.stats.filtered_out
    assert report.requests_used == 0  # фикстуры и справочник наземных плеч не ходят в сеть

    paths = {" → ".join(it.path) for it in report.itineraries}
    assert "MOW → TBS" in paths
    assert "MOW → OGZ → TBS" in paths

    # Абсолютный минимум — наземное плечо из Владикавказа (он тоже в origins).
    assert report.itineraries[0].path == ["OGZ", "TBS"]

    # Из Москвы наземный коридор через Владикавказ дешевле прямого рейса.
    ground_chain = _cheapest(report.itineraries, ["MOW", "OGZ", "TBS"])
    direct = _cheapest(report.itineraries, ["MOW", "TBS"], mode=Mode.AIR)
    assert ground_chain.chain_class == "D"
    assert ground_chain.cost.out_of_pocket_rub < direct.cost.out_of_pocket_rub

    with Store(demo_config.resolve_path(demo_config.runtime.db_path)) as store:
        assert store.history(limit=5)
        assert store.cheapest_ever(limit=3)


def test_hot_tour_can_beat_dry_ticket(demo_config):
    """Горячий тур обгоняет сухой билет из Москвы просто по цене пакета."""
    report = run_tracker(demo_config, notify=False)
    from_moscow = [it for it in report.itineraries if it.origin == "MOW"]
    assert from_moscow[0].chain_class == "E"
    direct = _cheapest(report.itineraries, ["MOW", "TBS"], mode=Mode.AIR)
    assert from_moscow[0].cost.out_of_pocket_rub < direct.cost.out_of_pocket_rub


def _cheapest(itineraries, path, mode: Mode | None = None):
    matching = [
        it for it in itineraries
        if it.path == path and (mode is None or it.legs[0].mode == mode)
    ]
    assert matching, f"нет вариантов для {path}"
    return min(matching, key=lambda it: it.cost.out_of_pocket_rub)


def test_offline_run_covers_several_chain_classes(demo_config):
    report = run_tracker(demo_config, notify=False)
    classes = {it.chain_class for it in report.itineraries}
    assert {"A", "D", "E"} <= classes


def test_offline_run_converts_currency_of_foreign_leg(demo_config):
    report = run_tracker(demo_config, notify=False)
    foreign = [leg for leg in report.legs if leg.currency == "EUR"]
    assert foreign, "в фикстурах есть плечо в евро"
    leg = foreign[0]
    rate = demo_config.runtime.fx.static_rates["EUR"]
    assert leg.price_rub == pytest.approx(leg.price_original * rate)


def test_tour_leg_is_classified_as_package(demo_config):
    report = run_tracker(demo_config, notify=False)
    tours = [it for it in report.itineraries if it.chain_class == "E"]
    assert tours
    assert all(tours[0].legs[0].mode == Mode.TOUR for _ in [0])
    assert "package_tour" in tours[0].flags


def test_ground_provider_supplies_border_leg(demo_config):
    report = run_tracker(demo_config, notify=False)
    ground_legs = [leg for leg in report.legs if leg.mode.is_ground]
    assert ground_legs
    assert all(leg.price_kind == PriceKind.ESTIMATE for leg in ground_legs)
    assert any(leg.origin == "OGZ" and leg.destination == "TBS" for leg in ground_legs)


def test_dedupe_prefers_live_price_at_equal_cost(demo_config):
    report = run_tracker(demo_config, notify=False)
    grouped = group_by_edge(report.legs, demo_config)
    assert grouped[("MOW", "TBS")]
    duplicated = report.legs + report.legs
    assert len(dedupe(duplicated)) == len(dedupe(report.legs))


def test_cli_run_and_routes_offline(demo_config, capsys, monkeypatch):
    monkeypatch.chdir(demo_config.base_dir)
    assert main(["-c", "config/demo.yaml", "routes", "--queries"]) == 0
    out = capsys.readouterr().out
    assert "MOW → OGZ → TBS" in out
    assert "План запросов" in out

    assert main(["-c", "config/demo.yaml", "run", "--no-alerts", "--limit", "8"]) == 0
    out = capsys.readouterr().out
    assert "Источники:" in out
    assert "MOW → OGZ → TBS" in out

    # Без ссылки вариант бесполезен: по нему нечего покупать.
    assert "искать: https://www.aviasales.ru/search/" in out
    assert "купить: билет у водителя или в кассе на месте" in out

    assert main(["-c", "config/demo.yaml", "history", "--best"]) == 0
    assert "MOW>OGZ>TBS" in capsys.readouterr().out

    assert main(["-c", "config/demo.yaml", "sources"]) == 0
    assert "fixtures" in capsys.readouterr().out

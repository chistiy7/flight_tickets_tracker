from __future__ import annotations

from datetime import datetime, timedelta

from conftest import TZ_MOW, make_leg
from tbs_tracker.alerts import evaluate
from tbs_tracker.models import Itinerary, LinkKind, Mode
from tbs_tracker.routing.search import compute_cost
from tbs_tracker.store import Store


def _itinerary(price: float, config) -> Itinerary:
    depart = datetime(2026, 10, 5, 9, 20, tzinfo=TZ_MOW)
    legs = [
        make_leg("MOW", "OGZ", price=price, depart=depart, duration_min=165),
        make_leg("OGZ", "TBS", price=1500, mode=Mode.BUS, baggage=True,
                 depart=depart + timedelta(hours=4), duration_min=360),
    ]
    itinerary = Itinerary(legs=legs, chain_class="D")
    itinerary.cost = compute_cost(legs, config)
    return itinerary


def test_store_records_and_computes_baseline(base_config, tmp_path):
    with Store(tmp_path / "hist.sqlite3") as store:
        for price in (12000, 11000, 10000, 9000, 8000, 7000):
            run_id = store.start_run(["fixtures"])
            store.record_itineraries(run_id, [_itinerary(price, base_config)])
            store.finish_run(run_id, requests_used=0, itineraries=1)

        baseline = store.baseline("MOW>OGZ>TBS", percentile=10)
        assert baseline.observations == 6
        assert baseline.min_rub is not None and baseline.percentile_rub is not None
        assert baseline.min_rub <= baseline.percentile_rub <= baseline.median_rub


def test_alert_fires_on_target_price_then_cools_down(base_config, tmp_path):
    base_config.alerts.target_price_rub = 12000
    base_config.alerts.cooldown_hours = 12
    with Store(tmp_path / "alerts.sqlite3") as store:
        run_id = store.start_run(["fixtures"])
        itinerary = _itinerary(6500, base_config)
        store.record_itineraries(run_id, [itinerary])

        first = evaluate([itinerary], store, base_config, run_id=run_id)
        assert [a.kind for a in first] == ["target"]
        assert "Москва → Владикавказ → Тбилиси" in first[0].message

        # Повторный прогон в пределах cooldown не должен дублировать алерт.
        again = evaluate([itinerary], store, base_config, run_id=run_id)
        assert again == []


def test_alert_fires_on_percentile_drop(base_config, tmp_path):
    base_config.alerts.target_price_rub = None
    base_config.alerts.min_observations = 3
    with Store(tmp_path / "drop.sqlite3") as store:
        for price in (12000, 12500, 13000, 12800):
            run_id = store.start_run(["fixtures"])
            store.record_itineraries(run_id, [_itinerary(price, base_config)])
            store.finish_run(run_id, requests_used=0, itineraries=1)

        cheap_run = store.start_run(["fixtures"])
        cheap = _itinerary(6000, base_config)
        store.record_itineraries(cheap_run, [cheap])
        alerts = evaluate([cheap], store, base_config, run_id=cheap_run)
        kinds = {a.kind for a in alerts}
        assert "percentile" in kinds
        assert "drop" in kinds


def test_alert_carries_purchase_links_for_every_leg(base_config, tmp_path):
    """Алерт без ссылок бесполезен: пока ищешь, где купить, цена уходит."""
    base_config.alerts.target_price_rub = 20000
    with Store(tmp_path / "links.sqlite3") as store:
        run_id = store.start_run(["fixtures"])
        itinerary = _itinerary(7000, base_config)
        itinerary.legs[0].deep_link = "https://azimuth.aero/"
        itinerary.legs[0].link_kind = LinkKind.BOOKING
        itinerary.legs[1].booking_ref = "билет у водителя"

        message = evaluate([itinerary], store, base_config, run_id=run_id)[0].message

        assert "купить: https://azimuth.aero/" in message
        assert "купить: билет у водителя" in message


def test_alert_marks_non_live_prices(base_config, tmp_path):
    base_config.alerts.target_price_rub = 20000
    with Store(tmp_path / "kind.sqlite3") as store:
        run_id = store.start_run(["fixtures"])
        itinerary = _itinerary(7000, base_config)
        alerts = evaluate([itinerary], store, base_config, run_id=run_id)
        assert alerts
        assert "подтвердите на сайте перевозчика" in alerts[0].message

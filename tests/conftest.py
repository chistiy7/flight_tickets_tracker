from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tbs_tracker.config import Config  # noqa: E402
from tbs_tracker.models import Leg, Mode, PaymentChannel, PriceKind  # noqa: E402

TZ_MOW = timezone(timedelta(hours=3))
TZ_TBS = timezone(timedelta(hours=4))


@pytest.fixture
def base_config(tmp_path: Path) -> Config:
    return Config.from_dict(
        {
            "search": {
                "origins": ["MOW", "LED", "AER", "MRV", "OGZ"],
                "destinations": ["TBS"],
                "hubs": ["EVN", "IST"],
                "entry_points": ["TBS", "KUT"],
                "max_legs": 3,
                "detour_factor": 2.4,
                "windows": [{"date_from": "2026-10-05", "date_to": "2026-10-09"}],
            },
            "runtime": {"db_path": str(tmp_path / "test.sqlite3"), "fx": {"mode": "static"}},
            "providers": {},
        },
        base_dir=tmp_path,
        today=date(2026, 9, 7),
    )


def make_leg(
    origin: str,
    destination: str,
    *,
    price: float,
    depart: datetime | None = None,
    arrive: datetime | None = None,
    mode: Mode = Mode.AIR,
    carrier: str = "XX",
    flexible: bool = False,
    duration_min: int | None = None,
    baggage: bool = False,
    source: str = "test",
    price_kind: PriceKind = PriceKind.CACHED,
    payment: PaymentChannel = PaymentChannel.RU_CARD,
) -> Leg:
    return Leg(
        origin=origin,
        destination=destination,
        mode=mode,
        price_rub=price,
        source=source,
        depart=depart,
        arrive=arrive,
        duration_min=duration_min,
        carrier=carrier,
        flexible=flexible,
        baggage_included=baggage,
        price_kind=price_kind,
        payment_channel=payment,
    )

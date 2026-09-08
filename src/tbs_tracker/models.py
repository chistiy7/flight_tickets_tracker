"""Доменные модели трекера: плечи маршрута, офферы, цепочки."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any


class Mode(str, Enum):
    AIR = "air"
    BUS = "bus"
    TRAIN = "train"
    TAXI = "taxi"
    TOUR = "tour"

    @property
    def is_ground(self) -> bool:
        return self in (Mode.BUS, Mode.TRAIN, Mode.TAXI)


class PriceKind(str, Enum):
    """Насколько цене можно верить как «цене к покупке»."""

    LIVE = "live"
    CACHED = "cached"
    ESTIMATE = "estimate"


class PaymentChannel(str, Enum):
    RU_CARD = "ru_card"
    FOREIGN_CARD = "foreign_card"
    CASH = "cash"


@dataclass(frozen=True)
class LegQuery:
    """Запрос цен на одно плечо в окне дат."""

    origin: str
    destination: str
    date_from: date
    date_to: date
    modes: tuple[Mode, ...] = (Mode.AIR,)
    passengers: int = 1

    @property
    def key(self) -> str:
        modes = "+".join(m.value for m in self.modes)
        return f"{self.origin}-{self.destination}:{self.date_from}:{self.date_to}:{modes}:{self.passengers}"

    def dates(self) -> list[date]:
        span = (self.date_to - self.date_from).days
        return [self.date_from + timedelta(days=i) for i in range(span + 1)]


@dataclass
class Leg:
    """Одно плечо маршрута с ценой от конкретного источника."""

    origin: str
    destination: str
    mode: Mode
    price_rub: float
    source: str
    depart: datetime | None = None
    arrive: datetime | None = None
    duration_min: int | None = None
    carrier: str | None = None
    flight_number: str | None = None
    price_original: float | None = None
    currency: str = "RUB"
    price_kind: PriceKind = PriceKind.CACHED
    payment_channel: PaymentChannel = PaymentChannel.RU_CARD
    baggage_included: bool = False
    transfers: int = 0
    deep_link: str | None = None
    observed_at: datetime | None = None
    # Плечо без жёсткого расписания (маршрутки «по заполнению», такси).
    flexible: bool = False
    # Что ещё входит в цену, кроме самой перевозки (актуально для пакетных туров).
    nights_included: int = 0
    accommodation_credit_rub: float = 0.0
    return_flight_included: bool = False
    notes: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duration_min is None and self.depart and self.arrive:
            self.duration_min = int((self.arrive - self.depart).total_seconds() // 60)
        if self.arrive is None and self.depart and self.duration_min:
            self.arrive = self.depart + timedelta(minutes=self.duration_min)
        if self.price_original is None:
            self.price_original = self.price_rub

    @property
    def leg_id(self) -> str:
        parts = [
            self.origin,
            self.destination,
            self.mode.value,
            self.carrier or "",
            self.flight_number or "",
            self.depart.isoformat() if self.depart else "",
            f"{self.price_rub:.0f}",
            self.source,
        ]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    @property
    def route_key(self) -> str:
        """Ключ маршрута без цены — по нему ведётся история наблюдений."""
        parts = [self.origin, self.destination, self.mode.value, self.carrier or "*"]
        return ":".join(parts)

    @property
    def depart_date(self) -> date | None:
        return self.depart.date() if self.depart else None

    def describe(self) -> str:
        if self.depart is None:
            when = "по заполнению"
        elif self.flexible:
            when = f"{self.depart.strftime('%d.%m')} (по заполнению)"
        else:
            when = self.depart.strftime("%d.%m %H:%M")
        who = self.carrier or self.mode.value
        if self.flight_number:
            who = f"{who} {self.flight_number}"
        return f"{self.origin}→{self.destination} {when} {who} {self.price_rub:.0f}₽"


@dataclass
class CostBreakdown:
    """Из чего складывается стоимость варианта.

    Разделение принципиальное: `out_of_pocket_rub` — это деньги, которые реально
    уходят из кошелька, и именно по ним сравниваются варианты. Включённое в тур
    проживание — не скидка на билет, а отдельная польза: она попадает в
    `accommodation_credit_rub` и влияет только на «полезную» стоимость.
    """

    tickets_rub: float = 0.0
    transfers_rub: float = 0.0
    baggage_rub: float = 0.0
    overnight_rub: float = 0.0
    risk_rub: float = 0.0
    accommodation_credit_rub: float = 0.0
    time_cost_rub: float = 0.0

    @property
    def out_of_pocket_rub(self) -> float:
        """Реальные деньги: билеты/пакет + трансферы + багаж + ночёвки в пути."""
        return (
            self.tickets_rub
            + self.transfers_rub
            + self.baggage_rub
            + self.overnight_rub
        )

    @property
    def applied_credit_rub(self) -> float:
        """Зачёт проживания не может превышать саму цену варианта."""
        return min(self.accommodation_credit_rub, self.out_of_pocket_rub)

    @property
    def value_rub(self) -> float:
        """Цена за вычетом того, что уже включено в пакет (отель)."""
        return self.out_of_pocket_rub - self.applied_credit_rub

    @property
    def generalized_rub(self) -> float:
        """Стоимость с учётом риска и цены времени — по ней ранжируем."""
        return self.value_rub + self.risk_rub + self.time_cost_rub

    def as_dict(self) -> dict[str, float]:
        return {
            "tickets_rub": round(self.tickets_rub, 2),
            "transfers_rub": round(self.transfers_rub, 2),
            "baggage_rub": round(self.baggage_rub, 2),
            "overnight_rub": round(self.overnight_rub, 2),
            "risk_rub": round(self.risk_rub, 2),
            "accommodation_credit_rub": round(self.applied_credit_rub, 2),
            "time_cost_rub": round(self.time_cost_rub, 2),
            "out_of_pocket_rub": round(self.out_of_pocket_rub, 2),
            "value_rub": round(self.value_rub, 2),
            "generalized_rub": round(self.generalized_rub, 2),
        }


@dataclass
class Itinerary:
    """Логистическая цепочка: одно или несколько плеч из РФ в Тбилиси."""

    legs: list[Leg]
    chain_class: str = "?"
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    flags: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return self.legs[0].origin

    @property
    def destination(self) -> str:
        return self.legs[-1].destination

    @property
    def path(self) -> list[str]:
        return [self.legs[0].origin] + [leg.destination for leg in self.legs]

    @property
    def depart(self) -> datetime | None:
        return self.legs[0].depart

    @property
    def arrive(self) -> datetime | None:
        return self.legs[-1].arrive

    @property
    def total_duration_min(self) -> int:
        if self.depart and self.arrive:
            return int((self.arrive - self.depart).total_seconds() // 60)
        return sum(leg.duration_min or 0 for leg in self.legs)

    @property
    def tickets_rub(self) -> float:
        return sum(leg.price_rub for leg in self.legs)

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for leg in self.legs:
            if leg.source not in seen:
                seen.append(leg.source)
        return seen

    @property
    def price_kind(self) -> PriceKind:
        """Цепочка настолько «живая», насколько ненадёжно её худшее плечо."""
        order = [PriceKind.LIVE, PriceKind.CACHED, PriceKind.ESTIMATE]
        return max((leg.price_kind for leg in self.legs), key=order.index)

    @property
    def itinerary_id(self) -> str:
        return hashlib.sha1("|".join(leg.leg_id for leg in self.legs).encode()).hexdigest()[:16]

    @property
    def route_key(self) -> str:
        return ">".join(self.path)

    def layovers_min(self) -> list[int]:
        gaps: list[int] = []
        for prev, nxt in zip(self.legs, self.legs[1:]):
            if prev.arrive and nxt.depart:
                gaps.append(int((nxt.depart - prev.arrive).total_seconds() // 60))
        return gaps

    def describe(self) -> str:
        return " + ".join(leg.describe() for leg in self.legs)


@dataclass
class ProviderResult:
    """Что вернул один провайдер по одному запросу плеча."""

    provider: str
    query: LegQuery | None
    legs: list[Leg] = field(default_factory=list)
    requests_used: int = 0
    skipped_reason: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped_reason is None


@dataclass
class TourOffer:
    """Пакетный тур (горячий тур), приведённый к сравнимому виду."""

    origin: str
    destination: str
    price_rub: float
    nights: int
    depart_date: date
    source: str
    hotel: str | None = None
    hotel_stars: int | None = None
    operator: str | None = None
    meal: str | None = None
    price_old_rub: float | None = None
    deep_link: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def discount_pct(self) -> float | None:
        """Насколько пакет уценён относительно исходной цены — признак «горящего»."""
        if not self.price_old_rub or self.price_old_rub <= 0:
            return None
        return (self.price_old_rub - self.price_rub) / self.price_old_rub * 100

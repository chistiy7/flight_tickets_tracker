"""Правила стыковок и допустимости цепочек.

Именно эти правила отделяют реальный маршрут от арифметической фантазии: два
отдельных билета с часовой стыковкой в Стамбуле складываются в самую дешёвую
сумму и почти гарантированно ломаются на практике.
"""

from __future__ import annotations

from datetime import timedelta

from ..config import ConnectionsConfig
from ..geo import crosses_border, place, requires_airport_change
from ..models import Leg, Mode

#: Плечи считаются «одним билетом», если их продал один и тот же источник
#: одним оффером. В нашей модели каждое плечо — отдельная покупка, поэтому
#: любая склейка двух плеч трактуется как self-transfer.
SELF_TRANSFER_NOTE = "два отдельных билета: срыв первого плеча не покрывается перевозчиком"


def min_connection_min(prev: Leg, nxt: Leg, cfg: ConnectionsConfig) -> int:
    """Минимальная стыковка между двумя плечами в минутах."""
    # Наземное плечо после/перед авиа: нужно доехать до автовокзала.
    if prev.mode.is_ground or nxt.mode.is_ground:
        base = cfg.min_ground_min
        if crosses_border(nxt.origin, nxt.destination) and (
            nxt.mode.is_ground or prev.mode.is_ground
        ):
            base = max(base, cfg.min_border_min)
        return base

    base = cfg.min_self_transfer_min
    prev_airports = place(prev.destination).airports
    next_airports = place(nxt.origin).airports
    if any(
        requires_airport_change(a, b)
        for a in prev_airports
        for b in next_airports
    ):
        base = max(base, cfg.min_airport_change_min)
    return base


def can_connect(prev: Leg, nxt: Leg, cfg: ConnectionsConfig) -> bool:
    if prev.destination != nxt.origin:
        return False
    if prev.arrive is None or nxt.depart is None:
        return False

    required = min_connection_min(prev, nxt, cfg)
    if nxt.flexible:
        # Маршрутки «по заполнению»: важно только, что после прилёта есть время
        # добраться и уехать в тот же/следующий день.
        earliest = prev.arrive + timedelta(minutes=required)
        latest = prev.arrive + timedelta(minutes=cfg.max_layover_min)
        return earliest.date() <= latest.date()

    gap_min = (nxt.depart - prev.arrive).total_seconds() / 60
    return required <= gap_min <= cfg.max_layover_min


def effective_departure(prev: Leg, nxt: Leg, cfg: ConnectionsConfig) -> Leg:
    """Для гибкого плеча зафиксировать реальное время отправления после стыковки."""
    if not nxt.flexible or prev.arrive is None:
        return nxt
    required = min_connection_min(prev, nxt, cfg)
    depart = prev.arrive + timedelta(minutes=required)
    fixed = Leg(**{**nxt.__dict__, "depart": depart, "arrive": None, "flexible": True})
    return fixed


def classify(legs: list[Leg]) -> str:
    """Класс цепочки согласно docs/routing.md (A..E)."""
    if len(legs) == 1:
        leg = legs[0]
        if leg.mode == Mode.TOUR:
            return "E"
        if leg.mode.is_ground:
            return "D"
        if leg.transfers > 0:
            return "B"
        return "A"
    if any(leg.mode.is_ground for leg in legs):
        return "D"
    return "C"


def flags_for(legs: list[Leg], layovers_min: list[int], cfg: ConnectionsConfig) -> list[str]:
    flags: list[str] = []
    if len(legs) > 1:
        flags.append("self_transfer")
    if any(leg.mode.is_ground for leg in legs):
        flags.append("ground_leg")
    if any(crosses_border(leg.origin, leg.destination) and leg.mode.is_ground for leg in legs):
        flags.append("land_border")
    if any(gap >= cfg.overnight_threshold_min for gap in layovers_min):
        flags.append("overnight_layover")
    if any(leg.payment_channel.value == "foreign_card" for leg in legs):
        flags.append("requires_foreign_card")
    if any(leg.mode == Mode.TOUR for leg in legs):
        flags.append("package_tour")
    if not all(leg.baggage_included for leg in legs):
        flags.append("baggage_extra")
    countries = {place(leg.destination).country for leg in legs[:-1]}
    if countries - {"RU", "GE"}:
        flags.append("transit_visa_check")
    return flags

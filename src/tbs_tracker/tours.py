"""Как сравнивать пакетный тур с авиабилетом.

Правило простое и главное: сравниваем по фактической цене. Если тур стоит 20 000 ₽,
а билет в одну сторону — 25 000 ₽, то тур выгоднее, и никакие поправки на «а отель мне
не нужен» этого не меняют: из кошелька уходит меньше денег, а перелёт в туре ещё и
туда-обратно.

Включённое проживание — это не скидка на билет, а дополнительная польза. Поэтому оно
не уменьшает цену тура, а учитывается отдельным зачётом, и только если проживание
действительно нужно: `costs.trip_nights` задаёт, сколько ночей в отеле вы бы всё равно
оплачивали. По умолчанию 0 — значит сравнение идёт по чистой цене.
"""

from __future__ import annotations

from .config import CostsConfig


def accommodation_credit_rub(nights_included: int, costs: CostsConfig) -> float:
    """Сколько из включённого проживания можно зачесть в пользу тура."""
    if nights_included <= 0 or costs.trip_nights <= 0:
        return 0.0
    useful_nights = min(nights_included, costs.trip_nights)
    return useful_nights * costs.tour_accommodation_rub_per_night


def describe_package(
    *,
    price_rub: float,
    nights: int,
    credit_rub: float,
    hotel: str | None = None,
    price_old_rub: float | None = None,
) -> str:
    parts = [f"пакет {price_rub:.0f}₽ за {nights} н."]
    if hotel:
        parts.append(hotel)
    if price_old_rub and price_old_rub > price_rub:
        discount = (price_old_rub - price_rub) / price_old_rub * 100
        parts.append(f"уценён на {discount:.0f}% с {price_old_rub:.0f}₽")
    parts.append("включает перелёт туда-обратно и проживание")
    if credit_rub > 0:
        parts.append(f"зачёт проживания {credit_rub:.0f}₽")
    else:
        parts.append("сравнение по фактической цене, без зачёта проживания")
    return "; ".join(parts)

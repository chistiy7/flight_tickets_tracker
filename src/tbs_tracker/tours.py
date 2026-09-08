"""Как сравнивать пакетный тур с авиабилетом.

Базовое правило — сравнение по фактической цене, и оно сознательно консервативное:
за цену пакета трекер по умолчанию считает, что вы получаете только нужный билет в одну
сторону. Всё остальное, что входит в тур (проживание, обратный перелёт, иногда питание и
трансфер), в деньгах не учитывается вообще. Поэтому если тур за 20 000 ₽ обогнал билет за
25 000 ₽, то он обогнал его наверняка — выгода не может оказаться нарисованной.

Обратная сторона: такой подход недооценивает пакет, когда включённые части вам реально
нужны. Поэтому есть два явных переключателя, и оба по умолчанию выключены:

- `costs.trip_nights` — сколько ночей в отеле вы бы всё равно оплачивали;
- `costs.return_flight_value_rub` — во сколько вы оцениваете обратный билет.

Зачёт не уменьшает сумму к оплате: он влияет только на «полезную» стоимость (`value_rub`),
и в отчёте это отдельная колонка. Так видно и то, сколько денег уходит, и то, сколько
вы получаете за эти деньги.
"""

from __future__ import annotations

from .config import CostsConfig


def accommodation_credit_rub(nights_included: int, costs: CostsConfig) -> float:
    """Зачёт за проживание — только за нужные вам ночи, не за все включённые."""
    if nights_included <= 0 or costs.trip_nights <= 0:
        return 0.0
    useful_nights = min(nights_included, costs.trip_nights)
    return useful_nights * costs.tour_accommodation_rub_per_night


def return_flight_credit_rub(return_included: bool, costs: CostsConfig) -> float:
    """Зачёт за включённый обратный перелёт по вашей же оценке его стоимости."""
    if not return_included or costs.return_flight_value_rub <= 0:
        return 0.0
    return costs.return_flight_value_rub


def bundle_credit_rub(
    *, nights_included: int, return_included: bool, costs: CostsConfig
) -> float:
    """Сколько из включённого в пакет можно зачесть в его пользу."""
    return (
        accommodation_credit_rub(nights_included, costs)
        + return_flight_credit_rub(return_included, costs)
    )


def describe_package(
    *,
    price_rub: float,
    nights: int,
    credit_rub: float,
    return_included: bool = True,
    hotel: str | None = None,
    price_old_rub: float | None = None,
) -> str:
    parts = [f"пакет {price_rub:.0f}₽ за {nights} н."]
    if hotel:
        parts.append(hotel)
    if price_old_rub and price_old_rub > price_rub:
        discount = (price_old_rub - price_rub) / price_old_rub * 100
        parts.append(f"уценён на {discount:.0f}% с {price_old_rub:.0f}₽")
    included = ["проживание"]
    if return_included:
        included.insert(0, "перелёт туда-обратно")
    parts.append("включает " + " и ".join(included))
    if credit_rub > 0:
        parts.append(f"зачёт включённого {credit_rub:.0f}₽")
    else:
        parts.append("цена сравнивается как за билет в одну сторону, включённое не зачтено")
    return "; ".join(parts)

"""Как пакетный тур участвует в сравнении с билетами.

Правило одно: сравнивается тело тура. Цена пакета встаёт в общий ряд с ценами
билетов как есть — то, что внутри него едет ещё и отель с обратным перелётом, не
превращается ни в скидку, ни в надбавку. Тур за 20 000 ₽ выгоднее билета за
25 000 ₽ ровно потому, что из кошелька уходит меньше денег, и никакой оценки
«а сколько стоит включённое» для этого вывода не требуется.

Отсюда и отсутствие настроек: оценивать бонусы деньгами — значит подгонять
сравнение под догадки о том, нужны ли вам эти ночи и то ли это направление
обратно. Состав пакета остаётся в описании и во флагах (`package_tour`,
`hotel_included`, `return_included`): на цену он не влияет, но виден.
"""

from __future__ import annotations


def describe_package(
    *,
    price_rub: float,
    nights: int,
    hotel: str | None = None,
    price_old_rub: float | None = None,
    return_included: bool = True,
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
    parts.append("в сравнении участвует только цена пакета")
    return "; ".join(parts)

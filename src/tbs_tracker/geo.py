"""Справочник городов, хабов и погранпереходов для коридора РФ → Тбилиси.

Коды — городские IATA-коды (MOW, а не VKO/DME), потому что все источники цен
работают на уровне города; конкретный аэропорт важен только для расчёта стыковки
со сменой аэропорта.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt


@dataclass(frozen=True)
class Place:
    code: str
    name: str
    country: str
    lat: float
    lon: float
    tz_offset: int
    #: Роль в логистике: origin (вылет из РФ), hub (транзит), entry (вход в Грузию),
    #: border (наземный переход), target (цель).
    role: str = "hub"
    airports: tuple[str, ...] = ()
    #: Почему город в списке — попадает в отчёты, чтобы выбор был объясним.
    rationale: str = ""


PLACES: dict[str, Place] = {
    # --- Россия: опорные города вылета -------------------------------------
    "MOW": Place("MOW", "Москва", "RU", 55.75, 37.62, 3, "origin",
                 ("VKO", "DME", "SVO", "ZIA"),
                 "Прямые рейсы в Тбилиси (Азимут, Red Wings, Georgian Airways) + все хабы"),
    "LED": Place("LED", "Санкт-Петербург", "RU", 59.94, 30.31, 3, "origin", ("LED",),
                 "Дешёвые плечи в EVN/IST/MSQ, прямых в TBS нет"),
    "AER": Place("AER", "Сочи", "RU", 43.60, 39.73, 3, "origin", ("AER",),
                 "Прямой Red Wings в TBS; близко к наземному коридору"),
    "MRV": Place("MRV", "Минеральные Воды", "RU", 44.21, 43.13, 3, "origin", ("MRV",),
                 "Прямые Азимут/Georgian Airways по выходным; вход в наземный коридор"),
    "OGZ": Place("OGZ", "Владикавказ", "RU", 43.21, 44.61, 3, "origin", ("OGZ",),
                 "Ключевая точка: 30 км до КПП Верхний Ларс, 148 км до Тбилиси"),
    "SVX": Place("SVX", "Екатеринбург", "RU", 56.75, 60.80, 5, "origin", ("SVX",),
                 "Прямой Red Wings в TBS; хаб для Урала"),
    "KZN": Place("KZN", "Казань", "RU", 55.79, 49.11, 3, "origin", ("KZN",),
                 "Дешёвые плечи в IST/EVN/DXB"),
    "OVB": Place("OVB", "Новосибирск", "RU", 55.03, 82.92, 7, "origin", ("OVB",),
                 "Сибирь: выход через ALA/NQZ/IST"),
    "ROV": Place("ROV", "Ростов-на-Дону", "RU", 47.22, 39.72, 3, "origin", ("ROV",),
                 "Автобусные плечи и подвоз к MRV/OGZ"),
    # --- Транзитные хабы ----------------------------------------------------
    "EVN": Place("EVN", "Ереван", "AM", 40.18, 44.51, 4, "hub", ("EVN",),
                 "Безвизово, много рейсов из РФ, автобус до Тбилиси 5-7 ч"),
    "IST": Place("IST", "Стамбул (IST)", "TR", 41.01, 28.98, 3, "hub", ("IST",),
                 "Turkish/AJet, максимальная ёмкость"),
    "SAW": Place("SAW", "Стамбул (Sabiha)", "TR", 40.90, 29.31, 3, "hub", ("SAW",),
                 "Pegasus; смена аэропорта с IST требует +3 ч"),
    "GYD": Place("GYD", "Баку", "AZ", 40.41, 49.87, 4, "hub", ("GYD",),
                 "AZAL; наземная граница требует проверки режима"),
    "MSQ": Place("MSQ", "Минск", "BY", 53.90, 27.56, 3, "hub", ("MSQ",),
                 "Belavia, безвизово, стабильные тарифы"),
    "DXB": Place("DXB", "Дубай", "AE", 25.20, 55.27, 4, "hub", ("DXB",),
                 "flydubai; работает на акциях"),
    "SHJ": Place("SHJ", "Шарджа", "AE", 25.35, 55.39, 4, "hub", ("SHJ",),
                 "Air Arabia; сверхдешёвые тарифы, но длинные стыковки"),
    "ALA": Place("ALA", "Алматы", "KZ", 43.24, 76.89, 5, "hub", ("ALA",),
                 "SCAT/Air Astana; актуально для Сибири"),
    "NQZ": Place("NQZ", "Астана", "KZ", 51.13, 71.43, 5, "hub", ("NQZ",),
                 "Альтернатива ALA"),
    # --- Грузия -------------------------------------------------------------
    "TBS": Place("TBS", "Тбилиси", "GE", 41.72, 44.78, 4, "target", ("TBS",),
                 "Целевая точка"),
    "KUT": Place("KUT", "Кутаиси", "GE", 42.18, 42.48, 4, "entry", ("KUT",),
                 "Лоукостеры (Wizz); +4 ч поезд/маршрутка до Тбилиси"),
    "BUS": Place("BUS", "Батуми", "GE", 41.60, 41.60, 4, "entry", ("BUS",),
                 "Поезд Батуми-Тбилиси ~5 ч, 20-30 лари"),
    # --- Погранпереход ------------------------------------------------------
    "LARS": Place("LARS", "КПП Верхний Ларс / Дарьяли", "RU-GE", 42.75, 44.62, 3,
                  "border", (),
                  "Единственный работающий автопереход РФ-Грузия; очереди 1-8 ч"),
}

#: Города Грузии, прибытие в которые считается достижением цели только вместе
#: с наземным плечом до Тбилиси.
GEORGIA_ENTRY_POINTS = ("TBS", "KUT", "BUS")

#: Пары аэропортов, между которыми пересадка требует переезда по городу.
AIRPORT_CHANGE_PAIRS = {
    frozenset(("IST", "SAW")),
    frozenset(("VKO", "DME")),
    frozenset(("VKO", "SVO")),
    frozenset(("VKO", "ZIA")),
    frozenset(("DME", "SVO")),
    frozenset(("DME", "ZIA")),
    frozenset(("SVO", "ZIA")),
    frozenset(("DXB", "SHJ")),
}


@dataclass(frozen=True)
class DirectRoute:
    """Известное прямое авиасообщение — используется для приоритизации запросов."""

    origin: str
    destination: str
    carriers: tuple[str, ...]
    note: str = ""


#: Прямые рейсы в Грузию из РФ (сентябрь 2026). Список сознательно консервативный:
#: он влияет только на порядок опроса источников, а не на фильтрацию результатов.
KNOWN_DIRECT_TO_GEORGIA: tuple[DirectRoute, ...] = (
    DirectRoute("MOW", "TBS", ("A4", "WZ", "A9"), "Внуково и Жуковский, ежедневно"),
    DirectRoute("AER", "TBS", ("WZ",), "Red Wings, ~1 ч 25 мин"),
    DirectRoute("MRV", "TBS", ("A4", "A9"), "по выходным"),
    DirectRoute("SVX", "TBS", ("WZ",), "несколько раз в неделю"),
)

#: Коридоры «хаб → Грузия», которые имеет смысл опрашивать.
HUB_TO_GEORGIA: tuple[DirectRoute, ...] = (
    DirectRoute("EVN", "TBS", ("bus",), "автобус/маршрутка 5-7 ч"),
    DirectRoute("IST", "TBS", ("TK", "VF"), ""),
    DirectRoute("SAW", "TBS", ("PC",), ""),
    DirectRoute("GYD", "TBS", ("J2",), ""),
    DirectRoute("MSQ", "TBS", ("B2",), ""),
    DirectRoute("DXB", "TBS", ("FZ",), ""),
    DirectRoute("SHJ", "TBS", ("G9",), ""),
    DirectRoute("ALA", "TBS", ("DV", "KC"), ""),
    DirectRoute("KUT", "TBS", ("train", "bus"), "внутри Грузии"),
    DirectRoute("BUS", "TBS", ("train", "bus"), "внутри Грузии"),
    DirectRoute("OGZ", "TBS", ("bus", "taxi"), "через КПП Верхний Ларс"),
)


def place(code: str) -> Place:
    try:
        return PLACES[code]
    except KeyError as exc:  # pragma: no cover - защита от опечаток в конфиге
        raise KeyError(f"Неизвестный код города: {code}") from exc


def distance_km(code_a: str, code_b: str) -> float:
    """Ортодромия между городами (для detour-фильтра)."""
    a, b = place(code_a), place(code_b)
    lat1, lon1, lat2, lon2 = map(radians, (a.lat, a.lon, b.lat, b.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))


def path_distance_km(path: list[str]) -> float:
    return sum(distance_km(a, b) for a, b in zip(path, path[1:]))


def detour_ratio(path: list[str]) -> float:
    """Во сколько раз путь длиннее прямой линии от начала до конца."""
    direct = distance_km(path[0], path[-1])
    if direct <= 0:
        return 1.0
    return path_distance_km(path) / direct


def requires_airport_change(code_a: str, code_b: str) -> bool:
    return frozenset((code_a, code_b)) in AIRPORT_CHANGE_PAIRS


def is_border_crossing(code_a: str, code_b: str) -> bool:
    """Плечо пересекает границу РФ-Грузия по земле."""
    a, b = place(code_a), place(code_b)
    ground_ru = {"OGZ", "MRV", "AER", "ROV", "LARS"}
    return a.code in ground_ru and b.country == "GE" or b.code in ground_ru and a.country == "GE"


def crosses_border(code_a: str, code_b: str) -> bool:
    return place(code_a).country != place(code_b).country


def origins() -> list[str]:
    return [p.code for p in PLACES.values() if p.role == "origin"]


def hubs() -> list[str]:
    return [p.code for p in PLACES.values() if p.role == "hub"]

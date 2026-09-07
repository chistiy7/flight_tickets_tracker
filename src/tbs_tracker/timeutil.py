"""Работа со временем: локальные зоны городов, парсинг ISO, форматирование."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .geo import PLACES


def city_tz(code: str) -> timezone:
    place = PLACES.get(code)
    offset = place.tz_offset if place else 0
    return timezone(timedelta(hours=offset))


def localize(value: datetime, city: str) -> datetime:
    """Привязать наивное время к зоне города; aware-время не трогать."""
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=city_tz(city))


def parse_dt(value: str | datetime | None, city: str | None = None) -> datetime | None:
    """Разобрать ISO-строку. Наивное время трактуется как местное для города."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return localize(value, city) if city else value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None and city:
        parsed = localize(parsed, city)
    return parsed


def to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def fmt_duration(minutes: int | None) -> str:
    if not minutes:
        return "—"
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}ч {mins}м"
    if hours:
        return f"{hours}ч"
    return f"{mins}м"


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m %H:%M") if value else "—"

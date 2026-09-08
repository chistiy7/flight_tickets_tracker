"""Загрузка и валидация конфигурации трекера."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from .models import Mode


class ConfigError(ValueError):
    pass


def _require(mapping: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{ctx}: отсутствует обязательное поле '{key}'")
    return mapping[key]


def _check_unknown(mapping: dict[str, Any], allowed: set[str], ctx: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigError(f"{ctx}: неизвестные поля {sorted(unknown)}")


@dataclass
class DateWindow:
    date_from: date
    date_to: date

    def __post_init__(self) -> None:
        if self.date_to < self.date_from:
            raise ConfigError(f"окно дат вывернуто: {self.date_from} > {self.date_to}")

    @property
    def nights_span(self) -> int:
        return (self.date_to - self.date_from).days


@dataclass
class SearchConfig:
    origins: list[str] = field(default_factory=lambda: ["MOW", "AER", "MRV", "OGZ", "SVX", "LED"])
    destinations: list[str] = field(default_factory=lambda: ["TBS"])
    entry_points: list[str] = field(default_factory=lambda: ["TBS", "KUT", "BUS"])
    hubs: list[str] = field(default_factory=lambda: ["EVN", "IST", "SAW", "GYD", "MSQ"])
    max_legs: int = 3
    detour_factor: float = 2.2
    passengers: int = 1
    windows: list[DateWindow] = field(default_factory=list)
    top_n: int = 15
    #: Наземные плечи (маршрутка через Верхний Ларс, автобус Ереван→Тбилиси,
    #: поезда внутри Грузии). По умолчанию выключены: трекер ищет только
    #: авиасообщение. Вместе с ними отключаются и цепочки класса D.
    include_ground: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], today: date) -> "SearchConfig":
        data = dict(data or {})
        _check_unknown(
            data,
            {
                "origins", "destinations", "entry_points", "hubs", "max_legs",
                "detour_factor", "passengers", "windows", "relative_windows", "top_n",
                "include_ground",
            },
            "search",
        )
        windows: list[DateWindow] = []
        for raw in data.pop("windows", []) or []:
            windows.append(DateWindow(_as_date(_require(raw, "date_from", "search.windows")),
                                      _as_date(_require(raw, "date_to", "search.windows"))))
        for raw in data.pop("relative_windows", []) or []:
            offset = int(raw.get("offset_days", 0))
            length = int(raw.get("length_days", 14))
            start = today + timedelta(days=offset)
            windows.append(DateWindow(start, start + timedelta(days=length)))
        if not windows:
            start = today + timedelta(days=14)
            windows.append(DateWindow(start, start + timedelta(days=21)))
        cfg = cls(windows=windows, **data)
        if cfg.max_legs < 1:
            raise ConfigError("search.max_legs должен быть >= 1")
        return cfg


@dataclass
class ConnectionsConfig:
    """Минимальные стыковки. Именно эти числа отделяют реальный маршрут от фантазии."""

    min_same_ticket_min: int = 75
    min_self_transfer_min: int = 210
    min_airport_change_min: int = 330
    min_border_min: int = 240
    min_ground_min: int = 90
    max_layover_min: int = 1440
    overnight_threshold_min: int = 480

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConnectionsConfig":
        data = dict(data or {})
        _check_unknown(data, set(cls.__dataclass_fields__), "connections")
        return cls(**data)


@dataclass
class CostsConfig:
    airport_transfer_rub: float = 600.0
    border_transfer_rub: float = 0.0
    overnight_rub: float = 3500.0
    baggage_rub: float = 3000.0
    need_baggage: bool = True
    time_value_rub_per_hour: float = 150.0
    self_transfer_failure_prob: float = 0.06
    rebooking_cost_rub: float = 12000.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CostsConfig":
        data = dict(data or {})
        _check_unknown(data, set(cls.__dataclass_fields__), "costs")
        cfg = cls(**data)
        if not 0.0 <= cfg.self_transfer_failure_prob <= 1.0:
            raise ConfigError("costs.self_transfer_failure_prob должен быть в [0, 1]")
        return cfg


@dataclass
class FiltersConfig:
    sanity_min_price_rub: float = 500.0
    sanity_max_price_rub: float = 300000.0
    allow_foreign_card_only: bool = True
    allow_cached_prices: bool = True
    max_total_duration_hours: int = 60
    exclude_carriers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FiltersConfig":
        data = dict(data or {})
        _check_unknown(data, set(cls.__dataclass_fields__), "filters")
        return cls(**data)


@dataclass
class ProviderConfig:
    name: str
    enabled: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def secret(self, env_key_option: str, default_env: str | None = None) -> str | None:
        env_name = self.options.get(env_key_option, default_env)
        if not env_name:
            return None
        return os.environ.get(env_name) or None


@dataclass
class AlertsConfig:
    target_price_rub: float | None = None
    drop_pct: float = 15.0
    percentile: float = 10.0
    min_observations: int = 5
    cooldown_hours: int = 12
    channels: dict[str, dict[str, Any]] = field(default_factory=lambda: {"console": {"enabled": True}})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlertsConfig":
        data = dict(data or {})
        _check_unknown(data, set(cls.__dataclass_fields__), "alerts")
        return cls(**data)


@dataclass
class FxConfig:
    mode: str = "static"
    static_rates: dict[str, float] = field(default_factory=lambda: {
        "RUB": 1.0, "USD": 95.0, "EUR": 105.0, "GEL": 35.0,
        "TRY": 2.6, "AMD": 0.24, "KZT": 0.19, "AED": 26.0, "BYN": 29.0,
    })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FxConfig":
        data = dict(data or {})
        _check_unknown(data, set(cls.__dataclass_fields__), "runtime.fx")
        cfg = cls(**data)
        if cfg.mode not in ("static", "cbr"):
            raise ConfigError("runtime.fx.mode: допустимо 'static' или 'cbr'")
        cfg.static_rates.setdefault("RUB", 1.0)
        return cfg


@dataclass
class RuntimeConfig:
    db_path: Path = Path("data/tracker.sqlite3")
    cache_dir: Path = Path(".cache")
    cache_ttl_minutes: int = 180
    max_requests_per_run: int = 300
    request_timeout_sec: int = 20
    max_workers: int = 6
    fx: FxConfig = field(default_factory=FxConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeConfig":
        data = dict(data or {})
        _check_unknown(data, set(cls.__dataclass_fields__), "runtime")
        fx = FxConfig.from_dict(data.pop("fx", {}))
        if "db_path" in data:
            data["db_path"] = Path(data["db_path"])
        if "cache_dir" in data:
            data["cache_dir"] = Path(data["cache_dir"])
        return cls(fx=fx, **data)


@dataclass
class Config:
    search: SearchConfig = field(default_factory=SearchConfig)
    connections: ConnectionsConfig = field(default_factory=ConnectionsConfig)
    costs: CostsConfig = field(default_factory=CostsConfig)
    filters: FiltersConfig = field(default_factory=FiltersConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)

    @property
    def enabled_providers(self) -> list[ProviderConfig]:
        return [p for p in self.providers.values() if p.enabled]

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base_dir: Path | None = None,
                  today: date | None = None) -> "Config":
        data = dict(data or {})
        _check_unknown(
            data,
            {"search", "connections", "costs", "filters", "alerts", "runtime", "providers"},
            "config",
        )
        providers: dict[str, ProviderConfig] = {}
        for name, raw in (data.get("providers") or {}).items():
            raw = dict(raw or {})
            enabled = bool(raw.pop("enabled", False))
            providers[name] = ProviderConfig(name=name, enabled=enabled, options=raw)
        return cls(
            search=SearchConfig.from_dict(data.get("search", {}), today or date.today()),
            connections=ConnectionsConfig.from_dict(data.get("connections", {})),
            costs=CostsConfig.from_dict(data.get("costs", {})),
            filters=FiltersConfig.from_dict(data.get("filters", {})),
            alerts=AlertsConfig.from_dict(data.get("alerts", {})),
            runtime=RuntimeConfig.from_dict(data.get("runtime", {})),
            providers=providers,
            base_dir=base_dir or Path.cwd(),
        )

    @classmethod
    def load(cls, path: str | Path, *, today: date | None = None) -> "Config":
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise ConfigError(f"конфиг не найден: {path}")
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        # Относительные пути в конфиге считаются от корня проекта, а сам конфиг
        # обычно лежит в config/, поэтому поднимаемся на уровень выше.
        base_dir = path.parent.parent if path.parent.name == "config" else path.parent
        return cls.from_dict(data, base_dir=base_dir, today=today)


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def parse_modes(values: Any, default: tuple[Mode, ...] = (Mode.AIR,)) -> tuple[Mode, ...]:
    if not values:
        return default
    if isinstance(values, str):
        values = [values]
    return tuple(Mode(v) for v in values)

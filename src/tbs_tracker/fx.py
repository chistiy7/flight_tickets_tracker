"""Приведение цен к рублям.

Все сравнения в трекере идут в ₽, потому что часть плеч продаётся в лари, лирах,
драмах и долларах, и без единой валюты «самый дешёвый» вариант выбрать нельзя.
"""

from __future__ import annotations

import logging

from .config import FxConfig
from .httpclient import HttpClient

log = logging.getLogger(__name__)

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"


class CurrencyConverter:
    def __init__(self, config: FxConfig, http: HttpClient | None = None) -> None:
        self._config = config
        self._http = http
        self._rates: dict[str, float] = {k.upper(): float(v) for k, v in config.static_rates.items()}
        self._rates.setdefault("RUB", 1.0)
        self._loaded_from_cbr = False

    @property
    def source(self) -> str:
        return "cbr" if self._loaded_from_cbr else "static"

    def _ensure_rates(self) -> None:
        if self._config.mode != "cbr" or self._loaded_from_cbr or self._http is None:
            return
        try:
            payload = self._http.get_json(CBR_URL, source="cbr")
            for code, info in (payload.get("Valute") or {}).items():
                nominal = float(info.get("Nominal") or 1)
                value = float(info.get("Value") or 0)
                if nominal > 0 and value > 0:
                    self._rates[code.upper()] = value / nominal
            self._rates["RUB"] = 1.0
            self._loaded_from_cbr = True
        except Exception as exc:  # курсы не должны ломать прогон
            log.warning("не удалось получить курсы ЦБ, используем статические: %s", exc)

    def rate(self, currency: str) -> float:
        currency = (currency or "RUB").upper()
        if currency == "RUB":
            return 1.0
        self._ensure_rates()
        rate = self._rates.get(currency)
        if rate is None:
            raise KeyError(
                f"нет курса для {currency}: добавьте его в runtime.fx.static_rates"
            )
        return rate

    def to_rub(self, amount: float, currency: str) -> float:
        return round(amount * self.rate(currency), 2)

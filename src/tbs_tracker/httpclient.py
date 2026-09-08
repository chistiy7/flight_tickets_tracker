"""HTTP-клиент с ретраями, таймаутами, файловым кэшем и учётом бюджета запросов.

Кэш нужен не только для скорости: у Tourvisor суточный лимит поисков, а Travelpayouts
прямо рекомендует не дёргать API в реальном времени. Бюджет запросов на прогон
не даёт трекеру случайно сжечь квоту.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

USER_AGENT = "tbs-tracker/0.1 (+https://github.com/; personal price tracker)"


class RequestBudgetExceeded(RuntimeError):
    pass


@dataclass
class RequestBudget:
    limit: int
    used: int = 0
    per_source: dict[str, int] = field(default_factory=dict)

    def charge(self, source: str, count: int = 1) -> None:
        if self.used + count > self.limit:
            raise RequestBudgetExceeded(
                f"исчерпан бюджет запросов на прогон ({self.limit}); источник {source}"
            )
        self.used += count
        self.per_source[source] = self.per_source.get(source, 0) + count

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class HttpClient:
    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        cache_ttl_minutes: int = 180,
        timeout_sec: int = 20,
        budget: RequestBudget | None = None,
        retries: int = 3,
        backoff_sec: float = 1.5,
        session: requests.Session | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl_sec = cache_ttl_minutes * 60
        self.timeout_sec = timeout_sec
        self.budget = budget
        self.retries = retries
        self.backoff_sec = backoff_sec
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ кэш
    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha1(key.encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _cache_read(self, key: str) -> Any | None:
        path = self._cache_path(key)
        if not path or not path.exists():
            return None
        if self.cache_ttl_sec and time.time() - path.stat().st_mtime > self.cache_ttl_sec:
            return None
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, key: str, payload: Any) -> None:
        path = self._cache_path(key)
        if not path:
            return
        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        except OSError as exc:  # pragma: no cover
            log.debug("не удалось записать кэш %s: %s", path, exc)

    # ------------------------------------------------------------- запросы
    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        source: str = "http",
        use_cache: bool = True,
        charge_budget: bool = True,
    ) -> Any:
        return self._json_request("GET", url, params=params, headers=headers,
                                  source=source, use_cache=use_cache,
                                  charge_budget=charge_budget)

    def post_json(
        self,
        url: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        source: str = "http",
        use_cache: bool = True,
        charge_budget: bool = True,
    ) -> Any:
        return self._json_request("POST", url, json_body=json_body, headers=headers,
                                  source=source, use_cache=use_cache,
                                  charge_budget=charge_budget)

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        source: str = "http",
        use_cache: bool = True,
        charge_budget: bool = True,
    ) -> Any:
        cache_key = json.dumps(
            [method, url, params or {}, json_body or {}], sort_keys=True, ensure_ascii=False
        )
        if use_cache:
            cached = self._cache_read(cache_key)
            if cached is not None:
                log.debug("cache hit %s %s", method, url)
                return cached

        # Бюджет защищает чужие квоты. Обращение к своему складу офферов его не
        # расходует, иначе опрос парсера конкурировал бы с внешними источниками.
        if self.budget and charge_budget:
            self.budget.charge(source)

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=headers, timeout=self.timeout_sec,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"{response.status_code} {response.reason}", response=response
                    )
                response.raise_for_status()
                payload = response.json()
                if use_cache:
                    self._cache_write(cache_key, payload)
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                sleep_for = self.backoff_sec * (2 ** (attempt - 1))
                log.warning("%s %s: попытка %d/%d не удалась (%s), пауза %.1fs",
                            method, url, attempt, self.retries, exc, sleep_for)
                time.sleep(sleep_for)
        raise RuntimeError(f"запрос к {url} не удался: {last_error}") from last_error

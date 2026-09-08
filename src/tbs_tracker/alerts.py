"""Детекторы дешёвых цен и каналы уведомлений.

Ключевая идея: алерт по кэш-цене (Travelpayouts) без подтверждения живым источником
даёт поток ложных срабатываний, поэтому такие сигналы помечаются как требующие
проверки, а не выдаются как «купи сейчас».
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config
from .httpclient import HttpClient
from .models import Itinerary, PriceKind
from .store import Store
from .timeutil import fmt_dt, fmt_duration

log = logging.getLogger(__name__)


@dataclass
class Alert:
    kind: str
    itinerary: Itinerary
    message: str

    @property
    def price_rub(self) -> float:
        return self.itinerary.cost.out_of_pocket_rub


def evaluate(
    itineraries: list[Itinerary], store: Store, config: Config, *, run_id: int | None = None
) -> list[Alert]:
    cfg = config.alerts
    alerts: list[Alert] = []
    now = datetime.now(timezone.utc)

    # По каждому маршруту оцениваем только лучший вариант — иначе один и тот же
    # рейс наплодит десятки алертов.
    best_per_route: dict[str, Itinerary] = {}
    for itinerary in itineraries:
        current = best_per_route.get(itinerary.route_key)
        if current is None or itinerary.cost.out_of_pocket_rub < current.cost.out_of_pocket_rub:
            best_per_route[itinerary.route_key] = itinerary

    for route_key, itinerary in best_per_route.items():
        price = itinerary.cost.out_of_pocket_rub
        baseline = store.baseline(
            route_key, percentile=cfg.percentile, exclude_run_id=run_id
        )
        triggered: list[tuple[str, str]] = []

        if cfg.target_price_rub is not None and price <= cfg.target_price_rub:
            triggered.append((
                "target",
                f"цена {price:.0f}₽ не выше целевой {cfg.target_price_rub:.0f}₽",
            ))

        if baseline.observations >= cfg.min_observations:
            if baseline.percentile_rub is not None and price <= baseline.percentile_rub:
                triggered.append((
                    "percentile",
                    f"цена {price:.0f}₽ ниже p{cfg.percentile:.0f} истории "
                    f"({baseline.percentile_rub:.0f}₽, наблюдений {baseline.observations})",
                ))
            if baseline.last_rub and baseline.last_rub > 0:
                drop_pct = (baseline.last_rub - price) / baseline.last_rub * 100
                if drop_pct >= cfg.drop_pct:
                    triggered.append((
                        "drop",
                        f"падение на {drop_pct:.0f}% относительно предыдущего наблюдения "
                        f"({baseline.last_rub:.0f}₽ → {price:.0f}₽)",
                    ))
        elif baseline.observations == 0 and cfg.target_price_rub is None:
            triggered.append(("new_route", "новый маршрут: истории ещё нет"))

        for kind, reason in triggered:
            if _in_cooldown(store, route_key, kind, cfg.cooldown_hours, now):
                continue
            alerts.append(Alert(kind=kind, itinerary=itinerary, message=_render(itinerary, reason)))
            store.record_alert(
                kind=kind,
                route_key=route_key,
                itinerary_id=itinerary.itinerary_id,
                price_rub=price,
                message=reason,
            )
    return alerts


def _in_cooldown(store: Store, route_key: str, kind: str, cooldown_hours: int,
                 now: datetime) -> bool:
    last = store.last_alert_at(route_key, kind)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last < timedelta(hours=cooldown_hours)


def _render(itinerary: Itinerary, reason: str) -> str:
    first = itinerary.legs[0]
    when = (
        f"{itinerary.depart:%d.%m} (по заполнению)"
        if first.flexible and itinerary.depart
        else fmt_dt(itinerary.depart)
    )
    lines = [
        f"{itinerary.route_key} — {itinerary.cost.out_of_pocket_rub:.0f}₽ "
        f"(билеты {itinerary.tickets_rub:.0f}₽), класс {itinerary.chain_class}",
        f"причина: {reason}",
        f"старт {when}, в пути {fmt_duration(itinerary.total_duration_min)}",
    ]
    for leg in itinerary.legs:
        lines.append(f"  • {leg.describe()} [{leg.source}]")
        if leg.nights_included and leg.notes:
            lines.append(f"      {leg.notes}")
        lines.append(f"      {leg.where_to_buy()}")
    if itinerary.price_kind != PriceKind.LIVE:
        lines.append(
            "  ! цена не живая (кэш/оценка) — подтвердите на сайте перевозчика перед покупкой"
        )
    if itinerary.flags:
        lines.append(f"  флаги: {', '.join(itinerary.flags)}")
    return "\n".join(lines)


class Notifier:
    """Отправка алертов в настроенные каналы."""

    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.http = http

    def send(self, alerts: list[Alert]) -> None:
        if not alerts:
            return
        channels = self.config.alerts.channels or {}
        for name, settings in channels.items():
            if not (settings or {}).get("enabled"):
                continue
            handler = getattr(self, f"_send_{name}", None)
            if handler is None:
                log.warning("неизвестный канал уведомлений: %s", name)
                continue
            try:
                handler(alerts, settings)
            except Exception as exc:  # noqa: BLE001 - канал не должен ронять прогон
                log.error("канал %s не смог отправить алерты: %s", name, exc)

    def _send_console(self, alerts: list[Alert], settings: dict) -> None:
        print(f"\n=== Алерты ({len(alerts)}) ===")
        for alert in alerts:
            print(f"[{alert.kind}] {alert.message}\n")

    def _send_telegram(self, alerts: list[Alert], settings: dict) -> None:
        token = os.environ.get(settings.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
        chat_id = os.environ.get(settings.get("chat_id_env", "TELEGRAM_CHAT_ID"))
        if not token or not chat_id:
            log.warning("telegram: нет токена или chat_id в переменных окружения")
            return
        if self.http is None:
            log.warning("telegram: нет http-клиента")
            return
        for alert in alerts:
            self.http.post_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json_body={
                    "chat_id": chat_id,
                    "text": f"[{alert.kind}] {alert.message}",
                    "disable_web_page_preview": True,
                },
                source="telegram",
                use_cache=False,
            )

    def _send_webhook(self, alerts: list[Alert], settings: dict) -> None:
        url = os.environ.get(settings.get("url_env", "ALERT_WEBHOOK_URL"))
        if not url:
            log.warning("webhook: нет URL в переменных окружения")
            return
        if self.http is None:
            return
        self.http.post_json(
            url,
            json_body={
                "alerts": [
                    {
                        "kind": alert.kind,
                        "price_rub": alert.price_rub,
                        "route": alert.itinerary.route_key,
                        "class": alert.itinerary.chain_class,
                        "message": alert.message,
                    }
                    for alert in alerts
                ]
            },
            source="webhook",
            use_cache=False,
        )

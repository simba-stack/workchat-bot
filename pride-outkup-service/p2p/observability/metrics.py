"""Prometheus metrics для P2P (ТЗ Том 18 §17 — Observability).

Метрики разделены на:
- Counters    — события (trades created, disputes, etc.)
- Histograms  — длительность workflow
- Gauges      — мгновенные снимки (escrow, pending trades, outbox lag)

Все Counter/Histogram вызовы безопасны (no-op если prometheus_client не установлен).
Workflows не модифицируются — это хук на будущее: можно вызывать
inc_trade_completed() из orchestrator или workflow.
"""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger("p2p.metrics")

try:
    from prometheus_client import (  # type: ignore
        Counter, Histogram, Gauge, CollectorRegistry, generate_latest,
        CONTENT_TYPE_LATEST,
    )
    _ENABLED = True
except Exception:
    _ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    def generate_latest(*_a, **_kw):  # type: ignore
        return b""

    class _Noop:
        def __init__(self, *a, **kw):
            pass
        def labels(self, *a, **kw):
            return self
        def inc(self, *a, **kw):
            return None
        def observe(self, *a, **kw):
            return None
        def set(self, *a, **kw):
            return None

    Counter = Histogram = Gauge = _Noop  # type: ignore


# ─────────────────────────────────────────────────────────────
# Counter metrics
# ─────────────────────────────────────────────────────────────

trades_total = Counter(
    "p2p_trades_total", "Total trades created/transitioned by status",
    ["status", "currency"],
)

advertisements_total = Counter(
    "p2p_advertisements_total", "Total advertisements created by type",
    ["type"],
)

disputes_total = Counter(
    "p2p_disputes_total", "Total disputes by resolution",
    ["resolution"],
)


# ─────────────────────────────────────────────────────────────
# Histogram
# ─────────────────────────────────────────────────────────────

workflow_duration_seconds = Histogram(
    "p2p_workflow_duration_seconds",
    "Workflow duration in seconds",
    ["workflow_type"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


# ─────────────────────────────────────────────────────────────
# Gauges (refreshed via worker)
# ─────────────────────────────────────────────────────────────

escrow_balance = Gauge(
    "p2p_escrow_balance",
    "Sum of trade_escrow across all wallets per currency",
    ["currency"],
)

pending_trades = Gauge(
    "p2p_pending_trades",
    "Number of trades not in terminal state",
    ["status"],
)

outbox_pending = Gauge(
    "p2p_outbox_pending",
    "Number of PENDING events in outbox",
)

recon_errors = Gauge(
    "p2p_recon_errors",
    "Errors count from last reconciliation run",
)


# ─────────────────────────────────────────────────────────────
# Public helpers (called by future workflow hooks)
# ─────────────────────────────────────────────────────────────

def inc_trade(status: str, currency: str = "USDT", value: int = 1) -> None:
    try:
        trades_total.labels(status=status, currency=currency).inc(value)
    except Exception:
        pass


def inc_advertisement(type_: str, value: int = 1) -> None:
    try:
        advertisements_total.labels(type=type_).inc(value)
    except Exception:
        pass


def inc_dispute(resolution: str, value: int = 1) -> None:
    try:
        disputes_total.labels(resolution=resolution).inc(value)
    except Exception:
        pass


def observe_workflow(workflow_type: str, seconds: float) -> None:
    try:
        workflow_duration_seconds.labels(workflow_type=workflow_type).observe(seconds)
    except Exception:
        pass


def set_outbox_pending(n: int) -> None:
    try:
        outbox_pending.set(n)
    except Exception:
        pass


def set_recon_errors(n: int) -> None:
    try:
        recon_errors.set(n)
    except Exception:
        pass


def set_escrow_balance(currency: str, amount: float) -> None:
    try:
        escrow_balance.labels(currency=currency).set(amount)
    except Exception:
        pass


def set_pending_trades(status: str, n: int) -> None:
    try:
        pending_trades.labels(status=status).set(n)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Refresh gauges from DB
# ─────────────────────────────────────────────────────────────

async def refresh_gauges(db) -> dict[str, Any]:
    """Раз в 30 сек обновлять Gauge-метрики через SELECT-агрегаты.

    Возвращает summary для логов / debug.
    """
    from sqlalchemy import select, func
    from p2p.models import P2PWallet, P2PTrade, P2POutbox
    from p2p.enums import TradeStatus, OutboxStatus

    summary: dict[str, Any] = {}

    # escrow_balance per currency
    try:
        r = await db.execute(
            select(P2PWallet.currency, func.coalesce(func.sum(P2PWallet.trade_escrow), 0))
            .group_by(P2PWallet.currency)
        )
        per_currency = {}
        for cur, amt in r.all():
            f_amt = float(amt or 0)
            set_escrow_balance(cur, f_amt)
            per_currency[cur] = f_amt
        summary["escrow_balance"] = per_currency
    except Exception as e:
        logger.warning("[metrics] escrow_balance refresh failed: %s", e)

    # pending_trades per status (non-terminal)
    try:
        non_terminal = [
            TradeStatus.CREATED.value, TradeStatus.ESCROW_LOCKED.value,
            TradeStatus.WAITING_FOR_PAYMENT.value, TradeStatus.PAYMENT_MARKED.value,
            TradeStatus.PAYMENT_CONFIRMATION.value, TradeStatus.DISPUTE_OPENED.value,
            TradeStatus.ARBITRATION.value, TradeStatus.RESOLVED.value,
        ]
        r = await db.execute(
            select(P2PTrade.status, func.count(P2PTrade.id))
            .where(P2PTrade.status.in_(non_terminal))
            .group_by(P2PTrade.status)
        )
        per_status: dict[str, int] = {s: 0 for s in non_terminal}
        for st, cnt in r.all():
            per_status[st] = int(cnt or 0)
        for st, cnt in per_status.items():
            set_pending_trades(st, cnt)
        summary["pending_trades"] = per_status
    except Exception as e:
        logger.warning("[metrics] pending_trades refresh failed: %s", e)

    # outbox_pending
    try:
        r = await db.execute(
            select(func.count(P2POutbox.id))
            .where(P2POutbox.status == OutboxStatus.PENDING.value)
        )
        cnt = int(r.scalar() or 0)
        set_outbox_pending(cnt)
        summary["outbox_pending"] = cnt
    except Exception as e:
        logger.warning("[metrics] outbox_pending refresh failed: %s", e)

    return summary


def render_latest() -> bytes:
    """Render всех метрик в Prometheus text format."""
    return generate_latest()

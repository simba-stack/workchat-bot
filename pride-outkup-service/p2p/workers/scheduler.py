"""Scheduler Worker (ТЗ Том 15).

Periodic tasks:
- TRADE_PAYMENT_TIMEOUT: если pay_deadline_at прошёл — auto-cancel
- DISPUTE_SLA: если диспут OPENED > N часов — нотификация админу
- IDEMPOTENCY_CLEANUP: удалить просроченные ключи
- RECONCILIATION: см. reconciliation.py (отдельный таск)
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from core.db import AsyncSessionLocal
from p2p import idempotency, policies
from p2p.enums import TradeStatus
from p2p.models import P2PTrade
from p2p.workflows import cancel_trade
from p2p.orchestrator import run_workflow

logger = logging.getLogger("p2p.worker.scheduler")


async def _check_payment_timeouts(db) -> int:
    """Найти WAITING_FOR_PAYMENT trades с истёкшим deadline → cancel."""
    now = datetime.now(timezone.utc)
    r = await db.execute(
        select(P2PTrade).where(
            P2PTrade.status == TradeStatus.WAITING_FOR_PAYMENT.value,
            P2PTrade.pay_deadline_at != None,  # noqa: E711
            P2PTrade.pay_deadline_at < now,
        ).limit(100)
    )
    expired = list(r.scalars().all())
    cnt = 0
    for t in expired:
        try:
            await run_workflow(
                db,
                workflow_type="cancel_trade",
                user_id=t.buyer_id,  # формально buyer'а, но source=system
                input_payload={"trade_id": t.id, "reason": "auto-timeout: payment deadline expired"},
                handler=cancel_trade.handle,
                actor_role="SYSTEM",
                source="scheduler:payment_timeout",
            )
            await db.commit()
            cnt += 1
        except Exception as e:
            await db.rollback()
            logger.warning("[scheduler] auto-cancel trade=%s failed: %s", t.id, e)
    return cnt


async def _cleanup_idempotency(db) -> int:
    n = await idempotency.cleanup_expired(db)
    await db.commit()
    return n


async def run() -> None:
    logger.info("[scheduler] started")
    last_idemp_cleanup = datetime.now(timezone.utc)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                # payment timeouts — каждые 30 сек
                cnt = await _check_payment_timeouts(db)
                if cnt:
                    logger.info("[scheduler] auto-cancelled %d expired trades", cnt)

                # idempotency cleanup — раз в час
                now = datetime.now(timezone.utc)
                if (now - last_idemp_cleanup).total_seconds() > 3600:
                    n = await _cleanup_idempotency(db)
                    if n:
                        logger.info("[scheduler] cleaned %d expired idempotency keys", n)
                    last_idemp_cleanup = now
        except Exception as e:
            logger.exception("[scheduler] iteration failed: %s", e)
        await asyncio.sleep(30)

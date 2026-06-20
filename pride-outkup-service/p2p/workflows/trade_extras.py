"""Trade extras workflows.

- extend_deadline: продлить pay_deadline_at трейда (только seller, +N минут)
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, locks, outbox
from p2p.enums import EventType, TradeStatus
from p2p.models import P2PTrade
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.trade_extras")


# Максимум на одно продление
MAX_EXTEND_MINUTES = 30
# Минимум
MIN_EXTEND_MINUTES = 1
# Сколько раз можно продлевать
MAX_EXTEND_COUNT = 2


async def extend_deadline(ctx: WorkflowContext) -> dict:
    """Продлить pay_deadline_at трейда максимум на +MAX_EXTEND_MINUTES.

    - Может только seller (когда buyer попросил время).
    - Только в WAITING_FOR_PAYMENT.
    - inc version, audit, emit TRADE_DEADLINE_EXTENDED.
    """
    p = ctx.input_payload
    db = ctx.db

    trade_id = p.get("trade_id")
    try:
        minutes = int(p.get("minutes") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "minutes must be int")
    if not trade_id:
        raise HTTPException(422, "trade_id required")
    if minutes < MIN_EXTEND_MINUTES or minutes > MAX_EXTEND_MINUTES:
        raise HTTPException(
            422,
            f"minutes must be {MIN_EXTEND_MINUTES}..{MAX_EXTEND_MINUTES}",
        )

    await locks.lock_trade(db, trade_id)
    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = r.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    if trade.seller_id != ctx.user_id:
        raise HTTPException(403, "only seller can extend deadline")

    if trade.status != TradeStatus.WAITING_FOR_PAYMENT.value:
        raise HTTPException(
            409,
            f"can extend deadline only in WAITING_FOR_PAYMENT (current: {trade.status})",
        )

    now = datetime.now(timezone.utc)
    old_deadline = trade.pay_deadline_at
    if old_deadline and old_deadline.tzinfo is None:
        old_deadline = old_deadline.replace(tzinfo=timezone.utc)
    base = old_deadline if (old_deadline and old_deadline > now) else now
    new_deadline = base + timedelta(minutes=minutes)

    prev_deadline_iso = old_deadline.isoformat() if old_deadline else None
    trade.pay_deadline_at = new_deadline
    trade.version = (trade.version or 0) + 1
    await db.flush()

    await audit.log(
        db,
        action="trade.deadline_extended",
        entity_type="trade",
        entity_id=trade.id,
        actor_id=ctx.user_id,
        actor_role=ctx.actor_role,
        previous_state={"pay_deadline_at": prev_deadline_iso},
        new_state={
            "pay_deadline_at": new_deadline.isoformat(),
            "extended_minutes": minutes,
        },
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.TRADE_DEADLINE_EXTENDED.value,
        payload={
            "trade_id": trade.id,
            "buyer_id": trade.buyer_id,
            "seller_id": trade.seller_id,
            "pay_deadline_at": new_deadline.isoformat(),
            "extended_minutes": minutes,
            "previous_deadline_at": prev_deadline_iso,
        },
        aggregate_type="trade",
        aggregate_id=trade.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "trade_id": trade.id,
        "pay_deadline_at": new_deadline.isoformat(),
        "extended_minutes": minutes,
        "version": trade.version,
    }

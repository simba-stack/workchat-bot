"""Workflow: mark_paid — buyer says 'я перевёл фиат'."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, locks, outbox, state
from p2p.enums import TradeStatus, EventType
from p2p.models import P2PTrade
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.mark_paid")


async def handle(ctx: WorkflowContext) -> dict:
    p = ctx.input_payload
    db = ctx.db

    trade_id = p.get("trade_id")
    if not trade_id:
        raise HTTPException(422, "trade_id required")

    await locks.lock_trade(db, trade_id)
    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = r.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    if trade.buyer_id != ctx.user_id:
        raise HTTPException(403, "only buyer can mark paid")

    if trade.status == TradeStatus.PAYMENT_MARKED.value:
        # idempotent
        return {"ok": True, "trade_id": trade_id, "status": trade.status,
                "already_marked": True}

    state.assert_trade_transition(trade.status, TradeStatus.PAYMENT_MARKED.value)
    prev = trade.status
    trade.status = TradeStatus.PAYMENT_MARKED.value
    trade.payment_marked_at = datetime.now(timezone.utc)
    trade.version += 1
    await db.flush()

    await audit.log(
        db,
        action="trade.payment_marked",
        entity_type="trade",
        entity_id=trade_id,
        actor_id=ctx.user_id,
        previous_state={"status": prev},
        new_state={"status": trade.status, "payment_marked_at": trade.payment_marked_at.isoformat()},
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.TRADE_PAYMENT_MARKED.value,
        payload={"trade_id": trade_id, "buyer_id": trade.buyer_id, "seller_id": trade.seller_id},
        aggregate_type="trade",
        aggregate_id=trade_id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {"ok": True, "trade_id": trade_id, "status": trade.status}

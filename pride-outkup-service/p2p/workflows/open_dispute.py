"""Workflow: open_dispute — buyer/seller открывает диспут (трейд → DISPUTE_OPENED)."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, locks, outbox, state
from p2p.enums import TradeStatus, DisputeStatus, EventType
from p2p.models import P2PTrade, P2PDispute
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.open_dispute")


async def handle(ctx: WorkflowContext) -> dict:
    p = ctx.input_payload
    db = ctx.db

    trade_id = p.get("trade_id")
    reason = (p.get("reason") or "")[:500]
    if not trade_id or not reason:
        raise HTTPException(422, "trade_id and reason required")

    await locks.lock_trade(db, trade_id)
    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = r.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    if ctx.user_id not in (trade.buyer_id, trade.seller_id):
        raise HTTPException(403, "not a participant")

    if trade.status not in (TradeStatus.PAYMENT_MARKED.value,
                            TradeStatus.PAYMENT_CONFIRMATION.value,
                            TradeStatus.WAITING_FOR_PAYMENT.value):
        raise HTTPException(409, f"cannot open dispute in status {trade.status}")

    # Один диспут на трейд — UniqueConstraint
    existing = await db.execute(select(P2PDispute).where(P2PDispute.trade_id == trade_id))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "dispute already opened")

    prev = trade.status

    dispute = P2PDispute(
        trade_id=trade_id,
        opener_id=ctx.user_id,
        reason=reason,
        status=DisputeStatus.OPENED.value,
        version=1,
    )
    db.add(dispute)
    await db.flush()

    state.assert_trade_transition(trade.status, TradeStatus.DISPUTE_OPENED.value)
    trade.status = TradeStatus.DISPUTE_OPENED.value
    trade.dispute_opened_at = datetime.now(timezone.utc)
    trade.version += 1
    await db.flush()

    await audit.log(
        db,
        action="dispute.opened",
        entity_type="dispute",
        entity_id=dispute.id,
        actor_id=ctx.user_id,
        previous_state={"trade_status": prev},
        new_state={"dispute_id": dispute.id, "trade_status": trade.status,
                   "opener_id": ctx.user_id, "reason": reason},
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.DISPUTE_OPENED.value,
        payload={"dispute_id": dispute.id, "trade_id": trade_id,
                 "opener_id": ctx.user_id, "buyer_id": trade.buyer_id,
                 "seller_id": trade.seller_id, "reason": reason},
        aggregate_type="dispute",
        aggregate_id=dispute.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {"ok": True, "dispute_id": dispute.id, "trade_id": trade_id, "status": trade.status}

"""Workflow: resolve_dispute — арбитр принимает решение.

Решения:
- BUYER: trade → COMPLETED, escrow buyer'у
- SELLER: trade → CANCELLED, escrow обратно seller'у
- SPLIT: split (требует amount_to_buyer)
"""
from __future__ import annotations
import logging
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, ledger, locks, outbox, state, wallet
from p2p.enums import (
    TradeStatus, DisputeStatus, DisputeResolution, EventType, P2PUserRole,
)
from p2p.models import P2PTrade, P2PDispute
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.resolve_dispute")


async def handle(ctx: WorkflowContext) -> dict:
    if ctx.actor_role not in (P2PUserRole.ARBITRATOR.value, P2PUserRole.ADMIN.value,
                              P2PUserRole.SUPER_ADMIN.value):
        raise HTTPException(403, "arbitrator role required")

    p = ctx.input_payload
    db = ctx.db

    dispute_id = p.get("dispute_id")
    resolution = (p.get("resolution") or "").upper()
    arbitrator_note = (p.get("arbitrator_note") or "")[:2000]
    amount_to_buyer = p.get("amount_to_buyer")
    if not dispute_id or not resolution:
        raise HTTPException(422, "dispute_id, resolution required")

    if resolution not in (DisputeResolution.BUYER.value,
                          DisputeResolution.SELLER.value,
                          DisputeResolution.SPLIT.value):
        raise HTTPException(422, "invalid resolution")

    await locks.lock_dispute(db, dispute_id)
    r = await db.execute(select(P2PDispute).where(P2PDispute.id == dispute_id))
    dispute = r.scalar_one_or_none()
    if not dispute:
        raise HTTPException(404, "dispute not found")
    if dispute.status in (DisputeStatus.RESOLVED.value, DisputeStatus.CLOSED.value):
        raise HTTPException(409, f"dispute already {dispute.status}")

    await locks.lock_trade(db, dispute.trade_id)
    tr = await db.execute(select(P2PTrade).where(P2PTrade.id == dispute.trade_id))
    trade = tr.scalar_one_or_none()
    if not trade:
        raise HTTPException(500, "trade missing for dispute")

    # Сначала dispute → ARBITRATION (если ещё OPENED)
    if dispute.status == DisputeStatus.OPENED.value:
        state.assert_dispute_transition(dispute.status, DisputeStatus.ARBITRATION.value)
        dispute.status = DisputeStatus.ARBITRATION.value
        if trade.status == TradeStatus.DISPUTE_OPENED.value:
            state.assert_trade_transition(trade.status, TradeStatus.ARBITRATION.value)
            trade.status = TradeStatus.ARBITRATION.value

    await locks.lock_user_wallet(db, trade.seller_id, trade.crypto_currency)
    await locks.lock_user_wallet(db, trade.buyer_id, trade.crypto_currency)

    # Применяем resolution
    if resolution == DisputeResolution.BUYER.value:
        await ledger.release_to_buyer(
            db,
            seller_id=trade.seller_id,
            buyer_id=trade.buyer_id,
            currency=trade.crypto_currency,
            amount=trade.crypto_amount,
            platform_fee=trade.fee_crypto or Decimal("0"),
            trade_id=trade.id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
        new_trade_status = TradeStatus.COMPLETED.value
        trade.completed_at = datetime.now(timezone.utc)
    elif resolution == DisputeResolution.SELLER.value:
        await ledger.refund_escrow_to_available(
            db,
            user_id=trade.seller_id,
            currency=trade.crypto_currency,
            amount=trade.crypto_amount,
            trade_id=trade.id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
        new_trade_status = TradeStatus.CANCELLED.value
        trade.cancelled_at = datetime.now(timezone.utc)
        trade.cancelled_reason = "dispute:refund_to_seller"
    else:  # SPLIT
        try:
            to_buyer = Decimal(str(amount_to_buyer))
        except Exception:
            raise HTTPException(422, "amount_to_buyer required (decimal)")
        if to_buyer <= 0 or to_buyer >= trade.crypto_amount:
            raise HTTPException(422, "amount_to_buyer must be 0 < x < trade.crypto_amount")
        to_seller = trade.crypto_amount - to_buyer
        # 1) часть buyer'у
        await ledger.release_to_buyer(
            db,
            seller_id=trade.seller_id, buyer_id=trade.buyer_id,
            currency=trade.crypto_currency, amount=to_buyer,
            platform_fee=Decimal("0"),
            trade_id=trade.id, workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
        # 2) остаток обратно seller'у
        await ledger.refund_escrow_to_available(
            db,
            user_id=trade.seller_id, currency=trade.crypto_currency, amount=to_seller,
            trade_id=trade.id, workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
        new_trade_status = TradeStatus.COMPLETED.value
        trade.completed_at = datetime.now(timezone.utc)

    await wallet.update_wallet_from_ledger(db, trade.seller_id, trade.crypto_currency)
    await wallet.update_wallet_from_ledger(db, trade.buyer_id, trade.crypto_currency)

    # State: ARBITRATION → RESOLVED, потом RESOLVED → COMPLETED/CANCELLED
    state.assert_dispute_transition(dispute.status, DisputeStatus.RESOLVED.value)
    dispute.status = DisputeStatus.RESOLVED.value
    dispute.resolution = resolution
    dispute.arbitrator_id = ctx.user_id
    dispute.resolution_note = arbitrator_note
    dispute.resolved_at = datetime.now(timezone.utc)

    if trade.status == TradeStatus.ARBITRATION.value:
        state.assert_trade_transition(trade.status, TradeStatus.RESOLVED.value)
        trade.status = TradeStatus.RESOLVED.value
    state.assert_trade_transition(trade.status, new_trade_status)
    trade.status = new_trade_status
    trade.version += 1
    dispute.version += 1
    await db.flush()

    await audit.log(
        db,
        action="dispute.resolved",
        entity_type="dispute",
        entity_id=dispute.id,
        actor_id=ctx.user_id,
        actor_role=ctx.actor_role,
        new_state={"resolution": resolution, "trade_status": trade.status,
                   "note": arbitrator_note},
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.DISPUTE_RESOLVED.value,
        payload={"dispute_id": dispute.id, "trade_id": trade.id,
                 "resolution": resolution, "arbitrator_id": ctx.user_id,
                 "buyer_id": trade.buyer_id, "seller_id": trade.seller_id},
        aggregate_type="dispute",
        aggregate_id=dispute.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "dispute_id": dispute.id,
        "trade_id": trade.id,
        "trade_status": trade.status,
        "dispute_status": dispute.status,
        "resolution": resolution,
    }

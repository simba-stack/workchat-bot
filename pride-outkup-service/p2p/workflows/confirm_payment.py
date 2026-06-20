"""Workflow: confirm_payment — seller подтверждает получение фиата → release escrow."""
from __future__ import annotations
import logging
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, ledger, locks, outbox, state, wallet
from p2p.enums import TradeStatus, EventType
from p2p.models import P2PTrade, P2PAdvertisement
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.confirm_payment")


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

    if trade.seller_id != ctx.user_id:
        raise HTTPException(403, "only seller can confirm payment")

    if trade.status == TradeStatus.COMPLETED.value:
        # idempotent
        return {"ok": True, "trade_id": trade_id, "status": trade.status,
                "already_completed": True}

    if trade.status not in (TradeStatus.PAYMENT_MARKED.value, TradeStatus.PAYMENT_CONFIRMATION.value):
        raise HTTPException(409, f"cannot confirm in status {trade.status}")

    prev = trade.status

    # ---------- Ledger: trade_escrow → buyer.available, fee → platform_fee ----------
    await locks.lock_user_wallet(db, trade.seller_id, trade.crypto_currency)
    await locks.lock_user_wallet(db, trade.buyer_id, trade.crypto_currency)
    await ledger.release_to_buyer(
        db,
        seller_id=trade.seller_id,
        buyer_id=trade.buyer_id,
        currency=trade.crypto_currency,
        amount=trade.crypto_amount,
        fee=trade.fee_crypto or Decimal("0"),
        trade_id=trade_id,
        workflow_id=ctx.workflow_id,
        correlation_id=ctx.correlation_id,
    )
    await wallet.update_wallet_from_ledger(db, trade.seller_id, trade.crypto_currency)
    await wallet.update_wallet_from_ledger(db, trade.buyer_id, trade.crypto_currency)

    # ---------- Advertisement: reserved_amount → total_amount decrement ----------
    await locks.lock_advertisement(db, trade.advertisement_id)
    ad_r = await db.execute(select(P2PAdvertisement).where(P2PAdvertisement.id == trade.advertisement_id))
    ad = ad_r.scalar_one_or_none()
    if ad:
        ad.reserved_amount = max(Decimal("0"), (ad.reserved_amount or Decimal("0")) - trade.crypto_amount)
        ad.total_amount = max(Decimal("0"), (ad.total_amount or Decimal("0")) - trade.crypto_amount)
        ad.version += 1
        # Auto-archive если полностью продано
        if ad.available_amount <= Decimal("0") and ad.reserved_amount <= Decimal("0"):
            ad.status = "ARCHIVED"

    # ---------- State ----------
    state.assert_trade_transition(trade.status, TradeStatus.COMPLETED.value)
    trade.status = TradeStatus.COMPLETED.value
    trade.completed_at = datetime.now(timezone.utc)
    trade.version += 1
    await db.flush()

    await audit.log(
        db,
        action="trade.completed",
        entity_type="trade",
        entity_id=trade_id,
        actor_id=ctx.user_id,
        previous_state={"status": prev},
        new_state={"status": trade.status, "completed_at": trade.completed_at.isoformat()},
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.TRADE_COMPLETED.value,
        payload={
            "trade_id": trade_id, "buyer_id": trade.buyer_id, "seller_id": trade.seller_id,
            "crypto": trade.crypto_currency, "amount": str(trade.crypto_amount),
        },
        aggregate_type="trade",
        aggregate_id=trade_id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {"ok": True, "trade_id": trade_id, "status": trade.status}

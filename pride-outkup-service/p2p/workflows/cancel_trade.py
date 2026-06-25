"""Workflow: cancel_trade — отмена trade с возвратом escrow."""
from __future__ import annotations
import logging
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, ledger, locks, outbox, state, wallet
from p2p.enums import TradeStatus, AdvertisementType, EventType
from p2p.models import P2PTrade, P2PAdvertisement
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.cancel_trade")


async def handle(ctx: WorkflowContext) -> dict:
    p = ctx.input_payload
    db = ctx.db

    trade_id = p.get("trade_id")
    reason = (p.get("reason") or "")[:200]
    if not trade_id:
        raise HTTPException(422, "trade_id required")

    await locks.lock_trade(db, trade_id)
    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = r.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    if trade.status == TradeStatus.CANCELLED.value:
        return {"ok": True, "trade_id": trade_id, "status": trade.status,
                "already_cancelled": True}

    if ctx.user_id not in (trade.buyer_id, trade.seller_id):
        raise HTTPException(403, "not a participant")

    # Cancellation policy: только до payment_marked буером свободно;
    # после payment_marked — только seller или admin
    if trade.status == TradeStatus.PAYMENT_MARKED.value:
        # Анти-скам: после отметки об оплате НИКТО не отменяет в одностороннем порядке
        # (иначе продавец-получатель фиата отменит сделку и заберёт эскроу себе).
        # Спорные ситуации решаются только через спор/арбитраж.
        raise HTTPException(409, "нельзя отменить сделку после отметки об оплате — откройте спор")
    if trade.status in (TradeStatus.PAYMENT_CONFIRMATION.value,
                        TradeStatus.DISPUTE_OPENED.value,
                        TradeStatus.ARBITRATION.value,
                        TradeStatus.RESOLVED.value,
                        TradeStatus.COMPLETED.value):
        raise HTTPException(409, f"cannot cancel in status {trade.status}")

    prev = trade.status

    # ---------- Refund escrow ----------
    await locks.lock_user_wallet(db, trade.seller_id, trade.crypto_currency)
    await locks.lock_advertisement(db, trade.advertisement_id)
    ad_r = await db.execute(select(P2PAdvertisement).where(P2PAdvertisement.id == trade.advertisement_id))
    ad = ad_r.scalar_one_or_none()

    if ad and ad.type == AdvertisementType.SELL.value:
        # Возвращаем в ad_hold (объявление продолжает работать)
        await ledger.refund_escrow_to_ad_hold(
            db,
            user_id=trade.seller_id,
            amount=trade.crypto_amount,
            trade_id=trade_id,
            currency=trade.crypto_currency,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
        if ad:
            ad.available_amount = (ad.available_amount + trade.crypto_amount)
            ad.reserved_amount = max(Decimal("0"), (ad.reserved_amount - trade.crypto_amount))
            # Stats: cancelled_count (TODO #5)
            ad.cancelled_count = int(ad.cancelled_count or 0) + 1
            ad.version += 1
    else:
        # BUY-ad: возврат seller'у в available
        await ledger.refund_escrow_to_available(
            db,
            user_id=trade.seller_id,
            currency=trade.crypto_currency,
            amount=trade.crypto_amount,
            trade_id=trade_id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
        if ad:
            ad.cancelled_count = int(ad.cancelled_count or 0) + 1
            ad.version += 1

    await wallet.update_wallet_from_ledger(db, trade.seller_id, trade.crypto_currency)

    # ---------- State ----------
    state.assert_trade_transition(trade.status, TradeStatus.CANCELLED.value)
    trade.status = TradeStatus.CANCELLED.value
    trade.cancelled_at = datetime.now(timezone.utc)
    trade.cancelled_reason = reason
    trade.version += 1
    await db.flush()

    await audit.log(
        db,
        action="trade.cancelled",
        entity_type="trade",
        entity_id=trade_id,
        actor_id=ctx.user_id,
        previous_state={"status": prev},
        new_state={"status": trade.status, "reason": reason},
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.TRADE_CANCELLED.value,
        payload={"trade_id": trade_id, "by_user_id": ctx.user_id,
                 "buyer_id": trade.buyer_id, "seller_id": trade.seller_id,
                 "reason": reason},
        aggregate_type="trade",
        aggregate_id=trade_id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {"ok": True, "trade_id": trade_id, "status": trade.status}

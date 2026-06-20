"""Workflow: create_trade (buyer opens trade against existing ad)."""
from __future__ import annotations
import logging
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, ledger, locks, outbox, policies, state, wallet
from p2p.enums import (
    TradeStatus, AdvertisementStatus, AdvertisementType, EventType,
)
from p2p.models import P2PAdvertisement, P2PTrade
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.create_trade")


async def handle(ctx: WorkflowContext) -> dict:
    """Открыть trade. Берём advertisement, считаем цену, escrow lock у seller'а."""
    p = ctx.input_payload
    db = ctx.db

    ad_id = p.get("advertisement_id")
    if not ad_id:
        raise HTTPException(422, "advertisement_id required")
    try:
        amount_crypto = Decimal(str(p.get("amount_crypto", "0")))
    except Exception:
        raise HTTPException(422, "amount_crypto must be decimal")
    if amount_crypto <= 0:
        raise HTTPException(422, "amount_crypto must be > 0")

    payment_method_type = p.get("payment_method_type")
    if not payment_method_type:
        raise HTTPException(422, "payment_method_type required")

    # ---------- Lock advertisement ----------
    ctx.step("ad.lock")
    await locks.lock_advertisement(db, ad_id)
    r = await db.execute(select(P2PAdvertisement).where(P2PAdvertisement.id == ad_id))
    ad = r.scalar_one_or_none()
    if not ad:
        raise HTTPException(404, "advertisement not found")
    if ad.status != AdvertisementStatus.ACTIVE.value:
        raise HTTPException(409, f"advertisement not active (status={ad.status})")
    if ad.owner_id == ctx.user_id:
        raise HTTPException(403, "cannot trade your own advertisement")

    if amount_crypto > ad.amount_available:
        raise HTTPException(400, f"requested {amount_crypto} > available {ad.amount_available}")

    if payment_method_type not in (ad.payment_methods or []):
        raise HTTPException(422, "payment_method_type not allowed by ad")

    # ---------- Цена и fiat amount ----------
    ctx.step("price.calc")
    if ad.pricing_mode == "FIXED":
        price = ad.price_fixed
    else:
        # Floating: пока берём market_index если есть, иначе fixed fallback
        from core.services import settings_kv
        try:
            rate = await settings_kv.get_rate_buy(db) if ad.type == AdvertisementType.SELL.value else await settings_kv.get_rate_sell(db)
            margin = (ad.price_margin_pct or Decimal("0"))
            price = rate * (Decimal("1") + margin / Decimal("100"))
        except Exception:
            raise HTTPException(500, "cannot determine floating price")
    amount_fiat = (amount_crypto * price).quantize(Decimal("0.01"))

    # Min/max checks
    if amount_fiat < ad.min_order_fiat or amount_fiat > ad.max_order_fiat:
        raise HTTPException(400, f"fiat amount {amount_fiat} out of range [{ad.min_order_fiat}; {ad.max_order_fiat}]")

    # ---------- Определить buyer/seller ----------
    if ad.type == AdvertisementType.SELL.value:
        seller_id = ad.owner_id
        buyer_id = ctx.user_id
    else:
        seller_id = ctx.user_id
        buyer_id = ad.owner_id

    # ---------- Лимит активных trades у инициатора ----------
    max_active = await policies.get_int(db, "MAX_ACTIVE_TRADES_PER_USER")
    r = await db.execute(
        select(P2PTrade).where(
            P2PTrade.buyer_id == ctx.user_id,
            P2PTrade.status.in_([
                TradeStatus.CREATED.value, TradeStatus.ESCROW_LOCKED.value,
                TradeStatus.WAITING_FOR_PAYMENT.value, TradeStatus.PAYMENT_MARKED.value,
                TradeStatus.PAYMENT_CONFIRMATION.value, TradeStatus.DISPUTE_OPENED.value,
                TradeStatus.ARBITRATION.value,
            ])
        )
    )
    if len(r.scalars().all()) >= max_active:
        raise HTTPException(409, f"too many active trades (max {max_active})")

    # ---------- Платформенный fee ----------
    fee_pct = await policies.get_decimal(db, "PLATFORM_FEE_PCT")
    platform_fee_crypto = (amount_crypto * fee_pct / Decimal("100")).quantize(Decimal("0.000001"))

    # ---------- Создать trade ----------
    ctx.step("trade.create")
    pay_timeout_min = await policies.get_int(db, "TRADE_PAYMENT_TIMEOUT_MIN")
    trade = P2PTrade(
        advertisement_id=ad_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        crypto=ad.crypto,
        fiat=ad.fiat,
        amount_crypto=amount_crypto,
        price=price,
        amount_fiat=amount_fiat,
        platform_fee_crypto=platform_fee_crypto,
        payment_method_type=payment_method_type,
        status=TradeStatus.CREATED.value,
        version=1,
        pay_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=pay_timeout_min),
        idempotency_key=ctx.idempotency_key,
    )
    db.add(trade)
    await db.flush()

    # ---------- Уменьшить available на advertisement ----------
    ctx.step("ad.reserve")
    ad.amount_available = (ad.amount_available - amount_crypto).quantize(Decimal("0.000001"))
    ad.amount_reserved = (ad.amount_reserved + amount_crypto).quantize(Decimal("0.000001"))
    ad.version += 1
    await db.flush()

    # ---------- Ledger: ad_hold → trade_escrow у seller'а ----------
    ctx.step("ledger.escrow")
    await locks.lock_user_wallet(db, seller_id, ad.crypto)
    if ad.type == AdvertisementType.SELL.value:
        # Seller уже зарезервировал в advertisement_hold — двигаем в trade_escrow
        await ledger.move_ad_hold_to_escrow(
            db,
            user_id=seller_id,
            currency=ad.crypto,
            amount=amount_crypto,
            advertisement_id=ad_id,
            trade_id=trade.id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
    else:
        # Объявление BUY — seller это taker, надо заморозить из available
        breakdown = await wallet.get_balance_breakdown(db, seller_id, ad.crypto)
        if breakdown.available < amount_crypto:
            raise HTTPException(400, f"seller insufficient available: {breakdown.available}")
        await ledger.reserve_seller_escrow(
            db,
            user_id=seller_id,
            currency=ad.crypto,
            amount=amount_crypto,
            trade_id=trade.id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
    await wallet.update_wallet_from_ledger(db, seller_id, ad.crypto)

    # ---------- State: CREATED → ESCROW_LOCKED → WAITING_FOR_PAYMENT ----------
    ctx.step("state.transition")
    state.assert_trade_transition(trade.status, TradeStatus.ESCROW_LOCKED.value)
    trade.status = TradeStatus.ESCROW_LOCKED.value
    state.assert_trade_transition(trade.status, TradeStatus.WAITING_FOR_PAYMENT.value)
    trade.status = TradeStatus.WAITING_FOR_PAYMENT.value
    trade.version += 1
    await db.flush()

    # ---------- Audit ----------
    await audit.log(
        db,
        action="trade.created",
        entity_type="trade",
        entity_id=trade.id,
        actor_id=ctx.user_id,
        actor_role=ctx.actor_role,
        new_state={
            "advertisement_id": ad_id, "buyer_id": buyer_id, "seller_id": seller_id,
            "amount_crypto": str(amount_crypto), "amount_fiat": str(amount_fiat),
            "price": str(price), "status": trade.status,
        },
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )

    await outbox.emit(
        db,
        event_type=EventType.TRADE_CREATED.value,
        payload={
            "trade_id": trade.id, "advertisement_id": ad_id,
            "buyer_id": buyer_id, "seller_id": seller_id,
            "crypto": ad.crypto, "fiat": ad.fiat,
            "amount_crypto": str(amount_crypto), "amount_fiat": str(amount_fiat),
        },
        aggregate_type="trade",
        aggregate_id=trade.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "trade_id": trade.id,
        "status": trade.status,
        "amount_crypto": str(trade.amount_crypto),
        "amount_fiat": str(trade.amount_fiat),
        "price": str(trade.price),
        "pay_deadline_at": trade.pay_deadline_at.isoformat() if trade.pay_deadline_at else None,
    }

"""Workflow: create_trade (buyer opens trade against existing ad)."""
from __future__ import annotations
import logging
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from sqlalchemy import select, func

from p2p import audit, ledger, locks, outbox, policies, risk, state, wallet
from p2p.enums import (
    TradeStatus, AdvertisementStatus, AdvertisementType, EventType,
    RiskDecision,
)
from p2p.models import P2PAdvertisement, P2PTrade
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.create_trade")


async def _gen_trade_number(db) -> str:
    """T-YYYY-NNNNN — последовательный номер на основе COUNT(*)+1 за все годы."""
    r = await db.execute(select(func.count(P2PTrade.id)))
    n = (r.scalar() or 0) + 1
    return f"T-{datetime.now(timezone.utc).year}-{n:05d}"


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

    # payment_method_id — UUID FK на p2p_payment_methods (опционально на этом этапе)
    payment_method_id = p.get("payment_method_id") or p.get("payment_method_type")

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

    if amount_crypto > ad.available_amount:
        raise HTTPException(400, f"requested {amount_crypto} > available {ad.available_amount}")

    # ---------- Цена и fiat amount ----------
    ctx.step("price.calc")
    if ad.pricing_mode == "FIXED":
        price = ad.price
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
    if amount_fiat < ad.min_amount_fiat or amount_fiat > ad.max_amount_fiat:
        raise HTTPException(400, f"fiat amount {amount_fiat} out of range [{ad.min_amount_fiat}; {ad.max_amount_fiat}]")

    # ---------- Risk Engine pre-check (ТЗ Том 10) ----------
    ctx.step("risk.assess")
    try:
        r_assess = await risk.assess_create_trade(
            db,
            user_id=ctx.user_id,
            amount_crypto=amount_crypto,
            amount_fiat=amount_fiat,
            advertisement_id=ad_id,
            currency=ad.crypto_currency,
        )
    except Exception as _risk_e:
        logger.warning("[risk] assess_create_trade raised, defaulting to ALLOW: %s", _risk_e)
        r_assess = None
    if r_assess is not None:
        if r_assess.decision == RiskDecision.DENY.value:
            if r_assess.should_freeze:
                try:
                    await risk.freeze_user(
                        db, user_id=ctx.user_id,
                        reason=",".join(r_assess.reasons)[:200] or "risk:deny",
                        by_user_id=None,
                    )
                except Exception as _fz_e:
                    logger.warning("[risk] freeze_user failed: %s", _fz_e)
            raise HTTPException(403, f"Risk DENY: {','.join(r_assess.reasons)}")
        if r_assess.decision == RiskDecision.REVIEW.value:
            logger.warning(
                "[risk] REVIEW trade for user=%s score=%s reasons=%s",
                ctx.user_id, r_assess.score, r_assess.reasons,
            )

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
    fee_crypto = (amount_crypto * fee_pct / Decimal("100")).quantize(Decimal("0.000001"))

    # ---------- Создать trade ----------
    ctx.step("trade.create")
    pay_timeout_min = await policies.get_int(db, "TRADE_PAYMENT_TIMEOUT_MIN")
    trade_number = await _gen_trade_number(db)
    trade = P2PTrade(
        trade_number=trade_number,
        advertisement_id=ad_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        crypto_currency=ad.crypto_currency,
        fiat_currency=ad.fiat_currency,
        crypto_amount=amount_crypto,
        price=price,
        fiat_amount=amount_fiat,
        fee_pct=fee_pct,
        fee_crypto=fee_crypto,
        payment_method_id=payment_method_id,
        payment_snapshot={},
        status=TradeStatus.CREATED.value,
        version=1,
        pay_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=pay_timeout_min),
        workflow_id=ctx.workflow_id,
        correlation_id=ctx.correlation_id,
    )
    db.add(trade)
    await db.flush()

    # ---------- Уменьшить available на advertisement ----------
    ctx.step("ad.reserve")
    ad.available_amount = (ad.available_amount - amount_crypto).quantize(Decimal("0.000001"))
    ad.reserved_amount = (ad.reserved_amount + amount_crypto).quantize(Decimal("0.000001"))
    ad.version += 1
    await db.flush()

    # ---------- Ledger: ad_hold → trade_escrow у seller'а ----------
    ctx.step("ledger.escrow")
    await locks.lock_user_wallet(db, seller_id, ad.crypto_currency)
    if ad.type == AdvertisementType.SELL.value:
        # Seller уже зарезервировал в advertisement_hold — двигаем в trade_escrow
        await ledger.move_ad_hold_to_escrow(
            db,
            user_id=seller_id,
            currency=ad.crypto_currency,
            amount=amount_crypto,
            advertisement_id=ad_id,
            trade_id=trade.id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
    else:
        # Объявление BUY — seller это taker, надо заморозить из available
        breakdown = await wallet.get_breakdown(db, seller_id, ad.crypto_currency)
        if breakdown.available < amount_crypto:
            raise HTTPException(400, f"seller insufficient available: {breakdown.available}")
        await ledger.reserve_seller_escrow(
            db,
            user_id=seller_id,
            currency=ad.crypto_currency,
            amount=amount_crypto,
            trade_id=trade.id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
    await wallet.update_wallet_from_ledger(db, seller_id, ad.crypto_currency)

    # ---------- State: CREATED → ESCROW_LOCKED → WAITING_FOR_PAYMENT ----------
    ctx.step("state.transition")
    state.assert_trade_transition(trade.status, TradeStatus.ESCROW_LOCKED.value)
    trade.status = TradeStatus.ESCROW_LOCKED.value
    trade.escrow_locked_at = datetime.now(timezone.utc)
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
            "crypto_amount": str(amount_crypto), "fiat_amount": str(amount_fiat),
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
            "crypto": ad.crypto_currency, "fiat": ad.fiat_currency,
            "crypto_amount": str(amount_crypto), "fiat_amount": str(amount_fiat),
        },
        aggregate_type="trade",
        aggregate_id=trade.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "trade_id": trade.id,
        "trade_number": trade.trade_number,
        "status": trade.status,
        "crypto_amount": str(trade.crypto_amount),
        "fiat_amount": str(trade.fiat_amount),
        "price": str(trade.price),
        "pay_deadline_at": trade.pay_deadline_at.isoformat() if trade.pay_deadline_at else None,
    }

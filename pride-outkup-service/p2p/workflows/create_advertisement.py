"""Workflow: create_advertisement.

Создание объявления (advertisement) — резервирование USDT в Ledger,
создание записи в p2p_advertisements, emit event.
"""
from __future__ import annotations
import logging
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, ledger, locks, outbox, policies, risk, state, wallet
from p2p.enums import (
    AdvertisementType, AdvertisementStatus, PricingMode,
    EventType, AdvertisementStatus as AS, RiskDecision,
)
from p2p.models import P2PAdvertisement, P2PWallet
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.create_ad")


async def handle(ctx: WorkflowContext) -> dict:
    """Создать advertisement. Для SELL: заморозить amount_usdt в Ledger."""
    p = ctx.input_payload
    db = ctx.db

    # ---------- Валидация полей ----------
    # Нормализуем регистр (enum хранит BUY/SELL uppercase; фронт может слать любой регистр)
    ad_type = str(p.get("type") or "").strip().upper()
    if ad_type not in (AdvertisementType.BUY.value, AdvertisementType.SELL.value):
        raise HTTPException(422, "type must be 'BUY' or 'SELL'")

    crypto = (p.get("crypto") or "USDT").upper()
    fiat = (p.get("fiat") or "RUB").upper()

    try:
        amount_total = Decimal(str(p.get("amount_total", "0")))
        min_order = Decimal(str(p.get("min_order_fiat", "0")))
        max_order = Decimal(str(p.get("max_order_fiat", "0")))
    except Exception:
        raise HTTPException(422, "amounts must be decimals")

    if amount_total <= 0:
        raise HTTPException(422, "amount_total must be > 0")
    if min_order <= 0 or max_order < min_order:
        raise HTTPException(422, "invalid order range")

    pricing_mode = (p.get("pricing_mode") or "FIXED").upper()
    if pricing_mode not in (PricingMode.FIXED.value, PricingMode.FLOATING.value):
        raise HTTPException(422, "pricing_mode invalid")

    price_value: Decimal | None = None
    price_margin = None
    if pricing_mode == PricingMode.FIXED.value:
        try:
            price_value = Decimal(str(p.get("price_fixed", "0")))
        except Exception:
            raise HTTPException(422, "price_fixed required")
        if price_value <= 0:
            raise HTTPException(422, "price_fixed must be > 0")
    else:
        if not await policies.get_bool(db, "ALLOW_FLOATING_PRICE"):
            raise HTTPException(403, "floating pricing disabled")
        try:
            price_margin = Decimal(str(p.get("price_margin_pct", "0")))
        except Exception:
            raise HTTPException(422, "price_margin_pct invalid")
        # Для FLOATING — снапшот текущей цены; если не передан, 0 (обновится позже триггером цен)
        try:
            price_value = Decimal(str(p.get("price_fixed", "0")))
        except Exception:
            price_value = Decimal("0")

    # ---------- Лимиты ----------
    ctx.step("limits.check")
    max_active = await policies.get_int(db, "MAX_ACTIVE_ADVERTISEMENTS")
    r = await db.execute(
        select(P2PAdvertisement).where(
            P2PAdvertisement.owner_id == ctx.user_id,
            P2PAdvertisement.status == AS.ACTIVE.value,
        )
    )
    active_count = len(r.scalars().all())
    if active_count >= max_active:
        raise HTTPException(409, f"max active advertisements reached: {max_active}")

    # ---------- Risk Engine pre-check (ТЗ Том 10) ----------
    ctx.step("risk.assess")
    try:
        r_assess = await risk.assess_create_advertisement(
            db,
            user_id=ctx.user_id,
            amount_total=amount_total,
            price=price_value if price_value is not None else Decimal("0"),
            type=ad_type,
            currency_pair=(crypto, fiat),
        )
    except Exception as _risk_e:
        logger.warning("[risk] assess_create_advertisement raised, defaulting to ALLOW: %s", _risk_e)
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
                "[risk] REVIEW advertisement for user=%s score=%s reasons=%s",
                ctx.user_id, r_assess.score, r_assess.reasons,
            )

    # ---------- Создаём advertisement (DRAFT) ----------
    ctx.step("advertisement.create")
    ad = P2PAdvertisement(
        owner_id=ctx.user_id,
        type=ad_type,
        crypto_currency=crypto,
        fiat_currency=fiat,
        total_amount=amount_total,
        available_amount=amount_total,
        reserved_amount=Decimal("0"),
        min_amount_fiat=min_order,
        max_amount_fiat=max_order,
        pricing_mode=pricing_mode,
        price=price_value if price_value is not None else Decimal("0"),
        price_margin_pct=price_margin,
        payment_method_ids=p.get("payment_methods") or [],
        pay_window_min=int(p.get("time_limit_minutes", 15)),
        require_verified_taker=bool(p.get("require_kyc", False)),
        min_taker_completed=int(p.get("min_completed_trades", 0)),
        description=p.get("terms_text"),
        merchant_note=p.get("auto_reply_text"),
        status=AS.DRAFT.value,
        version=1,
    )
    db.add(ad)
    await db.flush()

    # ---------- Резервирование USDT (только для SELL) ----------
    if ad_type == AdvertisementType.SELL.value:
        ctx.step("ledger.reserve")
        await locks.lock_user_wallet(db, ctx.user_id, crypto)
        # Чек available баланса
        breakdown = await wallet.get_breakdown(db, ctx.user_id, crypto)
        if breakdown.available < amount_total:
            raise HTTPException(
                400,
                f"insufficient balance: available={breakdown.available} need={amount_total}",
            )
        await ledger.reserve_for_advertisement(
            db,
            user_id=ctx.user_id,
            currency=crypto,
            amount=amount_total,
            advertisement_id=ad.id,
            workflow_id=ctx.workflow_id,
            correlation_id=ctx.correlation_id,
        )
        await wallet.update_wallet_from_ledger(db, ctx.user_id, crypto)

    # ---------- Переход DRAFT → ACTIVE ----------
    ctx.step("state.activate")
    state.assert_advertisement_transition(ad.status, AS.ACTIVE.value)
    ad.status = AS.ACTIVE.value
    await db.flush()

    # ---------- Audit ----------
    await audit.log(
        db,
        action="advertisement.created",
        entity_type="advertisement",
        entity_id=ad.id,
        actor_id=ctx.user_id,
        actor_role=ctx.actor_role,
        new_state={"type": ad_type, "amount_total": str(amount_total), "status": ad.status},
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )

    # ---------- Outbox event ----------
    await outbox.emit(
        db,
        event_type=EventType.ADVERTISEMENT_CREATED.value,
        payload={"advertisement_id": ad.id, "owner_id": ctx.user_id, "type": ad_type,
                 "crypto": crypto, "fiat": fiat, "amount": str(amount_total)},
        aggregate_type="advertisement",
        aggregate_id=ad.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "advertisement_id": ad.id,
        "status": ad.status,
        "amount_total": str(ad.total_amount),
        "amount_available": str(ad.available_amount),
    }

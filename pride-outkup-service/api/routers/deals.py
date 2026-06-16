"""V2 P2P Deals — industrial сделки с escrow + anti-fraud.

Поддерживает:
- escrow lock мейкера при создании
- pay_window_min из оффера (15..120 мин)
- float pricing live calc на момент сделки
- anti-fraud: max 3 active, cancel cooldown 24h после 3 cancels
- counterparty conditions: KYC, min_completed_deals
- auto-reply мейкера в DealMessage при создании
- chat (DealMessage) с TG notify
- dispute с заморозкой escrow
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user, require_verified
from core.db import get_db
from core.models import Deal, Dispute, Offer, User
from core.services import escrow_service, jarvis_sync, maker_stats, settings_kv

router = APIRouter()


async def _next_deal_number(db: AsyncSession) -> str:
    res = await db.execute(select(Deal.id).order_by(desc(Deal.id)).limit(1))
    last = res.scalar() or 0
    return f"dl{last + 1:05d}"


def _deal_to_dict(d: Deal) -> dict:
    return {
        "id": d.id,
        "deal_number": d.deal_number,
        "offer_id": d.offer_id,
        "buyer_id": d.buyer_id,
        "seller_id": d.seller_id,
        "coin": d.coin or "USDT",
        "fiat": d.fiat or "RUB",
        "amount_rub": float(d.amount_rub),
        "rate_rub_per_usdt": float(d.rate_rub_per_usdt),
        "amount_usdt": float(d.amount_usdt),
        "payment_method": d.payment_method,
        "bank": d.bank,
        "phone_or_card": d.phone_or_card,
        "receiver_name": d.receiver_name,
        "status": d.status,
        "receipt_url": d.receipt_url,
        "txid": d.txid,
        "fee_usdt": float(d.fee_usdt),
        "fee_pct": float(d.fee_pct),
        "expires_at": d.expires_at.isoformat() if d.expires_at else None,
        "pay_deadline_at": d.pay_deadline_at.isoformat() if d.pay_deadline_at else None,
        "paid_at": d.paid_at.isoformat() if d.paid_at else None,
        "released_at": d.released_at.isoformat() if d.released_at else None,
        "created_at": d.created_at.isoformat(),
    }


@router.post("")
async def create_deal(
    payload: dict,
    user: User = Depends(require_verified),
    db: AsyncSession = Depends(get_db),
):
    """Buyer создаёт сделку по offer.

    payload: {offer_id, amount_rub, payment_method, bank?, phone_or_card?, receiver_name?}
    """
    offer_id = int(payload.get("offer_id") or 0)
    o = await db.get(Offer, offer_id)
    if not o:
        raise HTTPException(404, "offer not found")
    if o.status != "active":
        raise HTTPException(400, "оффер не активен")
    if o.user_id == user.id:
        raise HTTPException(400, "нельзя торговать со своим оффером")

    ok, err = await maker_stats.can_take_deal(db, user)
    if not ok:
        raise HTTPException(429, err or "anti-fraud lock")
    ok2, err2 = await maker_stats.check_taker_meets_offer_conditions(db, user, o)
    if not ok2:
        raise HTTPException(403, err2 or "counterparty conditions not met")

    try:
        amount_rub = Decimal(str(payload.get("amount_rub") or 0))
    except Exception:
        raise HTTPException(400, "bad amount_rub")
    if amount_rub < o.min_amount_rub or amount_rub > o.max_amount_rub:
        raise HTTPException(
            400,
            f"сумма {float(o.min_amount_rub)}–{float(o.max_amount_rub)} {o.fiat or 'RUB'}",
        )

    payment_method = (payload.get("payment_method") or "").strip()
    if payment_method not in (o.payment_methods or []):
        raise HTTPException(400, "выберите метод из оффера")

    # Эффективная цена: для float — пересчёт по индексу
    from core.services import price_index as pi_svc
    if (o.price_type or "fixed") == "float" and o.float_margin_pct:
        live = await pi_svc.compute_float_price(db, o.coin or "USDT", o.fiat or "RUB", o.float_margin_pct)
        rate_used = live or o.rate_rub_per_usdt
    else:
        rate_used = o.rate_rub_per_usdt
    amount_usdt = (amount_rub / rate_used).quantize(Decimal("0.0001"))
    fee_pct = await settings_kv.get_fee_v2_pct(db)
    fee_usdt = (amount_usdt * fee_pct / 100).quantize(Decimal("0.0001"))

    if o.side == "sell":
        buyer_id, seller_id = user.id, o.user_id
    else:
        buyer_id, seller_id = o.user_id, user.id

    seller = await db.get(User, seller_id)
    if not seller or seller.balance_usdt < amount_usdt:
        raise HTTPException(400, "у продавца недостаточно USDT для escrow")

    # Реквизиты — из UserPaymentMethod продавца (а не из payload buyer'а!).
    # Buyer указывает только метод оплаты, реквизиты подставляет seller.
    from core.models import UserPaymentMethod
    res_pm = await db.execute(
        select(UserPaymentMethod)
        .where(UserPaymentMethod.user_id == seller_id)
        .where(UserPaymentMethod.type == payment_method)
        .where(UserPaymentMethod.is_active == True)  # noqa: E712
        .order_by(UserPaymentMethod.id.desc())
        .limit(1)
    )
    seller_pm = res_pm.scalar_one_or_none()
    if seller_pm:
        deal_bank = seller_pm.bank_name
        deal_card = seller_pm.card_or_phone
        deal_name = seller_pm.receiver_name
    else:
        # Fallback: если у продавца не настроены реквизиты — берём из payload (legacy).
        # В будущем будем требовать предварительной настройки реквизитов перед публикацией оффера.
        deal_bank = (payload.get("bank") or "").strip() or None
        deal_card = (payload.get("phone_or_card") or "").strip() or None
        deal_name = (payload.get("receiver_name") or "").strip() or None

    pay_window = o.pay_window_min or 30
    deadline = datetime.now(timezone.utc) + timedelta(minutes=pay_window)
    deal = Deal(
        deal_number=await _next_deal_number(db),
        offer_id=o.id,
        buyer_id=buyer_id, seller_id=seller_id,
        coin=o.coin or "USDT", fiat=o.fiat or "RUB",
        amount_rub=amount_rub,
        rate_rub_per_usdt=rate_used,
        amount_usdt=amount_usdt,
        payment_method=payment_method,
        bank=deal_bank,
        phone_or_card=deal_card,
        receiver_name=deal_name,
        status="awaiting_payment",
        fee_pct=fee_pct, fee_usdt=fee_usdt,
        expires_at=deadline, pay_deadline_at=deadline,
    )
    db.add(deal)
    await db.flush()
    await escrow_service.lock(db, seller, deal)

    # Auto-reply мейкера
    try:
        if o.auto_reply:
            from core.models import DealMessage
            db.add(DealMessage(
                deal_id=deal.id, from_user_id=o.user_id,
                text=o.auto_reply[:2000], is_system=False,
            ))
    except Exception:
        pass

    try:
        await jarvis_sync.send_event("deal_created", {
            "deal_id": deal.id, "deal_number": deal.deal_number,
            "buyer_id": buyer_id, "seller_id": seller_id,
            "amount_rub": float(amount_rub), "amount_usdt": float(amount_usdt),
        })
    except Exception:
        pass

    return {"ok": True, "deal": _deal_to_dict(deal)}


@router.get("/me")
async def list_my_deals(
    role: str = "all",
    status_: str | None = None,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(Deal)
    if role == "buyer":
        q = q.where(Deal.buyer_id == user.id)
    elif role == "seller":
        q = q.where(Deal.seller_id == user.id)
    else:
        q = q.where(or_(Deal.buyer_id == user.id, Deal.seller_id == user.id))
    if status_:
        q = q.where(Deal.status == status_)
    q = q.order_by(desc(Deal.created_at)).limit(max(1, min(limit, 200)))
    res = await db.execute(q)
    items = res.scalars().all()
    return {"items": [_deal_to_dict(d) for d in items], "count": len(items)}


@router.get("/{deal_id}")
async def get_deal(
    deal_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(Deal, deal_id)
    if not d or user.id not in (d.buyer_id, d.seller_id):
        raise HTTPException(404, "deal not found")
    return {"deal": _deal_to_dict(d)}


@router.post("/{deal_id}/mark_paid")
async def mark_paid(
    deal_id: int,
    payload: dict | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Buyer отмечает оплату."""
    d = await db.get(Deal, deal_id)
    if not d or d.buyer_id != user.id:
        raise HTTPException(404, "deal not found")
    if d.status != "awaiting_payment":
        raise HTTPException(400, "сделка не в awaiting_payment")
    d.status = "paid"
    d.paid_at = datetime.now(timezone.utc)
    if payload and payload.get("receipt_url"):
        d.receipt_url = payload["receipt_url"]
    await db.flush()
    try:
        await jarvis_sync.send_event("deal_marked_paid", {
            "deal_id": d.id, "deal_number": d.deal_number,
            "receipt_url": d.receipt_url,
        })
    except Exception:
        pass
    # Bot-notify продавцу
    try:
        from bot.main import notify_user
        seller = await db.get(User, d.seller_id)
        if seller:
            await notify_user(
                seller.tg_id,
                f"💰 <b>Покупатель оплатил сделку #{d.deal_number}</b>\n"
                f"{float(d.amount_rub)} {d.fiat} → {float(d.amount_usdt)} {d.coin}\n"
                f"Проверьте поступление средств и отпустите USDT.",
            )
    except Exception:
        pass
    return {"ok": True, "status": d.status}


@router.post("/{deal_id}/release")
async def release_deal(
    deal_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Seller подтверждает что деньги получены → release escrow."""
    d = await db.get(Deal, deal_id)
    if not d or d.seller_id != user.id:
        raise HTTPException(404, "deal not found")
    if d.status != "paid":
        raise HTTPException(400, "deal не в статусе paid")
    await escrow_service.release(db, d)
    await db.flush()
    try:
        await jarvis_sync.send_event("deal_released", {
            "deal_id": d.id, "deal_number": d.deal_number,
        })
    except Exception:
        pass
    # Bot-notify покупателю
    try:
        from bot.main import notify_user
        buyer = await db.get(User, d.buyer_id)
        if buyer:
            await notify_user(
                buyer.tg_id,
                f"✅ <b>Сделка #{d.deal_number} завершена!</b>\n"
                f"Получено: <b>{float(d.amount_usdt)} {d.coin}</b>\n"
                f"Продавец подтвердил оплату.",
            )
    except Exception:
        pass
    return {"ok": True, "status": d.status}


@router.post("/{deal_id}/cancel")
async def cancel_deal(
    deal_id: int,
    payload: dict | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Отменить ДО оплаты (после — открывайте dispute)."""
    d = await db.get(Deal, deal_id)
    if not d or user.id not in (d.buyer_id, d.seller_id):
        raise HTTPException(404, "deal not found")
    if d.status not in ("awaiting_payment",):
        raise HTTPException(400, "уже оплачено — открывайте dispute")
    reason = ((payload or {}).get("reason") or "user_cancelled")[:256]
    d.status = "cancelled"
    d.cancelled_at = datetime.now(timezone.utc)
    d.cancelled_reason = reason
    await escrow_service.refund(db, d, reason)
    await db.flush()
    # Bot-notify другой стороне
    try:
        from bot.main import notify_user
        other_id = d.seller_id if user.id == d.buyer_id else d.buyer_id
        other = await db.get(User, other_id)
        if other:
            who = "покупатель" if user.id == d.buyer_id else "продавец"
            await notify_user(
                other.tg_id,
                f"❌ <b>Сделка #{d.deal_number} отменена</b>\n"
                f"Отменил {who}. Причина: {reason[:120]}\n"
                f"Escrow возвращён продавцу.",
            )
    except Exception:
        pass
    return {"ok": True, "status": d.status}


# ─── Mini-App v3 aliases ────────────────────────────────────────────────
@router.post("/create")
async def deal_create_v3(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """payload: {offer_id, amount_fiat, payment_method?}"""
    offer_id = int(payload.get("offer_id") or 0)
    o = await db.get(Offer, offer_id)
    if not o or o.status != "active":
        raise HTTPException(404, "offer not active")
    if o.user_id == user.id:
        raise HTTPException(400, "нельзя торговать со своим оффером")

    ok, err = await maker_stats.can_take_deal(db, user)
    if not ok:
        raise HTTPException(429, err or "anti-fraud lock")
    ok2, err2 = await maker_stats.check_taker_meets_offer_conditions(db, user, o)
    if not ok2:
        raise HTTPException(403, err2 or "counterparty conditions not met")

    try:
        amount_rub = Decimal(str(payload.get("amount_fiat") or 0))
    except Exception:
        raise HTTPException(400, "bad amount_fiat")
    if amount_rub < o.min_amount_rub or amount_rub > o.max_amount_rub:
        raise HTTPException(400, f"сумма {float(o.min_amount_rub)}–{float(o.max_amount_rub)}")

    pms = o.payment_methods or []
    payment_method = (payload.get("payment_method") or (pms[0] if pms else "")).strip()

    from core.services import price_index as pi_svc
    if (o.price_type or "fixed") == "float" and o.float_margin_pct:
        live = await pi_svc.compute_float_price(db, o.coin or "USDT", o.fiat or "RUB", o.float_margin_pct)
        rate_used = live or o.rate_rub_per_usdt
    else:
        rate_used = o.rate_rub_per_usdt
    amount_usdt = (amount_rub / rate_used).quantize(Decimal("0.0001"))
    fee_pct = await settings_kv.get_fee_v2_pct(db)
    fee_usdt = (amount_usdt * fee_pct / 100).quantize(Decimal("0.0001"))

    if o.side == "sell":
        buyer_id, seller_id = user.id, o.user_id
    else:
        buyer_id, seller_id = o.user_id, user.id

    seller = await db.get(User, seller_id)
    if not seller or seller.balance_usdt < amount_usdt:
        raise HTTPException(400, "у продавца недостаточно USDT для escrow")

    # Реквизиты от seller (см. create_deal выше)
    from core.models import UserPaymentMethod
    res_pm = await db.execute(
        select(UserPaymentMethod)
        .where(UserPaymentMethod.user_id == seller_id)
        .where(UserPaymentMethod.type == payment_method)
        .where(UserPaymentMethod.is_active == True)  # noqa: E712
        .order_by(UserPaymentMethod.id.desc())
        .limit(1)
    )
    seller_pm = res_pm.scalar_one_or_none()
    deal_bank = seller_pm.bank_name if seller_pm else None
    deal_card = seller_pm.card_or_phone if seller_pm else None
    deal_name = seller_pm.receiver_name if seller_pm else None

    pay_window = o.pay_window_min or 30
    deadline = datetime.now(timezone.utc) + timedelta(minutes=pay_window)
    deal = Deal(
        deal_number=await _next_deal_number(db),
        offer_id=o.id,
        buyer_id=buyer_id, seller_id=seller_id,
        coin=o.coin or "USDT", fiat=o.fiat or "RUB",
        amount_rub=amount_rub,
        rate_rub_per_usdt=rate_used,
        amount_usdt=amount_usdt,
        payment_method=payment_method,
        bank=deal_bank,
        phone_or_card=deal_card,
        receiver_name=deal_name,
        status="awaiting_payment",
        fee_pct=fee_pct, fee_usdt=fee_usdt,
        expires_at=deadline, pay_deadline_at=deadline,
    )
    db.add(deal)
    await db.flush()
    await escrow_service.lock(db, seller, deal)

    try:
        from core.models import DealMessage
        db.add(DealMessage(
            deal_id=deal.id, from_user_id=None,
            text=f"Сделка #{deal.deal_number} создана. {float(amount_rub)} {deal.fiat} -> {float(amount_usdt)} {deal.coin}.",
            is_system=True,
        ))
        if o.auto_reply:
            db.add(DealMessage(
                deal_id=deal.id, from_user_id=o.user_id,
                text=o.auto_reply[:2000], is_system=False,
            ))
        await db.flush()
    except Exception:
        pass

    # Bot-notify обеим сторонам + JARVIS
    try:
        from bot.main import notify_user
        buyer = await db.get(User, buyer_id)
        if seller and seller.tg_id:
            await notify_user(
                seller.tg_id,
                f"🆕 <b>Новая сделка #{deal.deal_number}</b>\n"
                f"Покупатель ждёт оплаты: {float(amount_rub)} {deal.fiat}\n"
                f"USDT заморожено в Escrow до завершения сделки.",
            )
        if buyer and buyer.tg_id:
            await notify_user(
                buyer.tg_id,
                f"📋 <b>Сделка #{deal.deal_number} создана</b>\n"
                f"Откройте Mini-App чтобы увидеть реквизиты и таймер.",
            )
    except Exception:
        pass
    try:
        await jarvis_sync.send_event("deal_created", {
            "deal_id": deal.id, "deal_number": deal.deal_number,
            "buyer_id": buyer_id, "seller_id": seller_id,
            "amount_rub": float(amount_rub), "amount_usdt": float(amount_usdt),
        })
    except Exception:
        pass

    return {"ok": True, "id": deal.id, "deal_number": deal.deal_number}


@router.get("/my")
async def deals_my_v3(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Deal)
        .where(or_(Deal.buyer_id == user.id, Deal.seller_id == user.id))
        .order_by(desc(Deal.created_at))
        .limit(max(1, min(limit, 200)))
    )
    rows = (await db.execute(q)).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": d.id, "deal_number": d.deal_number,
                "coin": d.coin or "USDT", "fiat": d.fiat or "RUB",
                "amount_usdt": float(d.amount_usdt),
                "amount_fiat": float(d.amount_rub),
                "status": d.status,
                "buyer_id": d.buyer_id, "seller_id": d.seller_id,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ],
    }


@router.get("/{deal_id}/info")
async def deal_info_v3(
    deal_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(Deal, deal_id)
    if not d or user.id not in (d.buyer_id, d.seller_id):
        raise HTTPException(404, "deal not found")
    buyer = await db.get(User, d.buyer_id)
    seller = await db.get(User, d.seller_id)
    return {
        "ok": True,
        "id": d.id, "deal_number": d.deal_number,
        "coin": d.coin or "USDT", "fiat": d.fiat or "RUB",
        "amount_usdt": float(d.amount_usdt),
        "amount_fiat": float(d.amount_rub),
        "price": float(d.rate_rub_per_usdt),
        "status": d.status,
        "buyer_id": d.buyer_id, "seller_id": d.seller_id,
        "buyer_tg_id": buyer.tg_id if buyer else None,
        "seller_tg_id": seller.tg_id if seller else None,
        "buyer_username": buyer.username if buyer else None,
        "seller_username": seller.username if seller else None,
        "payment_method": d.payment_method,
        "expires_at": d.expires_at.isoformat() if d.expires_at else None,
        "pay_deadline_at": d.pay_deadline_at.isoformat() if d.pay_deadline_at else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


# ─── Chat messages ──────────────────────────────────────────────────────
@router.get("/{deal_id}/messages")
async def deal_messages_get(
    deal_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(Deal, deal_id)
    if not d or user.id not in (d.buyer_id, d.seller_id):
        raise HTTPException(404, "deal not found")
    from core.models import DealMessage
    rows = (await db.execute(
        select(DealMessage).where(DealMessage.deal_id == deal_id)
        .order_by(DealMessage.created_at.asc()).limit(500)
    )).scalars().all()
    user_ids = {m.from_user_id for m in rows if m.from_user_id}
    tg_map = {}
    if user_ids:
        urows = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        tg_map = {u.id: u.tg_id for u in urows}
    return {
        "ok": True,
        "items": [
            {
                "id": m.id, "deal_id": m.deal_id,
                "from_user_id": m.from_user_id,
                "from_user_tg": tg_map.get(m.from_user_id) if m.from_user_id else None,
                "text": m.text,
                "system": bool(m.is_system),
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ],
    }


@router.post("/{deal_id}/messages")
async def deal_messages_post(
    deal_id: int,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(Deal, deal_id)
    if not d or user.id not in (d.buyer_id, d.seller_id):
        raise HTTPException(404, "deal not found")
    if d.status in ("released", "cancelled"):
        raise HTTPException(400, "сделка закрыта, чат недоступен")
    text = (payload.get("text") or "").strip()[:2000]
    if not text:
        raise HTTPException(400, "empty message")
    from core.models import DealMessage
    msg = DealMessage(deal_id=deal_id, from_user_id=user.id, text=text)
    db.add(msg)
    await db.flush()
    try:
        from bot.main import notify_user
        other_id = d.seller_id if user.id == d.buyer_id else d.buyer_id
        other = await db.get(User, other_id)
        if other:
            await notify_user(
                other.tg_id,
                f"<b>Сделка #{d.deal_number}</b>\n<i>{text[:200]}</i>",
            )
    except Exception:
        pass
    return {"ok": True, "id": msg.id}


@router.post("/{deal_id}/dispute")
async def open_dispute(
    deal_id: int,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Открыть спор — escrow заморожен до решения админа."""
    d = await db.get(Deal, deal_id)
    if not d or user.id not in (d.buyer_id, d.seller_id):
        raise HTTPException(404, "deal not found")
    if d.status in ("released", "cancelled"):
        raise HTTPException(400, "сделка уже закрыта")
    reason = (payload.get("reason") or "").strip()
    if len(reason) < 5:
        raise HTTPException(400, "опишите причину спора (от 5 символов)")
    d.status = "disputed"
    dispute = Dispute(
        deal_id=d.id, opened_by_id=user.id,
        reason=reason[:1024],
        evidence_urls=payload.get("evidence_urls") or [],
        status="open",
    )
    db.add(dispute)
    await db.flush()
    try:
        await jarvis_sync.send_event("dispute_opened", {
            "deal_id": d.id, "deal_number": d.deal_number,
            "dispute_id": dispute.id, "opened_by_id": user.id,
            "reason": reason[:240],
        })
    except Exception:
        pass
    # Bot-notify другой стороне + арбитрам (в админ-чат через JARVIS)
    try:
        from bot.main import notify_user
        other_id = d.seller_id if user.id == d.buyer_id else d.buyer_id
        other = await db.get(User, other_id)
        if other:
            who = "покупатель" if user.id == d.buyer_id else "продавец"
            await notify_user(
                other.tg_id,
                f"⚠️ <b>Открыт спор по сделке #{d.deal_number}</b>\n"
                f"Инициатор: {who}\n"
                f"Причина: {reason[:240]}\n\n"
                f"Арбитр получит уведомление и подключится в ближайшее время. "
                f"USDT заморожено до решения.",
            )
    except Exception:
        pass
    return {"ok": True, "dispute_id": dispute.id}

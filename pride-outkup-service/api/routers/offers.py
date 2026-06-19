"""V2 P2P Offers — industrial доска объявлений.

Поддерживает:
- fixed / float pricing (margin% от индекса)
- price band валидация
- counterparty conditions: KYC, min_completed_deals, region
- pay_window_min: 15/30/45/60/90/120
- максимум 5 методов оплаты
- PRIDE Official всегда сверху
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user, require_verified
from core.db import get_db
from core.models import Offer, User
from core.services import escrow_service, price_index as pi_svc, settings_kv

router = APIRouter()


async def _effective_price(db: AsyncSession, o: Offer) -> Decimal:
    """Цена для UI: float = index * margin / 100, fallback = stored rate."""
    if (o.price_type or "fixed") == "float" and o.float_margin_pct:
        try:
            computed = await pi_svc.compute_float_price(
                db, o.coin or "USDT", o.fiat or "RUB", o.float_margin_pct,
            )
            if computed:
                return computed
        except Exception:
            pass
    return o.rate_rub_per_usdt


def _offer_to_dict(o: Offer, author: User | None = None, effective_price: Decimal | None = None) -> dict:
    price = float(effective_price) if effective_price is not None else float(o.rate_rub_per_usdt)
    return {
        "id": o.id,
        "user_id": o.user_id,
        "author": {
            "id": author.id if author else o.user_id,
            "username": author.username if author else None,
            "trust_score": author.trust_score if author else 0,
            "total_deals": author.total_deals if author else 0,
            "completion_rate_pct": author.completion_rate_pct if author else 0.0,
            "is_pride_official": o.is_pride_official,
            "maker_tier": getattr(author, "maker_tier", "none") if author else "none",
        } if author or o.is_pride_official else None,
        "side": o.side,
        "coin": o.coin or "USDT",
        "fiat": o.fiat or "RUB",
        "rate_rub_per_usdt": price,
        "price": price,
        "price_type": o.price_type or "fixed",
        "float_margin_pct": float(o.float_margin_pct) if o.float_margin_pct else None,
        "min_amount_rub": float(o.min_amount_rub),
        "max_amount_rub": float(o.max_amount_rub),
        "payment_methods": o.payment_methods or [],
        "pay_window_min": o.pay_window_min or 30,
        "min_taker_completed": o.min_taker_completed or 0,
        "require_kyc": bool(o.require_kyc),
        "region": o.region,
        "conditions": o.conditions,
        "auto_reply": o.auto_reply,
        "status": o.status,
        "paused_reason": o.paused_reason,
        "is_pride_official": o.is_pride_official,
        "filled_count": o.filled_count,
        "total_volume_usdt": float(o.total_volume_usdt),
        "created_at": o.created_at.isoformat(),
    }


@router.get("")
async def list_offers(
    side: str = "buy",
    payment_method: str | None = None,
    min_amount: int = 0,
    online_only: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """V2 список offers (стакан с точки зрения тейкера)."""
    if not await settings_kv.is_v2_p2p_public(db):
        if user.tg_id not in __import__("core.config", fromlist=["settings"]).settings.admin_ids:
            raise HTTPException(503, "V2 P2P stack пока в закрытой бете")

    offer_side = "sell" if side == "buy" else "buy"
    q = (
        select(Offer)
        .where(
            Offer.side == offer_side,
            Offer.status == "active",
            Offer.user_id != user.id,  # свои офферы не показываем на доске
        )
        .order_by(
            desc(Offer.is_pride_official),
            Offer.rate_rub_per_usdt.asc() if side == "buy" else Offer.rate_rub_per_usdt.desc(),
        )
        .limit(max(1, min(limit, 200)))
    )
    if payment_method:
        q = q.where(Offer.payment_methods.any(payment_method))
    if min_amount and min_amount > 0:
        q = q.where(Offer.max_amount_rub >= Decimal(str(min_amount)))

    offers = (await db.execute(q)).scalars().all()
    author_ids = list({o.user_id for o in offers})
    authors = {
        u.id: u for u in (await db.execute(select(User).where(User.id.in_(author_ids)))).scalars().all()
    }
    items = []
    for o in offers:
        eff = await _effective_price(db, o)
        items.append(_offer_to_dict(o, authors.get(o.user_id), eff))
    items.sort(key=lambda x: x["price"], reverse=(side == "sell"))
    items.sort(key=lambda x: x.get("is_pride_official", False), reverse=True)
    return {"items": items, "side_view": side, "count": len(items)}


@router.get("/{offer_id}")
async def get_offer(
    offer_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await db.get(Offer, offer_id)
    if not o:
        raise HTTPException(404, "offer not found")
    author = await db.get(User, o.user_id)
    eff = await _effective_price(db, o)
    return {"offer": _offer_to_dict(o, author, eff)}


@router.post("")
async def create_offer(
    payload: dict,
    user: User = Depends(require_verified),
    db: AsyncSession = Depends(get_db),
):
    """Создать оффер с поддержкой fixed/float pricing + price band."""
    side = (payload.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy or sell")
    coin = (payload.get("coin") or "USDT").upper()[:16]
    fiat = (payload.get("fiat") or "RUB").upper()[:8]
    price_type = (payload.get("price_type") or "fixed").lower()
    if price_type not in ("fixed", "float"):
        raise HTTPException(400, "price_type must be fixed|float")

    try:
        min_amt = Decimal(str(payload.get("min_amount_rub") or 0))
        max_amt = Decimal(str(payload.get("max_amount_rub") or 0))
    except Exception:
        raise HTTPException(400, "bad amounts")
    if min_amt < 100 or max_amt < min_amt:
        raise HTTPException(400, "min_amount>=100, max>=min")

    float_margin = None
    if price_type == "fixed":
        try:
            rate = Decimal(str(payload.get("rate_rub_per_usdt") or payload.get("price") or 0))
        except Exception:
            raise HTTPException(400, "bad rate")
        if rate <= 0:
            raise HTTPException(400, "rate>0")
    else:
        try:
            float_margin = Decimal(str(payload.get("float_margin_pct") or 0))
        except Exception:
            raise HTTPException(400, "bad float_margin_pct")
        if not (Decimal("85") <= float_margin <= Decimal("115")):
            raise HTTPException(400, "float_margin_pct в диапазоне 85..115")
        rate = await pi_svc.compute_float_price(db, coin, fiat, float_margin) or Decimal("0")
        if rate <= 0:
            raise HTTPException(400, f"индекс {coin}/{fiat} ещё не загружен")

    ok, idx, band = await pi_svc.within_band(db, rate, coin, fiat)
    if not ok and idx:
        raise HTTPException(
            400,
            f"цена {float(rate)} отклоняется от рынка {float(idx)} {fiat}/{coin} >±{float(band)}%",
        )

    payment_methods = payload.get("payment_methods") or []
    if not isinstance(payment_methods, list) or not payment_methods:
        raise HTTPException(400, "payment_methods (list) required")
    if len(payment_methods) > 5:
        raise HTTPException(400, "не больше 5 методов оплаты")

    pay_window = int(payload.get("pay_window_min") or 30)
    if pay_window not in (15, 30, 45, 60, 90, 120):
        raise HTTPException(400, "pay_window_min должен быть 15/30/45/60/90/120")

    min_taker_completed = max(0, int(payload.get("min_taker_completed") or 0))
    require_kyc = bool(payload.get("require_kyc") or False)
    region = (payload.get("region") or "").strip()[:16] or None

    # Этап 2: amount_usdt_total — сколько USDT seller вынес на продажу.
    # Если не указан клиентом — считаем из max_amount_rub / rate (как было).
    try:
        amount_usdt = Decimal(str(
            payload.get("amount_usdt_total") or payload.get("amount") or 0
        ))
    except Exception:
        amount_usdt = Decimal("0")
    if amount_usdt <= 0:
        amount_usdt = max_amt / rate

    if side == "sell":
        # СТРОЖАЙШАЯ проверка: на доступном балансе USDT >= amount_usdt.
        if user.balance_usdt < amount_usdt:
            raise HTTPException(
                400,
                f"Недостаточно USDT для выставления объявления: нужно {float(amount_usdt):.4f}, "
                f"доступно {float(user.balance_usdt):.4f}. Лимиты должны быть в пределах баланса.",
            )
        # Лимит max_amount тоже должен укладываться в баланс
        if max_amt / rate > amount_usdt:
            raise HTTPException(
                400,
                f"Максимум лимита ({float(max_amt):.0f}₽) превышает объём USDT в объявлении "
                f"({float(amount_usdt):.4f} USDT @ {float(rate):.2f}₽).",
            )

    o = Offer(
        user_id=user.id, side=side, coin=coin, fiat=fiat,
        price_type=price_type, float_margin_pct=float_margin,
        rate_rub_per_usdt=rate,
        min_amount_rub=min_amt, max_amount_rub=max_amt,
        amount_usdt_total=amount_usdt,
        amount_usdt_remaining=amount_usdt,
        payment_methods=payment_methods,
        pay_window_min=pay_window,
        min_taker_completed=min_taker_completed,
        require_kyc=require_kyc, region=region,
        conditions=(payload.get("conditions") or "").strip()[:1024] or None,
        auto_reply=(payload.get("auto_reply") or "").strip()[:1024] or None,
        status="active",
    )
    db.add(o)
    await db.flush()

    # Этап 2: для sell — лочим USDT в escrow сразу при создании
    if side == "sell":
        try:
            await escrow_service.lock_for_offer(db, user, o, amount_usdt)
        except HTTPException:
            # откатываем создание offer'а если lock сорвался
            await db.delete(o)
            await db.flush()
            raise

    return {"ok": True, "offer_id": o.id, "amount_usdt_total": float(amount_usdt)}


@router.patch("/{offer_id}")
async def update_offer(
    offer_id: int,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await db.get(Offer, offer_id)
    if not o:
        raise HTTPException(404, "offer not found")
    if o.user_id != user.id:
        raise HTTPException(403, "не ваш оффер")
    allowed = {
        "rate_rub_per_usdt", "min_amount_rub", "max_amount_rub",
        "payment_methods", "conditions", "auto_reply",
        "pay_window_min", "min_taker_completed", "require_kyc", "region",
    }
    for k, v in payload.items():
        if k in allowed and v is not None:
            if k in ("rate_rub_per_usdt", "min_amount_rub", "max_amount_rub"):
                v = Decimal(str(v))
            setattr(o, k, v)
    await db.flush()
    return {"ok": True}


@router.patch("/{offer_id}/pause")
async def pause_offer(
    offer_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await db.get(Offer, offer_id)
    if not o or o.user_id != user.id:
        raise HTTPException(404, "offer not found")
    if o.status == "paused":
        return {"ok": True, "status": "paused", "released_usdt": 0}
    o.status = "paused"
    o.paused_reason = "user_paused"
    # Этап 2: при паузе sell-offer возвращаем escrow юзеру
    released = Decimal("0")
    if o.side == "sell":
        released = await escrow_service.release_offer_lock(db, user, o)
    await db.flush()
    return {"ok": True, "status": o.status, "released_usdt": float(released)}


@router.patch("/{offer_id}/resume")
async def resume_offer(
    offer_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await db.get(Offer, offer_id)
    if not o or o.user_id != user.id:
        raise HTTPException(404, "offer not found")
    if o.status == "active":
        return {"ok": True, "status": "active"}
    # Этап 2: при возобновлении sell-offer повторно лочим escrow на remaining
    locked = Decimal("0")
    if o.side == "sell":
        need = o.amount_usdt_remaining or Decimal("0")
        if need > 0:
            if user.balance_usdt < need:
                raise HTTPException(
                    400,
                    f"Недостаточно USDT для возобновления: нужно {float(need):.4f}, "
                    f"доступно {float(user.balance_usdt):.4f}",
                )
            await escrow_service.lock_for_offer(db, user, o, need)
            locked = need
    o.status = "active"
    o.paused_reason = None
    await db.flush()
    return {"ok": True, "status": o.status, "locked_usdt": float(locked)}


@router.delete("/{offer_id}")
async def archive_offer(
    offer_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await db.get(Offer, offer_id)
    if not o or o.user_id != user.id:
        raise HTTPException(404, "offer not found")
    # Этап 2: возврат escrow при удалении sell-offer
    released = Decimal("0")
    if o.side == "sell" and o.status != "archived":
        released = await escrow_service.release_offer_lock(db, user, o)
    o.status = "archived"
    o.amount_usdt_remaining = Decimal("0")
    await db.flush()
    return {"ok": True, "released_usdt": float(released)}


@router.get("/me/list")
async def my_offers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Свои офферы — без archived/cancelled (их юзер удалил)."""
    res = await db.execute(
        select(Offer)
        .where(
            Offer.user_id == user.id,
            Offer.status.in_(("active", "paused", "completed")),
        )
        .order_by(desc(Offer.created_at))
    )
    items = []
    for o in res.scalars().all():
        eff = await _effective_price(db, o)
        items.append(_offer_to_dict(o, user, eff))
    return {"items": items}


# ─── Mini-App v3 aliases ────────────────────────────────────────────────
@router.get("/list")
async def list_offers_v3(
    side: str = "buy",
    coin: str = "USDT",
    fiat: str = "RUB",
    payment_method: str | None = None,
    min_amount: float = 0,
    verified_only: int = 0,
    region: str | None = None,
    limit: int = 50,
    page: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Industrial список офферов — фильтры + пагинация + effective price."""
    if not await settings_kv.is_v2_p2p_public(db):
        from core.config import settings as _s
        if user.tg_id not in _s.admin_ids:
            return {"ok": True, "items": [], "count": 0}

    offer_side = "sell" if side == "buy" else "buy"
    coin_u, fiat_u = coin.upper(), fiat.upper()
    page_size = max(1, min(limit, 100))
    offset = max(0, page) * page_size

    q = (
        select(Offer)
        .where(
            Offer.side == offer_side, Offer.status == "active",
            Offer.coin == coin_u, Offer.fiat == fiat_u,
        )
        .order_by(
            desc(Offer.is_pride_official),
            Offer.rate_rub_per_usdt.asc() if side == "buy" else Offer.rate_rub_per_usdt.desc(),
        )
        .offset(offset)
        .limit(page_size)
    )
    if payment_method:
        q = q.where(Offer.payment_methods.any(payment_method))
    if min_amount and min_amount > 0:
        q = q.where(Offer.max_amount_rub >= Decimal(str(min_amount)))
    if region:
        q = q.where(Offer.region == region[:16])

    rows = (await db.execute(q)).scalars().all()
    author_ids = list({o.user_id for o in rows})
    authors = {}
    if author_ids:
        authors = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_(author_ids))
            )).scalars().all()
        }

    items = []
    for o in rows:
        author = authors.get(o.user_id)
        if verified_only and author and getattr(author, "maker_tier", "none") == "none":
            continue
        eff = await _effective_price(db, o)
        items.append({
            "id": o.id, "user_id": o.user_id,
            "username": author.username if author else None,
            "side": o.side, "coin": o.coin or "USDT", "fiat": o.fiat or "RUB",
            "price": float(eff),
            "price_type": o.price_type or "fixed",
            "float_margin_pct": float(o.float_margin_pct) if o.float_margin_pct else None,
            "min_amount": float(o.min_amount_rub),
            "max_amount": float(o.max_amount_rub),
            "payment_methods": o.payment_methods or [],
            "pay_window_min": o.pay_window_min or 30,
            "min_taker_completed": o.min_taker_completed or 0,
            "require_kyc": bool(o.require_kyc),
            "region": o.region,
            "auto_reply": o.auto_reply,
            "conditions": o.conditions,
            "is_pride_official": o.is_pride_official,
            "is_active": o.status == "active",
            "maker_tier": getattr(author, "maker_tier", "none") if author else "none",
            "total_deals": author.total_deals if author else 0,
            "completion_rate": float(author.completion_rate_pct) if author else 100.0,
            "avg_release_time_sec": author.avg_release_time_sec if author else 0,
            "created_at": o.created_at.isoformat(),
        })
    items.sort(key=lambda x: x["price"], reverse=(side == "sell"))
    items.sort(key=lambda x: x.get("is_pride_official", False), reverse=True)
    return {"ok": True, "items": items, "count": len(items), "page": page}


@router.post("/create")
async def create_offer_v3(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать оффер (v3 формат для Mini-App). fixed/float + band + extended."""
    side = (payload.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy or sell")
    coin = (payload.get("coin") or "USDT").upper()[:16]
    fiat = (payload.get("fiat") or "RUB").upper()[:8]
    price_type = (payload.get("price_type") or "fixed").lower()
    if price_type not in ("fixed", "float"):
        raise HTTPException(400, "price_type must be fixed|float")

    try:
        min_amt = Decimal(str(payload.get("min_amount") or 0))
        max_amt = Decimal(str(payload.get("max_amount") or 0))
    except Exception:
        raise HTTPException(400, "bad amounts")
    if min_amt <= 0 or max_amt < min_amt:
        raise HTTPException(400, "min>0, max>=min")

    float_margin = None
    if price_type == "fixed":
        try:
            price = Decimal(str(payload.get("price") or 0))
        except Exception:
            raise HTTPException(400, "bad price")
        if price <= 0:
            raise HTTPException(400, "price>0")
    else:
        try:
            float_margin = Decimal(str(payload.get("float_margin_pct") or 0))
        except Exception:
            raise HTTPException(400, "bad float_margin_pct")
        if not (Decimal("85") <= float_margin <= Decimal("115")):
            raise HTTPException(400, "float_margin_pct в диапазоне 85..115")
        price = await pi_svc.compute_float_price(db, coin, fiat, float_margin) or Decimal("0")
        if price <= 0:
            raise HTTPException(400, f"индекс {coin}/{fiat} ещё не загружен")

    ok, idx, band = await pi_svc.within_band(db, price, coin, fiat)
    if not ok and idx:
        raise HTTPException(400, f"цена отклоняется от рынка >±{float(band)}%")

    pms = payload.get("payment_methods") or []
    if not isinstance(pms, list) or not pms:
        raise HTTPException(400, "payment_methods required")
    if len(pms) > 5:
        raise HTTPException(400, "не больше 5 методов оплаты")

    pay_window = int(payload.get("pay_window_min") or 30)
    if pay_window not in (15, 30, 45, 60, 90, 120):
        raise HTTPException(400, "pay_window_min должен быть 15/30/45/60/90/120")

    if side == "sell":
        need = max_amt / price
        if user.balance_usdt < need:
            raise HTTPException(400, f"нужно >= {float(need):.2f} {coin} на балансе")

    o = Offer(
        user_id=user.id, side=side, coin=coin, fiat=fiat,
        price_type=price_type, float_margin_pct=float_margin,
        rate_rub_per_usdt=price,
        min_amount_rub=min_amt, max_amount_rub=max_amt,
        payment_methods=pms,
        pay_window_min=pay_window,
        min_taker_completed=max(0, int(payload.get("min_taker_completed") or 0)),
        require_kyc=bool(payload.get("require_kyc") or False),
        region=(payload.get("region") or "").strip()[:16] or None,
        conditions=(payload.get("conditions") or "").strip()[:1024] or None,
        auto_reply=(payload.get("auto_reply") or "").strip()[:1024] or None,
        status="active",
    )
    db.add(o)
    await db.flush()
    return {"ok": True, "id": o.id}

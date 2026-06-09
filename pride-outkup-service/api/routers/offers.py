"""V2 P2P Offers — доска объявлений между клиентами.

PRIDE Official (is_pride_official=True) всегда первый в списке.
Фильтры: side, payment_method, min_amount.
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user, require_verified
from core.db import get_db
from core.models import Offer, User
from core.services import settings_kv

router = APIRouter()


def _offer_to_dict(o: Offer, author: User | None = None) -> dict:
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
        } if author or o.is_pride_official else None,
        "side": o.side,
        "rate_rub_per_usdt": float(o.rate_rub_per_usdt),
        "min_amount_rub": float(o.min_amount_rub),
        "max_amount_rub": float(o.max_amount_rub),
        "payment_methods": o.payment_methods or [],
        "conditions": o.conditions,
        "status": o.status,
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
    """V2 список offers.

    `side` — со стороны клиента (buyer-view):
      side=buy   → клиент хочет КУПИТЬ USDT → показываем offers с side='sell'
      side=sell  → клиент хочет ПРОДАТЬ USDT → показываем offers с side='buy'
    PRIDE Official всегда первым.
    """
    if not await settings_kv.is_v2_p2p_public(db):
        # Для админа доступно даже когда выключено
        if user.tg_id not in __import__("core.config", fromlist=["settings"]).settings.admin_ids:
            raise HTTPException(503, "V2 P2P stack пока в закрытой бете")

    offer_side = "sell" if side == "buy" else "buy"
    q = (
        select(Offer)
        .where(Offer.side == offer_side, Offer.status == "active")
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

    res = await db.execute(q)
    offers = res.scalars().all()
    # Загружаем авторов
    author_ids = list({o.user_id for o in offers})
    authors_res = await db.execute(select(User).where(User.id.in_(author_ids)))
    authors = {u.id: u for u in authors_res.scalars().all()}

    return {
        "items": [_offer_to_dict(o, authors.get(o.user_id)) for o in offers],
        "side_view": side,
        "count": len(offers),
    }


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
    return {"offer": _offer_to_dict(o, author)}


@router.post("")
async def create_offer(
    payload: dict,
    user: User = Depends(require_verified),
    db: AsyncSession = Depends(get_db),
):
    """Создать новый offer (V2)."""
    side = (payload.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy or sell")
    try:
        rate = Decimal(str(payload.get("rate_rub_per_usdt") or 0))
        min_amt = Decimal(str(payload.get("min_amount_rub") or 0))
        max_amt = Decimal(str(payload.get("max_amount_rub") or 0))
    except Exception:
        raise HTTPException(400, "bad amounts")
    if rate <= 0 or min_amt < 1000 or max_amt < min_amt:
        raise HTTPException(400, "rate>0, min_amount≥1000, max_amount≥min_amount")
    payment_methods = payload.get("payment_methods") or []
    if not isinstance(payment_methods, list) or not payment_methods:
        raise HTTPException(400, "payment_methods (list) required")

    # Sanity: USDT-баланс для side=sell должен покрыть max_amount хотя бы на одну сделку
    if side == "sell":
        need = max_amt / rate
        if user.balance_usdt < need:
            raise HTTPException(
                400,
                f"для sell-оффера нужно ≥ {float(need):.2f} USDT на балансе для эскроу",
            )

    o = Offer(
        user_id=user.id,
        side=side,
        rate_rub_per_usdt=rate,
        min_amount_rub=min_amt,
        max_amount_rub=max_amt,
        payment_methods=payment_methods,
        conditions=(payload.get("conditions") or "").strip()[:1024] or None,
        auto_reply=(payload.get("auto_reply") or "").strip()[:1024] or None,
        status="active",
    )
    db.add(o)
    await db.flush()
    return {"ok": True, "offer_id": o.id}


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
    o.status = "paused"
    await db.flush()
    return {"ok": True, "status": o.status}


@router.patch("/{offer_id}/resume")
async def resume_offer(
    offer_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await db.get(Offer, offer_id)
    if not o or o.user_id != user.id:
        raise HTTPException(404, "offer not found")
    o.status = "active"
    await db.flush()
    return {"ok": True, "status": o.status}


@router.delete("/{offer_id}")
async def archive_offer(
    offer_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await db.get(Offer, offer_id)
    if not o or o.user_id != user.id:
        raise HTTPException(404, "offer not found")
    o.status = "archived"
    await db.flush()
    return {"ok": True}


@router.get("/me/list")
async def my_offers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Свои офферы."""
    res = await db.execute(
        select(Offer).where(Offer.user_id == user.id).order_by(desc(Offer.created_at))
    )
    items = res.scalars().all()
    return {"items": [_offer_to_dict(o, user) for o in items]}

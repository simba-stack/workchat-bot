"""P2P Queries API (GET endpoints) — CQRS read-side.

Прямые SELECT — без orchestrator'а (read-only).
"""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import maker_stats, wallet
from p2p.enums import AdvertisementStatus, TradeStatus, WalletBalanceCategory
from p2p.models import (
    P2PAdvertisement, P2PTrade, P2PDispute, P2PWallet,
)

logger = logging.getLogger("p2p.api.queries")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-queries"])


def _ad_to_dict(a: P2PAdvertisement) -> dict:
    return {
        "id": a.id,
        "owner_id": a.owner_id,
        "type": a.type,
        "crypto": a.crypto_currency,
        "fiat": a.fiat_currency,
        "amount_total": str(a.total_amount),
        "amount_available": str(a.available_amount),
        "amount_reserved": str(a.reserved_amount),
        "min_order_fiat": str(a.min_amount_fiat),
        "max_order_fiat": str(a.max_amount_fiat),
        "pricing_mode": a.pricing_mode,
        "price_fixed": str(a.price) if a.price is not None else None,
        "price_margin_pct": str(a.price_margin_pct) if a.price_margin_pct is not None else None,
        "payment_methods": a.payment_method_ids or [],
        "time_limit_minutes": a.pay_window_min,
        "require_kyc": a.require_verified_taker,
        "min_completed_trades": a.min_taker_completed,
        "terms_text": a.description,
        "auto_reply_text": a.merchant_note,
        "status": a.status,
        "paused_reason": a.paused_reason,
        "paused_at": a.paused_at.isoformat() if a.paused_at else None,
        "archived_at": a.archived_at.isoformat() if a.archived_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "version": a.version,
    }


def _trade_to_dict(t: P2PTrade) -> dict:
    return {
        "id": t.id,
        "trade_number": t.trade_number,
        "advertisement_id": t.advertisement_id,
        "buyer_id": t.buyer_id,
        "seller_id": t.seller_id,
        "crypto": t.crypto_currency,
        "fiat": t.fiat_currency,
        "amount_crypto": str(t.crypto_amount),
        "amount_fiat": str(t.fiat_amount),
        "price": str(t.price),
        "fee_pct": str(t.fee_pct) if t.fee_pct is not None else None,
        "platform_fee_crypto": str(t.fee_crypto) if t.fee_crypto else None,
        "payment_method_id": t.payment_method_id,
        "status": t.status,
        "pay_deadline_at": t.pay_deadline_at.isoformat() if t.pay_deadline_at else None,
        "confirm_deadline_at": t.confirm_deadline_at.isoformat() if t.confirm_deadline_at else None,
        "escrow_locked_at": t.escrow_locked_at.isoformat() if t.escrow_locked_at else None,
        "payment_marked_at": t.payment_marked_at.isoformat() if t.payment_marked_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "cancelled_at": t.cancelled_at.isoformat() if t.cancelled_at else None,
        "cancelled_reason": t.cancelled_reason,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "version": t.version,
    }


# ============= ADVERTISEMENTS =============

@router.get("/advertisements")
async def q_list_advertisements(
    type: Optional[str] = Query(None, regex="^(buy|sell)$"),
    crypto: Optional[str] = Query(None),
    fiat: Optional[str] = Query(None),
    status: Optional[str] = Query("ACTIVE"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    conds = []
    if type:
        conds.append(P2PAdvertisement.type == type)
    if crypto:
        conds.append(P2PAdvertisement.crypto_currency == crypto.upper())
    if fiat:
        conds.append(P2PAdvertisement.fiat_currency == fiat.upper())
    if status:
        conds.append(P2PAdvertisement.status == status.upper())
    q = select(P2PAdvertisement)
    if conds:
        q = q.where(and_(*conds))
    q = q.order_by(desc(P2PAdvertisement.created_at)).limit(limit).offset(offset)
    r = await db.execute(q)
    items = [_ad_to_dict(a) for a in r.scalars().all()]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/advertisements/{ad_id}")
async def q_get_advertisement(ad_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(P2PAdvertisement).where(P2PAdvertisement.id == ad_id))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "advertisement not found")
    return _ad_to_dict(a)


@router.get("/my/advertisements")
async def q_my_advertisements(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(P2PAdvertisement).where(P2PAdvertisement.owner_id == user.id)
        .order_by(desc(P2PAdvertisement.created_at))
    )
    return {"items": [_ad_to_dict(a) for a in r.scalars().all()]}


# ============= TRADES =============

@router.get("/trades/{trade_id}")
async def q_get_trade(
    trade_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    t = r.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "trade not found")
    if user.id not in (t.buyer_id, t.seller_id):
        raise HTTPException(403, "not a participant")
    return _trade_to_dict(t)


@router.get("/my/trades")
async def q_my_trades(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(P2PTrade).where(
        or_(P2PTrade.buyer_id == user.id, P2PTrade.seller_id == user.id)
    )
    if status:
        q = q.where(P2PTrade.status == status.upper())
    q = q.order_by(desc(P2PTrade.created_at)).limit(limit)
    r = await db.execute(q)
    return {"items": [_trade_to_dict(t) for t in r.scalars().all()]}


# ============= USERS / STATS =============

@router.get("/users/{user_id}/stats")
async def q_user_stats(
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Публичная P2P-статистика юзера (для карточки мерчанта)."""
    return await maker_stats.get_user_stats(db, user_id)


@router.get("/my/stats")
async def q_my_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Моя P2P-статистика."""
    return await maker_stats.get_user_stats(db, user.id)


# ============= WALLET =============

@router.get("/wallet")
async def q_wallet(
    crypto: str = Query("USDT"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    b = await wallet.get_breakdown(db, user.id, crypto.upper())
    return {
        "user_id": user.id,
        "currency": crypto.upper(),
        "available": str(b.available),
        "advertisement_hold": str(b.advertisement_hold),
        "trade_escrow": str(b.trade_escrow),
        "frozen": str(b.frozen),
        "pending": str(b.pending),
        "total": str(b.available + b.advertisement_hold + b.trade_escrow + b.frozen),
    }

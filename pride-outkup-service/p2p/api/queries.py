"""P2P Queries API (GET endpoints) — CQRS read-side.

Прямые SELECT — без orchestrator'а (read-only).
"""
from __future__ import annotations
import base64
import json
import logging
from decimal import Decimal
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import maker_stats, wallet
from p2p.enums import AdvertisementStatus, DisputeStatus, TradeStatus, WalletBalanceCategory
from p2p.models import (
    P2PAdvertisement, P2PTrade, P2PDispute, P2PWallet,
)

logger = logging.getLogger("p2p.api.queries")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-queries"])


# ============= UI FLAGS / NEXT-ACTION HELPERS =============

_NEXT_ACTION_DESCR = {
    "CREATED": "Инициализация сделки",
    "ESCROW_LOCKED": "Эскроу заблокирован — ожидаем перехода к оплате",
    "WAITING_FOR_PAYMENT": "Покупатель должен оплатить и нажать «Я оплатил»",
    "PAYMENT_MARKED": "Продавец проверяет получение оплаты",
    "PAYMENT_CONFIRMATION": "Продавец подтверждает зачисление средств",
    "DISPUTE_OPENED": "Открыт спор — ожидаем назначения арбитра",
    "ARBITRATION": "Идёт арбитраж — арбитр принимает решение",
    "RESOLVED": "Решение принято — формируется итог сделки",
    "COMPLETED": "Сделка успешно завершена",
    "CANCELLED": "Сделка отменена",
}


def _compute_ui_flags(trade: P2PTrade, user_id: Optional[int], has_open_dispute: bool = False) -> dict:
    """Возвращает разрешённые действия для user_id."""
    if user_id is None:
        return {
            "can_mark_paid": False,
            "can_confirm": False,
            "can_cancel": False,
            "can_dispute": False,
            "can_send_message": False,
            "can_upload_file": False,
            "can_review": False,
            "can_copy_payment": False,
            "can_open_bank": False,
            "can_call_support": True,
            "can_extend_deadline": False,
        }
    is_buyer = user_id == trade.buyer_id
    is_seller = user_id == trade.seller_id
    s = trade.status
    can_mark_paid = is_buyer and s == TradeStatus.WAITING_FOR_PAYMENT.value and not has_open_dispute
    can_confirm = is_seller and s == TradeStatus.PAYMENT_MARKED.value and not has_open_dispute
    can_cancel = (
        is_buyer
        and s in (
            TradeStatus.CREATED.value,
            TradeStatus.ESCROW_LOCKED.value,
            TradeStatus.WAITING_FOR_PAYMENT.value,
        )
        and not has_open_dispute
    )
    can_dispute = (
        (is_buyer or is_seller)
        and s in (
            TradeStatus.WAITING_FOR_PAYMENT.value,
            TradeStatus.PAYMENT_MARKED.value,
            TradeStatus.PAYMENT_CONFIRMATION.value,
        )
        and not has_open_dispute
    )
    terminal = (
        TradeStatus.COMPLETED.value,
        TradeStatus.CANCELLED.value,
        TradeStatus.RESOLVED.value,
    )
    can_send_message = (is_buyer or is_seller) and s not in terminal
    can_upload_file = can_send_message
    can_review = (is_buyer or is_seller) and s == TradeStatus.COMPLETED.value
    can_copy_payment = is_buyer and s in (
        TradeStatus.WAITING_FOR_PAYMENT.value,
        TradeStatus.PAYMENT_MARKED.value,
    )
    can_open_bank = can_copy_payment
    can_extend_deadline = is_seller and s == TradeStatus.WAITING_FOR_PAYMENT.value
    return {
        "can_mark_paid": can_mark_paid,
        "can_confirm": can_confirm,
        "can_cancel": can_cancel,
        "can_dispute": can_dispute,
        "can_send_message": can_send_message,
        "can_upload_file": can_upload_file,
        "can_review": can_review,
        "can_copy_payment": can_copy_payment,
        "can_open_bank": can_open_bank,
        "can_call_support": True,
        "can_extend_deadline": can_extend_deadline,
    }


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
        "completed_count": int(getattr(a, "completed_count", 0) or 0),
        "trades_count": int(getattr(a, "trades_count", 0) or 0),
    }


def _trade_to_dict(t: P2PTrade, user_id: Optional[int] = None, has_open_dispute: bool = False) -> dict:
    ui_flags = _compute_ui_flags(t, user_id, has_open_dispute=has_open_dispute)
    allowed_actions = sorted([k for k, v in ui_flags.items() if v])
    seq_number = getattr(t, "seq_number", None)
    if seq_number is None:
        seq_number = t.version
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
        "payment_snapshot": t.payment_snapshot or {},
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
        "seq_number": seq_number,
        "has_open_dispute": has_open_dispute,
        "ui_flags": ui_flags,
        "allowed_actions": allowed_actions,
        "next_expected_action_description": _NEXT_ACTION_DESCR.get(t.status, ""),
    }


# ============= ADVERTISEMENTS =============

def _encode_cursor(d: dict) -> str:
    raw = json.dumps(d, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(s: str) -> Optional[dict]:
    if not s:
        return None
    try:
        pad = "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(s + pad)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.warning("[queries] invalid cursor: %s", e)
        raise HTTPException(422, "invalid cursor")


@router.get("/advertisements")
async def q_list_advertisements(
    type: Optional[str] = Query(None, regex="^(buy|sell|BUY|SELL)$"),
    crypto: Optional[str] = Query(None),
    fiat: Optional[str] = Query(None),
    status: Optional[str] = Query("ACTIVE"),
    sort: str = Query("created_desc", regex="^(price_asc|price_desc|rating_desc|trades_desc|created_desc)$"),
    cursor: Optional[str] = Query(None, description="opaque base64 cursor for keyset pagination"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List advertisements. Поддерживает sort + cursor (keyset) + offset (legacy)."""
    conds = []
    if type:
        conds.append(P2PAdvertisement.type == type.upper())
    if crypto:
        conds.append(P2PAdvertisement.crypto_currency == crypto.upper())
    if fiat:
        conds.append(P2PAdvertisement.fiat_currency == fiat.upper())
    if status:
        conds.append(P2PAdvertisement.status == status.upper())

    q = select(P2PAdvertisement)
    if conds:
        q = q.where(and_(*conds))

    next_cursor: Optional[str] = None
    cur = _decode_cursor(cursor) if cursor else None

    if sort == "price_asc":
        q = q.order_by(asc(P2PAdvertisement.price), asc(P2PAdvertisement.id))
    elif sort == "price_desc":
        q = q.order_by(desc(P2PAdvertisement.price), desc(P2PAdvertisement.id))
    elif sort == "rating_desc":
        q = q.order_by(desc(P2PAdvertisement.completed_count), desc(P2PAdvertisement.id))
    elif sort == "trades_desc":
        q = q.order_by(desc(P2PAdvertisement.completed_count), desc(P2PAdvertisement.id))
    else:
        # created_desc — поддерживает keyset через cursor
        if cur:
            try:
                cur_ts = datetime.fromisoformat(cur["ts"])
                cur_id = cur["id"]
                q = q.where(
                    or_(
                        P2PAdvertisement.created_at < cur_ts,
                        and_(
                            P2PAdvertisement.created_at == cur_ts,
                            P2PAdvertisement.id < cur_id,
                        ),
                    )
                )
            except Exception:
                raise HTTPException(422, "invalid cursor payload")
        q = q.order_by(desc(P2PAdvertisement.created_at), desc(P2PAdvertisement.id))

    if not cur:
        q = q.offset(offset)
    q = q.limit(limit + 1)

    r = await db.execute(q)
    rows = list(r.scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_ad_to_dict(a) for a in rows]

    if has_more and sort == "created_desc" and rows:
        last = rows[-1]
        if last.created_at is not None:
            next_cursor = _encode_cursor({
                "id": last.id,
                "ts": last.created_at.isoformat(),
            })

    return {
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


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

async def _has_open_dispute(db: AsyncSession, trade_id: str) -> bool:
    r = await db.execute(
        select(P2PDispute.id).where(
            P2PDispute.trade_id == trade_id,
            P2PDispute.status.in_([
                DisputeStatus.OPENED.value,
                DisputeStatus.ARBITRATION.value,
            ]),
        ).limit(1)
    )
    return r.scalar_one_or_none() is not None


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
    has_open = await _has_open_dispute(db, t.id)
    return _trade_to_dict(t, user_id=user.id, has_open_dispute=has_open)


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
    trades = list(r.scalars().all())
    open_ids: set = set()
    if trades:
        rd = await db.execute(
            select(P2PDispute.trade_id).where(
                P2PDispute.trade_id.in_([t.id for t in trades]),
                P2PDispute.status.in_([
                    DisputeStatus.OPENED.value,
                    DisputeStatus.ARBITRATION.value,
                ]),
            )
        )
        open_ids = {row[0] for row in rd.all()}
    return {
        "items": [
            _trade_to_dict(t, user_id=user.id, has_open_dispute=(t.id in open_ids))
            for t in trades
        ]
    }


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

"""P2P Merchant Dashboard aggregate endpoint (Том 30).

GET /api/v2/p2p/merchant/dashboard — единый ответ для дашборда мерчанта.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import maker_stats, rbac, wallet
from p2p.api.queries import _ad_to_dict, _trade_to_dict
from p2p.enums import (
    AdvertisementStatus, DisputeStatus, TradeStatus,
)
from p2p.models import (
    P2PAdvertisement, P2PDispute, P2PNotification, P2PTrade,
)

logger = logging.getLogger("p2p.api.merchant_dashboard")
router = APIRouter(prefix="/api/v2/p2p/merchant", tags=["p2p-merchant"])


_ACTIVE_TRADE_STATUSES = [
    TradeStatus.CREATED.value,
    TradeStatus.ESCROW_LOCKED.value,
    TradeStatus.WAITING_FOR_PAYMENT.value,
    TradeStatus.PAYMENT_MARKED.value,
    TradeStatus.PAYMENT_CONFIRMATION.value,
    TradeStatus.DISPUTE_OPENED.value,
    TradeStatus.ARBITRATION.value,
]


async def _period_stats(
    db: AsyncSession,
    user_id: int,
    since: datetime | None,
) -> dict:
    """Возвращает {trades, volume_usdt, disputes} для периода [since, now]."""
    is_party = or_(P2PTrade.buyer_id == user_id, P2PTrade.seller_id == user_id)
    conds = [is_party]
    if since:
        conds.append(P2PTrade.created_at >= since)

    rt = await db.execute(
        select(
            func.count(P2PTrade.id),
            func.coalesce(func.sum(P2PTrade.crypto_amount), 0),
        ).where(*conds, P2PTrade.crypto_currency == "USDT")
    )
    trades_count, volume = rt.one()

    # Disputes — у юзера за период
    dconds = [
        or_(P2PTrade.buyer_id == user_id, P2PTrade.seller_id == user_id),
    ]
    if since:
        dconds.append(P2PDispute.created_at >= since)
    rd = await db.execute(
        select(func.count(P2PDispute.id))
        .join(P2PTrade, P2PTrade.id == P2PDispute.trade_id)
        .where(*dconds)
    )
    disputes_count = rd.scalar() or 0

    return {
        "trades": int(trades_count or 0),
        "volume_usdt": str(Decimal(str(volume or 0))),
        "disputes": int(disputes_count or 0),
    }


@router.get("/dashboard")
async def merchant_dashboard(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Агрегированный ответ для Merchant Dashboard UI."""
    now = datetime.now(timezone.utc)

    # === MERCHANT meta ===
    role = await rbac.resolve_role_async(db, user)
    stats = await maker_stats.get_user_stats(db, user.id)
    rating = None  # rating в текущем стате не хранится — placeholder
    success_rate = stats.get("completion_rate") or 0.0
    response_time_sec = stats.get("avg_release_time_sec")
    completed_trades = stats.get("completed_trades", 0)
    verified = bool(getattr(user, "kyc_status", None) in ("approved", "verified"))
    trading_enabled = not bool(getattr(user, "is_blocked", False))

    merchant_block = {
        "level": role,
        "verified": verified,
        "trading_enabled": trading_enabled,
        "rating": rating,
        "completed_trades": completed_trades,
        "success_rate": float(success_rate),
        "response_time_sec": response_time_sec,
    }

    # === WALLET ===
    try:
        b = await wallet.get_breakdown(db, user.id, "USDT")
        wallet_block = {
            "currency": "USDT",
            "available": str(b.available),
            "advertisement_hold": str(b.advertisement_hold),
            "trade_escrow": str(b.trade_escrow),
            "frozen": str(b.frozen),
            "pending": str(b.pending),
            "total": str(
                b.available + b.advertisement_hold + b.trade_escrow + b.frozen + b.pending
            ),
        }
    except Exception as e:
        logger.warning("[merchant.dashboard] wallet breakdown failed: %s", e)
        wallet_block = {
            "currency": "USDT",
            "available": "0",
            "advertisement_hold": "0",
            "trade_escrow": "0",
            "frozen": "0",
            "pending": "0",
            "total": "0",
        }

    # === STATS by period ===
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    stats_block = {
        "today": await _period_stats(db, user.id, today_start),
        "7d": await _period_stats(db, user.id, now - timedelta(days=7)),
        "30d": await _period_stats(db, user.id, now - timedelta(days=30)),
        "all": await _period_stats(db, user.id, None),
    }

    # === ACTIVE ADVERTISEMENTS ===
    ra = await db.execute(
        select(P2PAdvertisement)
        .where(
            P2PAdvertisement.owner_id == user.id,
            P2PAdvertisement.status == AdvertisementStatus.ACTIVE.value,
        )
        .order_by(desc(P2PAdvertisement.created_at))
        .limit(50)
    )
    active_ads = [_ad_to_dict(a) for a in ra.scalars().all()]

    # === ACTIVE TRADES ===
    rt = await db.execute(
        select(P2PTrade)
        .where(
            or_(P2PTrade.buyer_id == user.id, P2PTrade.seller_id == user.id),
            P2PTrade.status.in_(_ACTIVE_TRADE_STATUSES),
        )
        .order_by(desc(P2PTrade.created_at))
        .limit(50)
    )
    active_trades_rows = list(rt.scalars().all())

    # Bulk open-dispute check
    open_ids: set[str] = set()
    if active_trades_rows:
        rd = await db.execute(
            select(P2PDispute.trade_id).where(
                P2PDispute.trade_id.in_([t.id for t in active_trades_rows]),
                P2PDispute.status.in_([
                    DisputeStatus.OPENED.value,
                    DisputeStatus.ARBITRATION.value,
                ]),
            )
        )
        open_ids = {row[0] for row in rd.all()}

    active_trades = [
        _trade_to_dict(t, user_id=user.id, has_open_dispute=(t.id in open_ids))
        for t in active_trades_rows
    ]

    # === NOTIFICATIONS (last 10) ===
    rn = await db.execute(
        select(P2PNotification)
        .where(P2PNotification.user_id == user.id)
        .order_by(desc(P2PNotification.created_at))
        .limit(10)
    )
    notifications = [
        {
            "id": n.id,
            "type": n.type,
            "title": n.title,
            "body": n.body,
            "payload": n.payload or {},
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rn.scalars().all()
    ]

    return {
        "merchant": merchant_block,
        "wallet": wallet_block,
        "stats": stats_block,
        "active_advertisements": active_ads,
        "active_trades": active_trades,
        "notifications": notifications,
        "version_sync": int(now.timestamp() * 1000),
        "server_ts": now.isoformat(),
    }

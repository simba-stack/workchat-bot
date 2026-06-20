"""Maker stats — агрегированная статистика юзера по P2P-сделкам.

Используется в:
- /api/v2/p2p/users/{id}/stats (publicly visible card)
- RBAC: автоматический промоушн в MERCHANT по completed_trades > N

Все запросы read-only, без mutate.
"""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from p2p.enums import TradeStatus
from p2p.models import P2PTrade

logger = logging.getLogger("p2p.maker_stats")


async def get_user_stats(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """Вернуть агрегированную P2P-статистику юзера.

    Все вычисления — по таблице p2p_trades.

    Поля:
        completed_trades: int
        cancelled_trades: int
        total_trades: int
        completion_rate: float (0..1, 0 если total_trades=0)
        avg_release_time_sec: int|None (avg по completed_at-payment_marked_at)
        total_volume_usdt: str (Decimal as str)
        last_trade_at: str|None (ISO)
    """
    is_party = or_(P2PTrade.buyer_id == user_id, P2PTrade.seller_id == user_id)

    # Counts by status
    r = await db.execute(
        select(P2PTrade.status, func.count(P2PTrade.id))
        .where(is_party)
        .group_by(P2PTrade.status)
    )
    by_status: dict[str, int] = {row[0]: int(row[1] or 0) for row in r.all()}

    completed = by_status.get(TradeStatus.COMPLETED.value, 0)
    cancelled = by_status.get(TradeStatus.CANCELLED.value, 0)
    total = sum(by_status.values())
    completion_rate = (completed / total) if total > 0 else 0.0

    # Total volume USDT (по completed, считаем оба направления)
    r = await db.execute(
        select(func.coalesce(func.sum(P2PTrade.crypto_amount), 0)).where(
            is_party,
            P2PTrade.status == TradeStatus.COMPLETED.value,
            P2PTrade.crypto_currency == "USDT",
        )
    )
    total_volume = Decimal(str(r.scalar() or 0))

    # Avg release time (completed_at - payment_marked_at) в секундах
    r = await db.execute(
        select(
            func.avg(
                func.extract("epoch", P2PTrade.completed_at)
                - func.extract("epoch", P2PTrade.payment_marked_at)
            )
        ).where(
            is_party,
            P2PTrade.status == TradeStatus.COMPLETED.value,
            P2PTrade.completed_at.is_not(None),
            P2PTrade.payment_marked_at.is_not(None),
        )
    )
    avg_release_raw = r.scalar()
    avg_release_sec: int | None = None
    if avg_release_raw is not None:
        try:
            avg_release_sec = max(0, int(float(avg_release_raw)))
        except (TypeError, ValueError):
            avg_release_sec = None

    # Last trade timestamp
    r = await db.execute(
        select(P2PTrade.created_at)
        .where(is_party)
        .order_by(desc(P2PTrade.created_at))
        .limit(1)
    )
    last_at = r.scalar()

    return {
        "user_id": user_id,
        "completed_trades": completed,
        "cancelled_trades": cancelled,
        "total_trades": total,
        "completion_rate": round(completion_rate, 4),
        "avg_release_time_sec": avg_release_sec,
        "total_volume_usdt": str(total_volume),
        "last_trade_at": last_at.isoformat() if last_at else None,
        "by_status": by_status,
    }

"""Maker stats / tier service.

Раз в час пересчитывает maker tier у всех User'ов с активными офферами:
- bronze:  >=10 completed, >=90% completion за 30д
- silver:  >=50 completed, >=95%, avg_release_time<30 мин
- gold:    >=200 completed, >=98%, avg_release_time<15 мин, dispute_rate<2%
- official: ставится вручную через POST /admin/maker/{id}/official_toggle
- none:     не отвечает требованиям bronze

Также проверяет anti-fraud:
- can_take_deal(user) — учитывает cancel_cooldown_until, max 3 active deals.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import AsyncSessionLocal
from core.models import Deal, User

logger = logging.getLogger(__name__)

TIER_RECOMPUTE_INTERVAL_SEC = 3600  # каждый час
MAX_ACTIVE_DEALS_PER_USER = 3


def _calc_tier(user: User) -> str:
    """Вычислить tier по полям User. official ставится вручную, не пересчитываем."""
    if user.maker_tier == "official":
        return "official"
    completed = user.completed_deals or 0
    total = user.total_deals or 0
    cancelled = user.cancelled_deals or 0
    disputed = user.disputed_deals or 0
    rate = (completed / total * 100) if total > 0 else 0.0
    avg_sec = user.avg_release_time_sec or 0
    dispute_rate = (disputed / total * 100) if total > 0 else 0.0

    if completed >= 200 and rate >= 98 and avg_sec and avg_sec < 15 * 60 and dispute_rate < 2:
        return "gold"
    if completed >= 50 and rate >= 95 and avg_sec and avg_sec < 30 * 60:
        return "silver"
    if completed >= 10 and rate >= 90:
        return "bronze"
    return "none"


async def recompute_all() -> int:
    """Пересчитать tier у всех юзеров. Возвращает кол-во обновлённых."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(User))).scalars().all()
        updated = 0
        for u in rows:
            new_tier = _calc_tier(u)
            if u.maker_tier != new_tier:
                u.maker_tier = new_tier
                u.maker_tier_updated_at = datetime.now(timezone.utc)
                updated += 1
        if updated:
            await db.commit()
            logger.info("[maker_stats] updated %d tiers", updated)
        return updated


async def can_take_deal(db: AsyncSession, user: User) -> tuple[bool, Optional[str]]:
    """Проверить anti-fraud перед открытием сделки.

    Returns (ok, error_message).
    """
    now = datetime.now(timezone.utc)
    if user.cancel_cooldown_until and user.cancel_cooldown_until > now:
        return False, f"Антифрод: новые сделки доступны после {user.cancel_cooldown_until:%H:%M %d.%m}"
    # Max active deals
    active_count = (await db.execute(
        select(func.count(Deal.id)).where(
            or_(Deal.buyer_id == user.id, Deal.seller_id == user.id),
            Deal.status.in_(("created", "awaiting_payment", "paid", "disputed")),
        )
    )).scalar() or 0
    if active_count >= MAX_ACTIVE_DEALS_PER_USER:
        return False, f"Максимум {MAX_ACTIVE_DEALS_PER_USER} активных сделок одновременно"
    return True, None


async def check_taker_meets_offer_conditions(
    db: AsyncSession, taker: User, offer
) -> tuple[bool, Optional[str]]:
    """Проверка counterparty conditions оффера: KYC, min_completed_deals."""
    if offer.require_kyc and taker.kyc_status not in ("verified",):
        return False, "Этот оффер требует подтверждённый KYC"
    if offer.min_taker_completed and (taker.completed_deals or 0) < offer.min_taker_completed:
        return False, (
            f"Этот оффер требует не менее {offer.min_taker_completed} завершённых сделок "
            f"(у вас {taker.completed_deals or 0})"
        )
    return True, None


async def tier_loop() -> None:
    logger.info("[maker_stats] started, interval=%ds", TIER_RECOMPUTE_INTERVAL_SEC)
    while True:
        try:
            await recompute_all()
        except Exception as e:
            logger.exception("[maker_stats] tick error: %s", e)
        try:
            await asyncio.sleep(TIER_RECOMPUTE_INTERVAL_SEC)
        except asyncio.CancelledError:
            break

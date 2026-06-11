"""Deal lifecycle background tasks.

1. expire_overdue_deals — раз в минуту:
   - находит deals со status='awaiting_payment' и expires_at<now
   - переводит в 'cancelled' с reason='expired'
   - возвращает escrow продавцу
   - уведомляет обе стороны
   - инкрементит User.cancelled_deals (для anti-fraud)

2. enforce_price_band — раз в 60с:
   - находит active fixed-офферы у которых цена вылетела из price band
   - переводит в paused с paused_reason='price_band_violation'
   - уведомляет мейкера
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import AsyncSessionLocal
from core.models import Deal, Offer, User
from core.services import escrow_service, jarvis_sync, price_index as pi_svc

logger = logging.getLogger(__name__)

LIFECYCLE_INTERVAL_SEC = 60
ANTI_FRAUD_CANCEL_LIMIT_24H = 3
ANTI_FRAUD_COOLDOWN_HOURS = 24
DEAL_PAYMENT_DEFAULT_MIN = 30


async def _notify_telegram(user: User, text: str) -> None:
    try:
        from bot.main import notify_user
        await notify_user(user.tg_id, text)
    except Exception:
        pass


async def _expire_one(db: AsyncSession, d: Deal) -> None:
    """Истечь одну сделку: cancel + refund + notify + anti-fraud."""
    d.status = "cancelled"
    d.cancelled_at = datetime.now(timezone.utc)
    d.cancelled_reason = "expired"
    try:
        await escrow_service.refund(db, d, "expired")
    except Exception as e:
        logger.exception("[lifecycle] escrow refund failed for deal %s: %s", d.id, e)

    # Anti-fraud: считаем cancel buyer'у (он не оплатил)
    buyer = await db.get(User, d.buyer_id)
    if buyer:
        buyer.cancelled_deals = (buyer.cancelled_deals or 0) + 1
        # Если за последние 24ч ≥3 cancels — кулдаун
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_cancels = (await db.execute(
            select(Deal).where(
                Deal.buyer_id == buyer.id,
                Deal.status == "cancelled",
                Deal.cancelled_at >= since,
            )
        )).scalars().all()
        if len(recent_cancels) >= ANTI_FRAUD_CANCEL_LIMIT_24H:
            buyer.cancel_cooldown_until = datetime.now(timezone.utc) + timedelta(hours=ANTI_FRAUD_COOLDOWN_HOURS)
            logger.warning("[lifecycle] anti-fraud cooldown for user %s (%d cancels in 24h)",
                           buyer.id, len(recent_cancels))

    # Notify обе стороны
    seller = await db.get(User, d.seller_id)
    if buyer:
        await _notify_telegram(buyer,
            f"Сделка #{d.deal_number} отменена по таймауту. Оплата не пришла за {DEAL_PAYMENT_DEFAULT_MIN} мин.")
    if seller:
        await _notify_telegram(seller,
            f"Сделка #{d.deal_number} отменена по таймауту. {float(d.amount_usdt)} {d.coin or 'USDT'} вернулись на баланс.")

    try:
        await jarvis_sync.send_event("deal_expired", {
            "deal_id": d.id, "deal_number": d.deal_number,
        })
    except Exception:
        pass


async def expire_overdue_deals() -> int:
    """Один проход expirer'а. Возвращает количество expired сделок."""
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        rows = (await db.execute(
            select(Deal).where(
                Deal.status == "awaiting_payment",
                or_(
                    and_(Deal.pay_deadline_at.is_not(None), Deal.pay_deadline_at < now),
                    and_(Deal.pay_deadline_at.is_(None), Deal.expires_at.is_not(None), Deal.expires_at < now),
                ),
            ).limit(50)
        )).scalars().all()
        if not rows:
            return 0
        for d in rows:
            try:
                await _expire_one(db, d)
            except Exception as e:
                logger.exception("[lifecycle] expire deal %s failed: %s", d.id, e)
        await db.commit()
        logger.info("[lifecycle] expired %d deals", len(rows))
        return len(rows)


async def enforce_price_band() -> int:
    """Перевод офферов в paused если цена вылетела из коридора."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Offer).where(
                Offer.status == "active",
                Offer.price_type == "fixed",
            ).limit(200)
        )).scalars().all()
        if not rows:
            return 0
        paused = 0
        for o in rows:
            try:
                ok, idx, band = await pi_svc.within_band(
                    db, o.rate_rub_per_usdt, o.coin or "USDT", o.fiat or "RUB",
                )
                if not ok and idx:
                    o.status = "paused"
                    o.paused_reason = f"price_band ±{float(band)}%"
                    paused += 1
                    author = await db.get(User, o.user_id)
                    if author:
                        await _notify_telegram(
                            author,
                            f"Оффер #{o.id} приостановлен: цена {float(o.rate_rub_per_usdt)} "
                            f"вылетела из коридора ±{float(band)}% от рынка {float(idx)}.",
                        )
            except Exception as e:
                logger.warning("[lifecycle] band check offer %s: %s", o.id, e)
        if paused:
            await db.commit()
            logger.info("[lifecycle] paused %d offers (price band)", paused)
        return paused


async def lifecycle_loop() -> None:
    """Главный цикл — раз в минуту expirer + band-enforcer."""
    logger.info("[lifecycle] started, interval=%ds", LIFECYCLE_INTERVAL_SEC)
    while True:
        try:
            await expire_overdue_deals()
            await enforce_price_band()
        except Exception as e:
            logger.exception("[lifecycle] tick error: %s", e)
        try:
            await asyncio.sleep(LIFECYCLE_INTERVAL_SEC)
        except asyncio.CancelledError:
            break

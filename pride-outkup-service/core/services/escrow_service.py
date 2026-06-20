"""Escrow service — блокировка/разблокировка USDT под Deal."""
from datetime import datetime, timezone
from decimal import Decimal
import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Deal, EscrowLock, OperationLog, User

logger = logging.getLogger(__name__)


async def _sync_usdt_from_coin_balances(db: AsyncSession, user: User) -> None:
    """Синхронизирует User.balance_usdt с UserCoinBalance[USDT].

    Новые юзера пополняют через TRC20 → баланс попадает в UserCoinBalance.
    Старая логика P2P/эскроу работает с User.balance_usdt напрямую.
    Этот helper берёт MAX из двух источников и обновляет User.balance_usdt.
    """
    try:
        from core.services import balance_service
        from decimal import Decimal as _D
        coin_bal = await balance_service.get_balance(db, user.id, "USDT")
        legacy = user.balance_usdt or _D("0")
        if coin_bal > legacy:
            # Подтягиваем — есть USDT в coin_balances но нет в legacy
            user.balance_usdt = coin_bal
            await db.flush()
    except Exception as e:
        logger.warning("[escrow] sync usdt failed for user=%s: %s", user.id, e)


async def lock(db: AsyncSession, seller: User, deal: Deal) -> EscrowLock:
    """Блокировать USDT продавца под сделку.

    Списывает с seller.balance_usdt, создаёт EscrowLock(status='locked').
    """
    await _sync_usdt_from_coin_balances(db, seller)
    amount = deal.amount_usdt
    await _sync_usdt_from_coin_balances(db, seller)
    if seller.balance_usdt < amount:
        raise HTTPException(400, "недостаточно USDT в эскроу: пополни баланс")

    seller.balance_usdt -= amount
    lock_row = EscrowLock(
        user_id=seller.id,
        amount_usdt=amount,
        deal_id=deal.id,
        status="locked",
    )
    db.add(lock_row)

    op = OperationLog(
        user_id=seller.id,
        type="escrow_lock",
        amount_usdt=-amount,
        balance_after=seller.balance_usdt,
        ref_table="deals",
        ref_id=deal.id,
        note=f"escrow lock #{deal.deal_number}",
    )
    db.add(op)
    await db.flush()
    logger.info("[escrow] LOCKED %s USDT seller=%s deal=%s", amount, seller.id, deal.id)
    return lock_row


async def release(db: AsyncSession, deal: Deal) -> None:
    """Освободить escrow — USDT переходит buyer'у.
    Pessimistic lock на EscrowLock row — защита от дублей при гонке.
    """
    res = await db.execute(
        select(EscrowLock).where(
            EscrowLock.deal_id == deal.id, EscrowLock.status == "locked",
        ).with_for_update()
    )
    lock_row = res.scalar_one_or_none()
    if not lock_row:
        raise HTTPException(400, "escrow lock not found")

    buyer = await db.get(User, deal.buyer_id)
    seller = await db.get(User, deal.seller_id)
    if not buyer or not seller:
        raise HTTPException(500, "buyer/seller not found")

    fee = deal.fee_usdt or Decimal("0")
    payout = lock_row.amount_usdt - fee
    if payout < 0:
        payout = Decimal("0")
    buyer.balance_usdt += payout
    lock_row.status = "released"
    lock_row.released_at = datetime.now(timezone.utc)
    deal.released_at = lock_row.released_at
    deal.status = "released"

    # ── Stats для maker tier ────────────────────────────────────────
    if deal.paid_at and deal.released_at:
        release_sec = int((deal.released_at - deal.paid_at).total_seconds())
        prev_avg = seller.avg_release_time_sec or 0
        prev_completed = seller.completed_deals or 0
        seller.avg_release_time_sec = (
            (prev_avg * prev_completed + release_sec) // (prev_completed + 1)
            if prev_completed >= 0 else release_sec
        )
    for u in (buyer, seller):
        u.total_deals = (u.total_deals or 0) + 1
        u.completed_deals = (u.completed_deals or 0) + 1
        u.total_volume_usdt = (u.total_volume_usdt or Decimal("0")) + lock_row.amount_usdt

    db.add(OperationLog(
        user_id=buyer.id,
        type="deal_payout",
        amount_usdt=payout,
        balance_after=buyer.balance_usdt,
        ref_table="deals",
        ref_id=deal.id,
        note=f"deal {deal.deal_number} payout (fee {fee})",
    ))
    if fee > 0:
        db.add(OperationLog(
            user_id=buyer.id,
            type="fee_collected",
            amount_usdt=fee,
            balance_after=None,
            ref_table="deals",
            ref_id=deal.id,
            note=f"PRIDE fee {fee} from deal {deal.deal_number}",
        ))

    await db.flush()
    logger.info("[escrow] RELEASED %s USDT (fee %s) buyer=%s deal=%s",
                payout, fee, buyer.id, deal.id)


async def refund(db: AsyncSession, deal: Deal, reason: str = "cancelled") -> None:
    """Вернуть escrow продавцу."""
    res = await db.execute(
        select(EscrowLock).where(
            EscrowLock.deal_id == deal.id, EscrowLock.status == "locked",
        )
    )
    lock_row = res.scalar_one_or_none()
    if not lock_row:
        return  # уже refunded или released

    seller = await db.get(User, deal.seller_id)
    if not seller:
        raise HTTPException(500, "seller not found")

    seller.balance_usdt += lock_row.amount_usdt
    lock_row.status = "refunded"
    lock_row.released_at = datetime.now(timezone.utc)

    db.add(OperationLog(
        user_id=seller.id,
        type="escrow_refund",
        amount_usdt=lock_row.amount_usdt,
        balance_after=seller.balance_usdt,
        ref_table="deals",
        ref_id=deal.id,
        note=f"refund deal {deal.deal_number}: {reason}",
    ))
    await db.flush()
    logger.info("[escrow] REFUNDED %s USDT seller=%s deal=%s (%s)",
                lock_row.amount_usdt, seller.id, deal.id, reason)


# ═══════════════════════════════════════════════════════════════════════
# Этап 2: Offer-level escrow (заморозка USDT под активный sell-offer)
# ═══════════════════════════════════════════════════════════════════════
async def lock_for_offer(db: AsyncSession, user: User, offer, amount: Decimal):
    """Залочить USDT под активный sell-offer.

    Списывает с user.balance_usdt, создаёт EscrowLock(offer_id, amount).
    Если уже есть lock на этот offer — суммирует.

    Raises HTTPException(400) если на балансе не хватает.
    """
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    await _sync_usdt_from_coin_balances(db, user)
    if user.balance_usdt < amount:
        raise HTTPException(
            400,
            f"недостаточно USDT: нужно {float(amount):.4f}, доступно {float(user.balance_usdt):.4f}",
        )

    existing = await db.execute(
        select(EscrowLock).where(
            EscrowLock.offer_id == offer.id,
            EscrowLock.user_id == user.id,
            EscrowLock.status == "locked",
        )
    )
    lock_row = existing.scalar_one_or_none()
    if lock_row:
        lock_row.amount_usdt = (lock_row.amount_usdt or Decimal("0")) + amount
    else:
        lock_row = EscrowLock(
            user_id=user.id,
            amount_usdt=amount,
            offer_id=offer.id,
            reason="offer_active",
            status="locked",
        )
        db.add(lock_row)

    user.balance_usdt -= amount
    db.add(OperationLog(
        user_id=user.id,
        type="escrow_lock_offer",
        amount_usdt=-amount,
        balance_after=user.balance_usdt,
        ref_table="offers",
        ref_id=offer.id,
        note=f"lock for active offer #{offer.id}",
    ))
    await db.flush()
    logger.info("[escrow] LOCKED %s USDT user=%s offer=%s", amount, user.id, offer.id)
    return lock_row


async def release_offer_lock(db: AsyncSession, user: User, offer) -> Decimal:
    """Освободить все escrow-локи привязанные к offer.

    Используется при pause/delete sell-offer. Возвращает USDT на баланс юзеру.
    Returns: сумма освобождённых USDT.
    """
    r = await db.execute(
        select(EscrowLock).where(
            EscrowLock.offer_id == offer.id,
            EscrowLock.user_id == user.id,
            EscrowLock.status == "locked",
        )
    )
    locks = list(r.scalars().all())
    if not locks:
        return Decimal("0")

    released = Decimal("0")
    for l in locks:
        amt = l.amount_usdt or Decimal("0")
        user.balance_usdt += amt
        l.status = "released"
        l.released_at = datetime.now(timezone.utc)
        released += amt
        db.add(OperationLog(
            user_id=user.id,
            type="escrow_release_offer",
            amount_usdt=amt,
            balance_after=user.balance_usdt,
            ref_table="offers",
            ref_id=offer.id,
            note=f"release for offer #{offer.id}",
        ))
    await db.flush()
    logger.info("[escrow] RELEASED %s USDT user=%s offer=%s (offer-level)",
                released, user.id, offer.id)
    return released


async def get_balance_breakdown(db: AsyncSession, user: User) -> dict:
    """Возвращает доступный/захолдированный баланс + список причин холда.

    Используется на главном экране Mini-App и в боте /balance.
    """
    r = await db.execute(
        select(EscrowLock).where(
            EscrowLock.user_id == user.id,
            EscrowLock.status == "locked",
        )
    )
    locks = list(r.scalars().all())
    locked_total = sum((l.amount_usdt or Decimal("0")) for l in locks)
    available = user.balance_usdt
    return {
        "total_usdt": float(available + locked_total),
        "available_usdt": float(available),
        "locked_usdt": float(locked_total),
        "locks": [
            {
                "id": l.id,
                "amount_usdt": float(l.amount_usdt or 0),
                "reason": l.reason or "active_offer",
                "offer_id": l.offer_id,
                "deal_id": l.deal_id,
            }
            for l in locks
        ],
    }

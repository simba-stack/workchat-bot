"""Escrow service — блокировка/разблокировка USDT под Deal."""
from datetime import datetime, timezone
from decimal import Decimal
import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Deal, EscrowLock, OperationLog, User

logger = logging.getLogger(__name__)


async def lock(db: AsyncSession, seller: User, deal: Deal) -> EscrowLock:
    """Блокировать USDT продавца под сделку.

    Списывает с seller.balance_usdt, создаёт EscrowLock(status='locked').
    """
    amount = deal.amount_usdt
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

    seller теряет окончательно, buyer.balance_usdt += amount - fee.
    fee.usdt → системный счёт (logged).
    """
    res = await db.execute(
        select(EscrowLock).where(
            EscrowLock.deal_id == deal.id, EscrowLock.status == "locked",
        )
    )
    lock_row = res.scalar_one_or_none()
    if not lock_row:
        raise HTTPException(400, "escrow lock not found")

    buyer = await db.get(User, deal.buyer_id)
    if not buyer:
        raise HTTPException(500, "buyer not found")

    fee = deal.fee_usdt or Decimal("0")
    payout = lock_row.amount_usdt - fee
    if payout < 0:
        payout = Decimal("0")
    buyer.balance_usdt += payout
    lock_row.status = "released"
    lock_row.released_at = datetime.now(timezone.utc)
    deal.released_at = lock_row.released_at
    deal.status = "released"

    db.add(OperationLog(
        user_id=buyer.id,
        type="deal_payout",
        amount_usdt=payout,
        balance_after=buyer.balance_usdt,
        ref_table="deals",
        ref_id=deal.id,
        note=f"deal {deal.deal_number} payout (fee {fee})",
    ))
    # Бухгалтерская запись на системный fee (user_id=None не получится — кладём в meta)
    if fee > 0:
        db.add(OperationLog(
            user_id=buyer.id,  # технически тот же — но type=fee_collected
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

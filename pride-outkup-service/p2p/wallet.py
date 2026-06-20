"""Wallet Projection Engine — projection поверх Ledger (ТЗ Том 20).

Wallet НЕ источник истины. Источник — Ledger.
Wallet хранит готовые агрегаты для быстрого чтения.

После каждой ledger transaction вызывается update_wallet_from_ledger(user_id, currency).
Это пересчитывает все 5 категорий из ledger entries.

Инвариант (проверяется в update):
  Available + AdvertisementHold + TradeEscrow + Frozen + Pending = Σ Credits − Σ Debits

Несовпадение → CRITICAL incident.
"""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from p2p import locks
from p2p.enums import LedgerAccountType
from p2p.models import P2PLedgerAccount, P2PLedgerEntry, P2PWallet

logger = logging.getLogger("p2p.wallet")


class WalletBreakdown(NamedTuple):
    available: Decimal
    advertisement_hold: Decimal
    trade_escrow: Decimal
    frozen: Decimal
    pending: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.available + self.advertisement_hold + self.trade_escrow
            + self.frozen + self.pending
        )


# Маппинг LedgerAccountType → WalletBalanceCategory
_TYPE_TO_CATEGORY: dict[str, str] = {
    LedgerAccountType.USER_AVAILABLE.value: "available",
    LedgerAccountType.ADVERTISEMENT_HOLD.value: "advertisement_hold",
    LedgerAccountType.USER_ESCROW.value: "trade_escrow",
    LedgerAccountType.USER_FROZEN.value: "frozen",
    LedgerAccountType.FRAUD_HOLD.value: "frozen",
    LedgerAccountType.AML_HOLD.value: "frozen",
    LedgerAccountType.USER_PENDING.value: "pending",
}


async def get_or_create_wallet(
    db: AsyncSession,
    user_id: int,
    currency: str = "USDT",
) -> P2PWallet:
    """Найти или создать wallet projection для (user_id, currency)."""
    await locks.lock_user_wallet(db, user_id, currency)
    r = await db.execute(
        select(P2PWallet).where(
            P2PWallet.user_id == user_id,
            P2PWallet.currency == currency,
        )
    )
    w = r.scalar_one_or_none()
    if w is None:
        w = P2PWallet(user_id=user_id, currency=currency)
        db.add(w)
        await db.flush()
    return w


async def _calculate_from_ledger(
    db: AsyncSession,
    user_id: int,
    currency: str,
) -> WalletBreakdown:
    """Пересчитать все 5 категорий из Ledger entries для юзера."""
    r = await db.execute(
        select(
            P2PLedgerAccount.account_type,
            func.coalesce(func.sum(P2PLedgerEntry.credit), 0),
            func.coalesce(func.sum(P2PLedgerEntry.debit), 0),
        )
        .join(P2PLedgerAccount, P2PLedgerAccount.id == P2PLedgerEntry.account_id)
        .where(
            P2PLedgerAccount.owner_id == user_id,
            P2PLedgerAccount.currency == currency,
        )
        .group_by(P2PLedgerAccount.account_type)
    )

    categories: dict[str, Decimal] = {
        "available": Decimal("0"),
        "advertisement_hold": Decimal("0"),
        "trade_escrow": Decimal("0"),
        "frozen": Decimal("0"),
        "pending": Decimal("0"),
    }
    for acc_type, sum_credit, sum_debit in r.all():
        category = _TYPE_TO_CATEGORY.get(acc_type)
        if category is None:
            continue
        balance = Decimal(str(sum_credit or 0)) - Decimal(str(sum_debit or 0))
        categories[category] += balance

    return WalletBreakdown(**categories)


async def update_wallet_from_ledger(
    db: AsyncSession,
    user_id: int,
    currency: str = "USDT",
) -> P2PWallet:
    """Обновить wallet projection из Ledger. Вызывается после ledger.post_transaction().

    Под advisory lock, чтобы конкурентные транзакции не перетёрли версию.
    """
    wallet = await get_or_create_wallet(db, user_id, currency)
    breakdown = await _calculate_from_ledger(db, user_id, currency)

    # Проверка инварианта: ни одна категория не должна быть отрицательной
    if (breakdown.available < 0 or breakdown.advertisement_hold < 0
            or breakdown.trade_escrow < 0 or breakdown.frozen < 0
            or breakdown.pending < 0):
        logger.error(
            "[wallet] CRITICAL NEGATIVE BALANCE user=%s currency=%s breakdown=%s",
            user_id, currency, breakdown,
        )
        raise HTTPException(500, "wallet: negative balance detected")

    wallet.available = breakdown.available
    wallet.advertisement_hold = breakdown.advertisement_hold
    wallet.trade_escrow = breakdown.trade_escrow
    wallet.frozen = breakdown.frozen
    wallet.pending = breakdown.pending
    wallet.version += 1
    await db.flush()

    logger.info(
        "[wallet] updated user=%s avail=%s hold=%s escrow=%s frozen=%s pending=%s",
        user_id, breakdown.available, breakdown.advertisement_hold,
        breakdown.trade_escrow, breakdown.frozen, breakdown.pending,
    )
    return wallet


async def get_breakdown(
    db: AsyncSession,
    user_id: int,
    currency: str = "USDT",
) -> WalletBreakdown:
    """Получить breakdown баланса для отображения. Use projection если свежий, иначе пересчитать."""
    w = await get_or_create_wallet(db, user_id, currency)
    return WalletBreakdown(
        available=w.available, advertisement_hold=w.advertisement_hold,
        trade_escrow=w.trade_escrow, frozen=w.frozen, pending=w.pending,
    )


async def reconcile(
    db: AsyncSession,
    user_id: int,
    currency: str = "USDT",
) -> dict:
    """Reconciliation: проверить что Wallet Projection совпадает с Ledger.

    Используется фон-задачей reconciliation worker и debug endpoint.
    Возвращает {"ok": bool, "projection": {...}, "ledger": {...}, "delta": {...}}.
    """
    w = await get_or_create_wallet(db, user_id, currency)
    proj = WalletBreakdown(
        available=w.available, advertisement_hold=w.advertisement_hold,
        trade_escrow=w.trade_escrow, frozen=w.frozen, pending=w.pending,
    )
    ledger = await _calculate_from_ledger(db, user_id, currency)

    delta = {
        "available": proj.available - ledger.available,
        "advertisement_hold": proj.advertisement_hold - ledger.advertisement_hold,
        "trade_escrow": proj.trade_escrow - ledger.trade_escrow,
        "frozen": proj.frozen - ledger.frozen,
        "pending": proj.pending - ledger.pending,
    }
    ok = all(d == 0 for d in delta.values())
    if not ok:
        logger.error(
            "[wallet] RECONCILE FAILED user=%s currency=%s delta=%s",
            user_id, currency, delta,
        )
    return {
        "ok": ok,
        "projection": proj._asdict(),
        "ledger": ledger._asdict(),
        "delta": {k: str(v) for k, v in delta.items()},
    }

# === Aliases for backwards compatibility ===
get_balance_breakdown = get_breakdown

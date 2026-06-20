"""Ledger Engine — double-entry bookkeeping (ТЗ Том 21).

Главные принципы:
- Σ Debit = Σ Credit для каждой LedgerTransaction (иначе ValueError)
- IMMUTABLE: нет UPDATE/DELETE на entries
- Wallet projection обновляется ПОСЛЕ commit
- Используется advisory lock на account во время записи

Никакой другой сервис НЕ может писать в ledger_entries напрямую.
Все финансовые операции — только через post_transaction().
"""
from __future__ import annotations
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from p2p import locks
from p2p.enums import LedgerAccountType
from p2p.models import P2PLedgerAccount, P2PLedgerEntry

logger = logging.getLogger("p2p.ledger")


@dataclass
class LedgerLeg:
    """Одна сторона проводки. debit XOR credit > 0."""
    account_type: str       # LedgerAccountType
    owner_id: int | None    # None для системных счетов
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.debit, Decimal):
            self.debit = Decimal(str(self.debit))
        if not isinstance(self.credit, Decimal):
            self.credit = Decimal(str(self.credit))
        if self.debit < 0 or self.credit < 0:
            raise ValueError("debit/credit must be >= 0")
        if (self.debit > 0 and self.credit > 0) or (self.debit == 0 and self.credit == 0):
            raise ValueError("LedgerLeg requires exactly one of debit/credit > 0")


async def _get_or_create_account(
    db: AsyncSession,
    account_type: str,
    owner_id: int | None,
    currency: str,
) -> P2PLedgerAccount:
    """Найти или создать счёт. Под advisory lock — защита от race при первом создании."""
    await locks.advisory_lock(db, f"ledger_acc:{account_type}:{owner_id}:{currency}")
    r = await db.execute(
        select(P2PLedgerAccount).where(
            P2PLedgerAccount.account_type == account_type,
            P2PLedgerAccount.owner_id == owner_id,
            P2PLedgerAccount.currency == currency,
        )
    )
    acc = r.scalar_one_or_none()
    if acc is None:
        acc = P2PLedgerAccount(
            account_type=account_type,
            owner_id=owner_id,
            currency=currency,
            status="ACTIVE",
        )
        db.add(acc)
        await db.flush()
    return acc


async def get_account(
    db: AsyncSession,
    account_type: str,
    owner_id: int | None,
    currency: str = "USDT",
) -> P2PLedgerAccount | None:
    """Найти счёт. Возвращает None если не существует."""
    r = await db.execute(
        select(P2PLedgerAccount).where(
            P2PLedgerAccount.account_type == account_type,
            P2PLedgerAccount.owner_id == owner_id,
            P2PLedgerAccount.currency == currency,
        )
    )
    return r.scalar_one_or_none()


async def get_account_balance(
    db: AsyncSession,
    account: P2PLedgerAccount,
) -> Decimal:
    """Баланс счёта = Σ Credit − Σ Debit.

    Для активных счетов (USER_AVAILABLE, ADVERTISEMENT_HOLD, etc) баланс ≥ 0.
    Знак для пассивных не контролируем здесь — это ответственность бизнес-логики.
    """
    from sqlalchemy import func
    r = await db.execute(
        select(
            func.coalesce(func.sum(P2PLedgerEntry.credit), 0),
            func.coalesce(func.sum(P2PLedgerEntry.debit), 0),
        ).where(P2PLedgerEntry.account_id == account.id)
    )
    sum_credit, sum_debit = r.one()
    return Decimal(str(sum_credit or 0)) - Decimal(str(sum_debit or 0))


async def post_transaction(
    db: AsyncSession,
    legs: Sequence[LedgerLeg],
    currency: str = "USDT",
    *,
    reference_type: str | None = None,
    reference_id: str | None = None,
    workflow_id: str | None = None,
    correlation_id: str | None = None,
    operation_id: str | None = None,
    note: str | None = None,
) -> str:
    """Записать ledger transaction. Возвращает transaction_id.

    Инвариант: Σ Debit = Σ Credit. Иначе HTTPException(500).
    Все entries создаются IMMUTABLE (без UPDATE/DELETE).

    Все legs должны быть в одной валюте (для multi-currency — отдельные транзакции).
    """
    if not legs:
        raise HTTPException(500, "post_transaction: empty legs")

    total_debit = sum((l.debit for l in legs), Decimal("0"))
    total_credit = sum((l.credit for l in legs), Decimal("0"))
    if total_debit != total_credit:
        # КРИТИЧНАЯ ОШИБКА — нарушение double-entry
        logger.error(
            "[ledger] CRITICAL: Σ Debit (%s) ≠ Σ Credit (%s). Legs=%s",
            total_debit, total_credit, legs,
        )
        raise HTTPException(500, "Ledger imbalance: debit != credit")

    if total_debit == 0:
        raise HTTPException(500, "post_transaction: zero-amount transaction")

    transaction_id = str(uuid.uuid4())

    for leg in legs:
        acc = await _get_or_create_account(db, leg.account_type, leg.owner_id, currency)
        # Лок на счёт чтобы не было race с другими транзакциями
        await locks.advisory_lock(db, f"ledger_post:{acc.id}")

        entry = P2PLedgerEntry(
            transaction_id=transaction_id,
            account_id=acc.id,
            debit=leg.debit,
            credit=leg.credit,
            currency=currency,
            reference_type=reference_type,
            reference_id=reference_id,
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            operation_id=operation_id,
            note=leg.note or note,
        )
        db.add(entry)

    await db.flush()
    logger.info(
        "[ledger] posted tx=%s legs=%d amount=%s currency=%s ref=%s/%s",
        transaction_id, len(legs), total_debit, currency, reference_type, reference_id,
    )
    return transaction_id


# ═══════════════════════════════════════════════════════════════════════
# Готовые helper'ы для типовых сценариев
# ═══════════════════════════════════════════════════════════════════════

async def reserve_for_advertisement(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    advertisement_id: str,
    *,
    currency: str = "USDT",
    workflow_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """User Available → Advertisement Hold."""
    return await post_transaction(
        db,
        [
            LedgerLeg(LedgerAccountType.USER_AVAILABLE.value, user_id,
                      debit=amount, note=f"reserve for ad {advertisement_id}"),
            LedgerLeg(LedgerAccountType.ADVERTISEMENT_HOLD.value, user_id,
                      credit=amount, note=f"hold for ad {advertisement_id}"),
        ],
        currency=currency,
        reference_type="advertisement", reference_id=advertisement_id,
        workflow_id=workflow_id, correlation_id=correlation_id,
    )


async def move_ad_hold_to_escrow(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    trade_id: str,
    *,
    currency: str = "USDT",
    workflow_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """Advertisement Hold → Trade Escrow (при открытии сделки)."""
    return await post_transaction(
        db,
        [
            LedgerLeg(LedgerAccountType.ADVERTISEMENT_HOLD.value, user_id,
                      debit=amount, note=f"to escrow trade {trade_id}"),
            LedgerLeg(LedgerAccountType.USER_ESCROW.value, user_id,
                      credit=amount, note=f"escrow for trade {trade_id}"),
        ],
        currency=currency,
        reference_type="trade", reference_id=trade_id,
        workflow_id=workflow_id, correlation_id=correlation_id,
    )


async def reserve_seller_escrow(
    db: AsyncSession,
    seller_id: int,
    amount: Decimal,
    trade_id: str,
    *,
    currency: str = "USDT",
    workflow_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """User Available → Trade Escrow (для BUY-offer, когда юзер сразу даёт USDT)."""
    return await post_transaction(
        db,
        [
            LedgerLeg(LedgerAccountType.USER_AVAILABLE.value, seller_id,
                      debit=amount, note=f"direct escrow trade {trade_id}"),
            LedgerLeg(LedgerAccountType.USER_ESCROW.value, seller_id,
                      credit=amount, note=f"escrow for trade {trade_id}"),
        ],
        currency=currency,
        reference_type="trade", reference_id=trade_id,
        workflow_id=workflow_id, correlation_id=correlation_id,
    )


async def release_to_buyer(
    db: AsyncSession,
    seller_id: int,
    buyer_id: int,
    amount: Decimal,
    fee: Decimal,
    trade_id: str,
    *,
    currency: str = "USDT",
    workflow_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """Trade Escrow → Buyer Available + Platform Fee (завершение сделки)."""
    if fee < 0:
        raise HTTPException(500, "fee must be >= 0")
    if amount <= fee:
        raise HTTPException(500, "amount must be > fee")
    payout = amount - fee

    legs = [
        # Seller Escrow → Buyer Available + Platform fee
        LedgerLeg(LedgerAccountType.USER_ESCROW.value, seller_id,
                  debit=amount, note=f"release trade {trade_id}"),
        LedgerLeg(LedgerAccountType.USER_AVAILABLE.value, buyer_id,
                  credit=payout, note=f"payout from trade {trade_id}"),
    ]
    if fee > 0:
        legs.append(
            LedgerLeg(LedgerAccountType.PLATFORM_REVENUE.value, None,
                      credit=fee, note=f"fee from trade {trade_id}"),
        )

    return await post_transaction(
        db, legs, currency=currency,
        reference_type="trade", reference_id=trade_id,
        workflow_id=workflow_id, correlation_id=correlation_id,
    )


async def refund_escrow_to_ad_hold(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    trade_id: str,
    *,
    currency: str = "USDT",
    workflow_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """Trade Escrow → Advertisement Hold (cancel сделки если объявление ещё active)."""
    return await post_transaction(
        db,
        [
            LedgerLeg(LedgerAccountType.USER_ESCROW.value, user_id,
                      debit=amount, note=f"cancel trade {trade_id}"),
            LedgerLeg(LedgerAccountType.ADVERTISEMENT_HOLD.value, user_id,
                      credit=amount, note=f"return to ad hold from trade {trade_id}"),
        ],
        currency=currency,
        reference_type="trade", reference_id=trade_id,
        workflow_id=workflow_id, correlation_id=correlation_id,
    )


async def refund_escrow_to_available(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    trade_id: str,
    *,
    currency: str = "USDT",
    workflow_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """Trade Escrow → User Available (если объявление уже неактивно)."""
    return await post_transaction(
        db,
        [
            LedgerLeg(LedgerAccountType.USER_ESCROW.value, user_id,
                      debit=amount, note=f"cancel trade {trade_id}"),
            LedgerLeg(LedgerAccountType.USER_AVAILABLE.value, user_id,
                      credit=amount, note=f"return to available from trade {trade_id}"),
        ],
        currency=currency,
        reference_type="trade", reference_id=trade_id,
        workflow_id=workflow_id, correlation_id=correlation_id,
    )


async def release_ad_hold_to_available(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    advertisement_id: str,
    *,
    currency: str = "USDT",
    workflow_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """Advertisement Hold → User Available (при удалении/пауза объявления)."""
    return await post_transaction(
        db,
        [
            LedgerLeg(LedgerAccountType.ADVERTISEMENT_HOLD.value, user_id,
                      debit=amount, note=f"release ad {advertisement_id}"),
            LedgerLeg(LedgerAccountType.USER_AVAILABLE.value, user_id,
                      credit=amount, note=f"return from ad {advertisement_id}"),
        ],
        currency=currency,
        reference_type="advertisement", reference_id=advertisement_id,
        workflow_id=workflow_id, correlation_id=correlation_id,
    )

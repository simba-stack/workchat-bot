"""Balance service — атомарные операции с UserCoinBalance.

Все ops через SELECT ... FOR UPDATE чтобы предотвратить race condition.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import OperationLog, UserCoinBalance

logger = logging.getLogger(__name__)


async def get_or_create(db: AsyncSession, user_id: int, coin: str) -> UserCoinBalance:
    """SELECT ... FOR UPDATE (или создаёт)."""
    coin = coin.upper()
    res = await db.execute(
        select(UserCoinBalance)
        .where(UserCoinBalance.user_id == user_id, UserCoinBalance.coin_code == coin)
        .with_for_update()
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = UserCoinBalance(user_id=user_id, coin_code=coin, balance=Decimal("0"))
        db.add(row)
        await db.flush()
    return row


async def get_balance(db: AsyncSession, user_id: int, coin: str) -> Decimal:
    res = await db.execute(
        select(UserCoinBalance.balance)
        .where(UserCoinBalance.user_id == user_id, UserCoinBalance.coin_code == coin.upper())
    )
    val = res.scalar_one_or_none()
    return val or Decimal("0")


async def list_balances(db: AsyncSession, user_id: int) -> dict[str, Decimal]:
    res = await db.execute(
        select(UserCoinBalance.coin_code, UserCoinBalance.balance)
        .where(UserCoinBalance.user_id == user_id)
    )
    return {code: amt for code, amt in res.all()}


async def credit(
    db: AsyncSession,
    user_id: int,
    coin: str,
    amount: Decimal,
    *,
    op_type: str = "credit",
    note: str = "",
    txid: Optional[str] = None,
    ref_table: Optional[str] = None,
    ref_id: Optional[int] = None,
) -> UserCoinBalance:
    """Начислить."""
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    row = await get_or_create(db, user_id, coin)
    row.balance += amount
    db.add(OperationLog(
        user_id=user_id, type=op_type, amount_usdt=amount,
        balance_after=row.balance, txid=txid,
        ref_table=ref_table, ref_id=ref_id,
        note=f"[{coin.upper()}] {note}".strip(),
    ))
    await db.flush()
    return row


async def debit(
    db: AsyncSession,
    user_id: int,
    coin: str,
    amount: Decimal,
    *,
    op_type: str = "debit",
    note: str = "",
    txid: Optional[str] = None,
    ref_table: Optional[str] = None,
    ref_id: Optional[int] = None,
) -> UserCoinBalance:
    """Списать. HTTPException если недостаточно."""
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    row = await get_or_create(db, user_id, coin)
    if row.balance < amount:
        raise HTTPException(400, f"недостаточно {coin.upper()}: на балансе {row.balance}")
    row.balance -= amount
    db.add(OperationLog(
        user_id=user_id, type=op_type, amount_usdt=-amount,
        balance_after=row.balance, txid=txid,
        ref_table=ref_table, ref_id=ref_id,
        note=f"[{coin.upper()}] {note}".strip(),
    ))
    await db.flush()
    return row


async def transfer_atomic(
    db: AsyncSession,
    from_user_id: int,
    to_user_id: int,
    coin: str,
    amount: Decimal,
    note: str = "",
) -> tuple[UserCoinBalance, UserCoinBalance]:
    """Atomic перевод между юзерами. SELECT FOR UPDATE на обе строки."""
    if from_user_id == to_user_id:
        raise HTTPException(400, "нельзя себе же")
    # Lock в детерминированном порядке (по id) чтобы избежать deadlock
    first_id = min(from_user_id, to_user_id)
    second_id = max(from_user_id, to_user_id)
    await get_or_create(db, first_id, coin)
    await get_or_create(db, second_id, coin)
    from_row = await debit(db, from_user_id, coin, amount,
                           op_type="transfer_out", note=note,
                           ref_table="transfers")
    to_row = await credit(db, to_user_id, coin, amount,
                          op_type="transfer_in", note=note,
                          ref_table="transfers")
    return from_row, to_row

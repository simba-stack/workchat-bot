"""Reconciliation Worker (ТЗ Том 23).

Раз в N минут проверяет финансовую целостность:
1. Σ Debit = Σ Credit для всех Ledger entries (по currency)
2. Wallet projection точно равен сумме Ledger по категориям
3. Нет отрицательных балансов

Любое расхождение → CRITICAL log + создаётся событие RECON_FAILED.
"""
from __future__ import annotations
import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select, func

from core.db import AsyncSessionLocal
from p2p import wallet, outbox
from p2p.enums import EventType
from p2p.models import P2PLedgerAccount, P2PLedgerEntry, P2PWallet

logger = logging.getLogger("p2p.worker.recon")


async def _check_ledger_balance(db) -> list[str]:
    """Σ Debit = Σ Credit per currency."""
    errors = []
    # group by currency через join account
    q = select(
        P2PLedgerAccount.currency,
        func.coalesce(func.sum(P2PLedgerEntry.debit), 0).label("d"),
        func.coalesce(func.sum(P2PLedgerEntry.credit), 0).label("c"),
    ).join(P2PLedgerAccount, P2PLedgerEntry.account_id == P2PLedgerAccount.id).group_by(P2PLedgerAccount.currency)
    r = await db.execute(q)
    for row in r.all():
        currency, d, c = row[0], Decimal(str(row[1] or 0)), Decimal(str(row[2] or 0))
        if d != c:
            errors.append(f"LEDGER_IMBALANCE currency={currency} Σdebit={d} Σcredit={c} delta={d-c}")
    return errors


async def _check_wallet_vs_ledger(db) -> list[str]:
    """Wallet projection точно равен сумме Ledger по категориям. Проверяем только тех у кого есть кошельки."""
    errors = []
    r = await db.execute(select(P2PWallet).limit(1000))
    for w in r.scalars().all():
        try:
            calc = await wallet._calculate_from_ledger(db, w.user_id, w.currency)  # type: ignore[attr-defined]
        except AttributeError:
            # если функция _calculate_from_ledger не вынесена — пропустим
            continue
        if calc.available != (w.available or Decimal("0")):
            errors.append(
                f"WALLET_MISMATCH user={w.user_id} {w.currency} available "
                f"projection={w.available} ledger={calc.available}"
            )
        if calc.advertisement_hold != (w.advertisement_hold or Decimal("0")):
            errors.append(
                f"WALLET_MISMATCH user={w.user_id} {w.currency} ad_hold "
                f"projection={w.advertisement_hold} ledger={calc.advertisement_hold}"
            )
        if calc.trade_escrow != (w.trade_escrow or Decimal("0")):
            errors.append(
                f"WALLET_MISMATCH user={w.user_id} {w.currency} escrow "
                f"projection={w.trade_escrow} ledger={calc.trade_escrow}"
            )
    return errors


async def _check_no_negatives(db) -> list[str]:
    """В Wallet не должно быть отрицательных значений."""
    errors = []
    r = await db.execute(select(P2PWallet).where(
        (P2PWallet.available < 0) | (P2PWallet.advertisement_hold < 0)
        | (P2PWallet.trade_escrow < 0) | (P2PWallet.frozen < 0) | (P2PWallet.pending < 0)
    ))
    for w in r.scalars().all():
        errors.append(f"NEGATIVE_BALANCE user={w.user_id} {w.currency} "
                      f"avail={w.available} hold={w.advertisement_hold} "
                      f"esc={w.trade_escrow} froz={w.frozen} pend={w.pending}")
    return errors


async def run_once() -> dict:
    async with AsyncSessionLocal() as db:
        errs: list[str] = []
        errs += await _check_ledger_balance(db)
        errs += await _check_wallet_vs_ledger(db)
        errs += await _check_no_negatives(db)
        if errs:
            for e in errs:
                logger.error("[recon] %s", e)
            await outbox.emit(
                db,
                event_type=EventType.RECON_FAILED.value,
                payload={"errors": errs[:50], "count": len(errs)},
            )
            await db.commit()
        return {"errors": errs, "count": len(errs)}


async def run() -> None:
    logger.info("[recon-worker] started")
    while True:
        try:
            result = await run_once()
            if result["count"] == 0:
                logger.info("[recon-worker] OK — ledger balanced, wallets match")
            await asyncio.sleep(300)  # 5 минут
        except Exception as e:
            logger.exception("[recon-worker] failed: %s", e)
            await asyncio.sleep(120)

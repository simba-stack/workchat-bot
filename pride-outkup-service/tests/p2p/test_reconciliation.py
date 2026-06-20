"""Тесты Reconciliation worker — detect negative balance / ledger mismatch."""
from __future__ import annotations
from decimal import Decimal

import pytest
from sqlalchemy import text, select

from p2p import ledger, wallet
from p2p.enums import LedgerAccountType
from p2p.ledger import LedgerLeg
from p2p.workers import reconciliation
from p2p.models import P2PWallet


@pytest.mark.asyncio
async def test_recon_detects_negative_balance(db_session, user_factory, seed_wallet, monkeypatch):
    """Вручную INSERT негативный wallet → recon.run_once() возвращает ошибки."""
    u = await user_factory()
    await seed_wallet(u.id, amount="100")
    await db_session.commit()

    # Вручную ставим negative
    await db_session.execute(text(
        "UPDATE p2p_wallets SET available = -50 WHERE user_id = :u"
    ), {"u": u.id})
    await db_session.commit()

    res = await reconciliation.run_once()
    # Должны быть ошибки (или NEGATIVE_BALANCE, или WALLET_MISMATCH)
    assert res["count"] > 0, f"recon expected errors, got {res}"
    err_text = " ".join(res["errors"])
    assert ("NEGATIVE_BALANCE" in err_text) or ("WALLET_MISMATCH" in err_text)


@pytest.mark.asyncio
async def test_recon_detects_ledger_mismatch(db_session, user_factory, seed_wallet):
    """Insert orphan ledger entry → recon report содержит WALLET_MISMATCH."""
    u = await user_factory()
    await seed_wallet(u.id, amount="500")
    await db_session.commit()

    # Создаём дополнительный credit на USER_AVAILABLE напрямую (без double-entry счёта)
    # Чтобы не нарушить debit==credit инвариант сразу, поломаем wallet projection:
    # просто оставим ledger как есть, но обнулим wallet projection — projection ≠ ledger.
    await db_session.execute(text(
        "UPDATE p2p_wallets SET available = 0, advertisement_hold = 0, "
        "trade_escrow = 0, frozen = 0, pending = 0 WHERE user_id = :u"
    ), {"u": u.id})
    await db_session.commit()

    res = await reconciliation.run_once()
    assert res["count"] > 0
    err_text = " ".join(res["errors"])
    assert "WALLET_MISMATCH" in err_text or "MISMATCH" in err_text

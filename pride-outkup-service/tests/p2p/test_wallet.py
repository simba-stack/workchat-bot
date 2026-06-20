"""Тесты Wallet projection (consistency vs Ledger, негативные балансы)."""
from __future__ import annotations
from decimal import Decimal

import pytest
from fastapi import HTTPException

from p2p import ledger, wallet
from p2p.enums import LedgerAccountType
from p2p.ledger import LedgerLeg


@pytest.mark.asyncio
async def test_wallet_breakdown_matches_ledger(db_session, user_factory, seed_wallet):
    """После серии операций wallet.available == сумма Ledger credits-debits для USER_AVAILABLE."""
    u = await user_factory()
    await seed_wallet(u.id, amount="2000")

    await ledger.reserve_for_advertisement(
        db_session, user_id=u.id, amount=Decimal("400"), advertisement_id="A",
    )
    await ledger.reserve_for_advertisement(
        db_session, user_id=u.id, amount=Decimal("200"), advertisement_id="B",
    )
    await ledger.move_ad_hold_to_escrow(
        db_session, user_id=u.id, amount=Decimal("150"), trade_id="T",
    )
    await wallet.update_wallet_from_ledger(db_session, u.id, "USDT")

    # Reconcile должен вернуть ok=True
    rec = await wallet.reconcile(db_session, u.id, "USDT")
    assert rec["ok"], f"reconcile failed: {rec}"

    breakdown = await wallet.get_breakdown(db_session, u.id, "USDT")
    # Должно: available = 2000 - 400 - 200 = 1400
    # ad_hold  = 400 + 200 - 150 = 450
    # escrow   = 150
    assert breakdown.available == Decimal("1400")
    assert breakdown.advertisement_hold == Decimal("450")
    assert breakdown.trade_escrow == Decimal("150")


@pytest.mark.asyncio
async def test_negative_balance_raises(db_session, user_factory):
    """Попытка release когда escrow==0 → ошибка через инвариант wallet."""
    seller = await user_factory()
    buyer = await user_factory()
    # escrow ноль — release списывает с пустого счёта → wallet projection поймает
    await ledger.release_to_buyer(
        db_session,
        seller_id=seller.id, buyer_id=buyer.id,
        amount=Decimal("100"), fee=Decimal("0"),
        trade_id="t-neg",
    )
    with pytest.raises(HTTPException) as exc:
        await wallet.update_wallet_from_ledger(db_session, seller.id, "USDT")
    assert exc.value.status_code == 500
    assert "negative" in str(exc.value.detail).lower()

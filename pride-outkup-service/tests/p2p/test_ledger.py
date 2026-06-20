"""Тесты Ledger Engine (double-entry, immutability, helpers)."""
from __future__ import annotations
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from p2p import ledger, wallet
from p2p.enums import LedgerAccountType
from p2p.ledger import LedgerLeg
from p2p.models import P2PLedgerEntry, P2PLedgerAccount


@pytest.mark.asyncio
async def test_ledger_sum_debit_equals_credit(db_session, user_factory, seed_wallet):
    """Σ debit == Σ credit per currency после пяти случайных транзакций."""
    u = await user_factory()
    u2 = await user_factory()
    await seed_wallet(u.id, amount="5000")
    await seed_wallet(u2.id, amount="5000")

    # 5 разных операций
    await ledger.reserve_for_advertisement(
        db_session, user_id=u.id, amount=Decimal("100"), advertisement_id="ad-1",
    )
    await ledger.reserve_for_advertisement(
        db_session, user_id=u.id, amount=Decimal("50"), advertisement_id="ad-2",
    )
    await ledger.move_ad_hold_to_escrow(
        db_session, user_id=u.id, amount=Decimal("30"), trade_id="t-1",
    )
    await ledger.freeze_balance(
        db_session, user_id=u2.id, amount=Decimal("70"), reason="risk",
    )
    await ledger.release_ad_hold_to_available(
        db_session, user_id=u.id, amount=Decimal("20"), advertisement_id="ad-1",
    )

    # Σ всех debit/credit
    r = await db_session.execute(
        select(
            func.coalesce(func.sum(P2PLedgerEntry.debit), 0),
            func.coalesce(func.sum(P2PLedgerEntry.credit), 0),
        ).where(P2PLedgerEntry.currency == "USDT")
    )
    sum_d, sum_c = r.one()
    assert Decimal(str(sum_d)) == Decimal(str(sum_c)), (
        f"ledger imbalance: debit={sum_d} credit={sum_c}"
    )


@pytest.mark.asyncio
async def test_reserve_for_advertisement_moves_balance(db_session, user_factory, seed_wallet):
    """available → advertisement_hold."""
    u = await user_factory()
    await seed_wallet(u.id, amount="1000")
    before = await wallet.get_breakdown(db_session, u.id, "USDT")
    assert before.available == Decimal("1000")

    await ledger.reserve_for_advertisement(
        db_session, user_id=u.id, amount=Decimal("400"), advertisement_id="ad-x",
    )
    await wallet.update_wallet_from_ledger(db_session, u.id, "USDT")
    after = await wallet.get_breakdown(db_session, u.id, "USDT")

    assert after.available == Decimal("600")
    assert after.advertisement_hold == Decimal("400")


@pytest.mark.asyncio
async def test_release_to_buyer_moves_to_buyer_and_fee(db_session, user_factory, seed_wallet):
    """trade_escrow → buyer.available + platform_fee."""
    seller = await user_factory()
    buyer = await user_factory()
    await seed_wallet(seller.id, amount="1000")

    # Seller: available -> ad_hold -> escrow
    await ledger.reserve_for_advertisement(
        db_session, user_id=seller.id, amount=Decimal("200"), advertisement_id="ad",
    )
    await ledger.move_ad_hold_to_escrow(
        db_session, user_id=seller.id, amount=Decimal("200"), trade_id="t-1",
    )
    await wallet.update_wallet_from_ledger(db_session, seller.id, "USDT")
    seller_before = await wallet.get_breakdown(db_session, seller.id, "USDT")
    assert seller_before.trade_escrow == Decimal("200")

    # Release: 200 escrow → buyer (199) + platform_fee (1)
    await ledger.release_to_buyer(
        db_session,
        seller_id=seller.id, buyer_id=buyer.id,
        amount=Decimal("200"), fee=Decimal("1"),
        trade_id="t-1",
    )
    await wallet.update_wallet_from_ledger(db_session, seller.id, "USDT")
    await wallet.update_wallet_from_ledger(db_session, buyer.id, "USDT")

    seller_after = await wallet.get_breakdown(db_session, seller.id, "USDT")
    buyer_after = await wallet.get_breakdown(db_session, buyer.id, "USDT")
    assert seller_after.trade_escrow == Decimal("0")
    assert buyer_after.available == Decimal("199")

    # Platform fee = 1 в PLATFORM_REVENUE
    fee_acc = await ledger.get_account(
        db_session, LedgerAccountType.PLATFORM_REVENUE.value, None, "USDT",
    )
    assert fee_acc is not None
    bal = await ledger.get_account_balance(db_session, fee_acc)
    assert bal == Decimal("1")


@pytest.mark.asyncio
async def test_refund_escrow_to_ad_hold(db_session, user_factory, seed_wallet):
    """trade_escrow → advertisement_hold на cancel."""
    seller = await user_factory()
    await seed_wallet(seller.id, amount="500")

    await ledger.reserve_for_advertisement(
        db_session, user_id=seller.id, amount=Decimal("300"), advertisement_id="ad-r",
    )
    await ledger.move_ad_hold_to_escrow(
        db_session, user_id=seller.id, amount=Decimal("100"), trade_id="t-r",
    )
    await wallet.update_wallet_from_ledger(db_session, seller.id, "USDT")
    before = await wallet.get_breakdown(db_session, seller.id, "USDT")
    assert before.advertisement_hold == Decimal("200")
    assert before.trade_escrow == Decimal("100")

    await ledger.refund_escrow_to_ad_hold(
        db_session, user_id=seller.id, amount=Decimal("100"),
        trade_id="t-r",
    )
    await wallet.update_wallet_from_ledger(db_session, seller.id, "USDT")
    after = await wallet.get_breakdown(db_session, seller.id, "USDT")

    assert after.advertisement_hold == Decimal("300")
    assert after.trade_escrow == Decimal("0")


@pytest.mark.asyncio
async def test_freeze_balance(db_session, user_factory, seed_wallet):
    """available → frozen."""
    u = await user_factory()
    await seed_wallet(u.id, amount="500")

    await ledger.freeze_balance(
        db_session, user_id=u.id, amount=Decimal("150"), reason="aml",
    )
    await wallet.update_wallet_from_ledger(db_session, u.id, "USDT")
    after = await wallet.get_breakdown(db_session, u.id, "USDT")

    assert after.available == Decimal("350")
    assert after.frozen == Decimal("150")


@pytest.mark.asyncio
async def test_ledger_imbalance_rejected(db_session, user_factory):
    """post_transaction с неравными legs → HTTPException 500."""
    u = await user_factory()
    with pytest.raises(HTTPException) as exc:
        await ledger.post_transaction(
            db_session,
            [
                LedgerLeg(LedgerAccountType.USER_AVAILABLE.value, u.id,
                          debit=Decimal("100")),
                LedgerLeg(LedgerAccountType.USER_ESCROW.value, u.id,
                          credit=Decimal("99")),
            ],
            currency="USDT",
        )
    assert exc.value.status_code == 500
    assert "imbalance" in str(exc.value.detail).lower() or "debit" in str(exc.value.detail).lower()

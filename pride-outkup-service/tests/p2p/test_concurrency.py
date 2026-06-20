"""Concurrency-тесты: race на одно объявление, double confirm."""
from __future__ import annotations
import asyncio
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from p2p import wallet
from p2p.enums import LedgerAccountType, TradeStatus
from p2p.models import P2PTrade, P2PLedgerEntry, P2PLedgerAccount
from p2p.orchestrator import run_workflow
from p2p.workflows import (
    create_trade as wf_create_trade,
    mark_paid as wf_mark_paid,
    confirm_payment as wf_confirm_payment,
)


@pytest.mark.asyncio
async def test_race_create_trade_same_ad(_session_factory, user_factory, seed_wallet, ad_factory, db_session):
    """5 параллельных create_trade на одно ad с суммой 100 каждая.

    Ad имеет 300 USDT доступно — пройти должно ровно 3, остальные → 400.
    """
    seller = await user_factory()
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="300", price="80",
                              min_order_fiat="100", max_order_fiat="24000")
    # buyers
    buyers = [await user_factory() for _ in range(5)]
    await db_session.commit()  # сделать данные видимыми в other sessions

    async def _try_buy(buyer_id: int) -> dict | str:
        async with _session_factory() as sess:
            try:
                r = await run_workflow(
                    sess,
                    workflow_type="create_trade",
                    user_id=buyer_id,
                    input_payload={"advertisement_id": ad_id, "amount_crypto": "100"},
                    handler=wf_create_trade.handle,
                )
                await sess.commit()
                return r
            except HTTPException as e:
                await sess.rollback()
                return f"http:{e.status_code}"
            except Exception as e:
                await sess.rollback()
                return f"exc:{type(e).__name__}"

    results = await asyncio.gather(*[_try_buy(b.id) for b in buyers])
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if not isinstance(r, dict)]

    # Должно успешно пройти ровно 3 (300 USDT / 100 amount)
    assert len(successes) == 3, f"expected 3 successes, got {len(successes)}: {results}"
    assert len(failures) == 2

    # В БД ровно 3 trade
    async with _session_factory() as check:
        cnt = (await check.execute(
            select(func.count(P2PTrade.id))
        )).scalar()
        assert cnt == 3


@pytest.mark.asyncio
async def test_race_confirm_payment_double(_session_factory, user_factory, seed_wallet, ad_factory, db_session):
    """3 параллельных confirm_payment → только один COMPLETED, fee posted один раз."""
    seller = await user_factory()
    buyer = await user_factory()
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="500", price="80",
                              min_order_fiat="100", max_order_fiat="40000")

    # Открыть trade + mark_paid в основной сессии
    res = await run_workflow(
        db_session, workflow_type="create_trade", user_id=buyer.id,
        input_payload={"advertisement_id": ad_id, "amount_crypto": "100"},
        handler=wf_create_trade.handle,
    )
    trade_id = res["trade_id"]
    await run_workflow(
        db_session, workflow_type="mark_paid", user_id=buyer.id,
        input_payload={"trade_id": trade_id}, handler=wf_mark_paid.handle,
    )
    await db_session.commit()

    async def _try_confirm() -> str:
        async with _session_factory() as sess:
            try:
                r = await run_workflow(
                    sess, workflow_type="confirm_payment", user_id=seller.id,
                    input_payload={"trade_id": trade_id},
                    handler=wf_confirm_payment.handle,
                )
                await sess.commit()
                return f"ok:{r.get('status')}"
            except HTTPException as e:
                await sess.rollback()
                return f"http:{e.status_code}"
            except Exception as e:
                await sess.rollback()
                return f"exc:{type(e).__name__}"

    results = await asyncio.gather(*[_try_confirm() for _ in range(3)])
    # хотя бы один успех; остальные либо idempotent-ok либо ошибка состояния
    ok_count = sum(1 for r in results if r.startswith("ok:"))
    assert ok_count >= 1

    # Trade должен быть COMPLETED ровно один раз
    async with _session_factory() as check:
        trade = (await check.execute(
            select(P2PTrade).where(P2PTrade.id == trade_id)
        )).scalar_one()
        assert trade.status == TradeStatus.COMPLETED.value

        # Платформенная комиссия списана ровно один раз
        r = await check.execute(
            select(
                func.coalesce(func.sum(P2PLedgerEntry.credit), 0)
            ).select_from(P2PLedgerEntry).join(
                P2PLedgerAccount, P2PLedgerAccount.id == P2PLedgerEntry.account_id,
            ).where(
                P2PLedgerAccount.account_type == LedgerAccountType.PLATFORM_REVENUE.value,
                P2PLedgerEntry.reference_id == trade_id,
            )
        )
        total_fee = Decimal(str(r.scalar() or 0))
        # fee_pct = 0.5%, amount=100 → fee=0.5
        assert total_fee == (trade.fee_crypto or Decimal("0"))
        assert total_fee > Decimal("0")

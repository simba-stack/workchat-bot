"""E2E edge-cases: cancel, dispute, resolve (buyer/seller/split), idempotency."""
from __future__ import annotations
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from p2p import wallet
from p2p.enums import TradeStatus, P2PUserRole, DisputeResolution
from p2p.models import P2PTrade
from p2p.orchestrator import run_workflow
from p2p.workflows import (
    create_trade as wf_create_trade,
    mark_paid as wf_mark_paid,
    confirm_payment as wf_confirm_payment,
    cancel_trade as wf_cancel,
    open_dispute as wf_open_dispute,
    resolve_dispute as wf_resolve,
)


async def _open_trade(db_session, buyer_id: int, ad_id: str, amount: str = "100"):
    return await run_workflow(
        db_session,
        workflow_type="create_trade",
        user_id=buyer_id,
        input_payload={"advertisement_id": ad_id, "amount_crypto": amount},
        handler=wf_create_trade.handle,
    )


@pytest.mark.asyncio
async def test_cancel_trade_returns_escrow(db_session, user_factory, seed_wallet, ad_factory):
    """Cancel в WAITING_FOR_PAYMENT → seller.advertisement_hold восстановлен."""
    seller = await user_factory()
    buyer = await user_factory()
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="500", price="80",
                              min_order_fiat="100", max_order_fiat="40000")

    trade_res = await _open_trade(db_session, buyer.id, ad_id, amount="200")
    trade_id = trade_res["trade_id"]

    seller_mid = await wallet.get_breakdown(db_session, seller.id, "USDT")
    # escrow=200, ad_hold=300, avail=1500
    assert seller_mid.trade_escrow == Decimal("200")
    assert seller_mid.advertisement_hold == Decimal("300")

    # Cancel buyer'ом (он в WAITING_FOR_PAYMENT — это допустимо)
    await run_workflow(
        db_session,
        workflow_type="cancel_trade",
        user_id=buyer.id,
        input_payload={"trade_id": trade_id, "reason": "передумал"},
        handler=wf_cancel.handle,
    )

    seller_after = await wallet.get_breakdown(db_session, seller.id, "USDT")
    assert seller_after.trade_escrow == Decimal("0")
    assert seller_after.advertisement_hold == Decimal("500")


@pytest.mark.asyncio
async def test_open_dispute_freezes_release(db_session, user_factory, seed_wallet, ad_factory):
    """После open_dispute попытка confirm_payment → HTTPException 409 (status != PAYMENT_MARKED)."""
    seller = await user_factory()
    buyer = await user_factory()
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="500", price="80",
                              min_order_fiat="100", max_order_fiat="40000")

    res = await _open_trade(db_session, buyer.id, ad_id, "100")
    trade_id = res["trade_id"]

    await run_workflow(
        db_session,
        workflow_type="mark_paid",
        user_id=buyer.id,
        input_payload={"trade_id": trade_id},
        handler=wf_mark_paid.handle,
    )
    # buyer открывает dispute
    await run_workflow(
        db_session,
        workflow_type="open_dispute",
        user_id=buyer.id,
        input_payload={"trade_id": trade_id, "reason": "продавец молчит"},
        handler=wf_open_dispute.handle,
    )

    # Теперь confirm_payment от seller'а должен упасть (trade в DISPUTE_OPENED)
    with pytest.raises(HTTPException) as exc:
        await run_workflow(
            db_session,
            workflow_type="confirm_payment",
            user_id=seller.id,
            input_payload={"trade_id": trade_id},
            handler=wf_confirm_payment.handle,
        )
    assert exc.value.status_code in (409, 422, 500)


@pytest.mark.asyncio
async def test_resolve_dispute_buyer(db_session, user_factory, seed_wallet, ad_factory):
    """Арбитр BUYER → buyer получает USDT."""
    seller = await user_factory()
    buyer = await user_factory()
    arb = await user_factory(username="arbitrator")
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="500", price="80",
                              min_order_fiat="100", max_order_fiat="40000")
    res = await _open_trade(db_session, buyer.id, ad_id, "100")
    trade_id = res["trade_id"]

    await run_workflow(
        db_session, workflow_type="mark_paid", user_id=buyer.id,
        input_payload={"trade_id": trade_id}, handler=wf_mark_paid.handle,
    )
    open_res = await run_workflow(
        db_session, workflow_type="open_dispute", user_id=buyer.id,
        input_payload={"trade_id": trade_id, "reason": "test"},
        handler=wf_open_dispute.handle,
    )
    dispute_id = open_res["dispute_id"]

    await run_workflow(
        db_session, workflow_type="resolve_dispute", user_id=arb.id,
        input_payload={"dispute_id": dispute_id, "resolution": "BUYER", "arbitrator_note": "ok"},
        handler=wf_resolve.handle,
        actor_role=P2PUserRole.ARBITRATOR.value,
    )

    trade = (await db_session.execute(
        select(P2PTrade).where(P2PTrade.id == trade_id)
    )).scalar_one()
    assert trade.status == TradeStatus.COMPLETED.value

    buyer_b = await wallet.get_breakdown(db_session, buyer.id, "USDT")
    assert buyer_b.available > Decimal("0")


@pytest.mark.asyncio
async def test_resolve_dispute_seller(db_session, user_factory, seed_wallet, ad_factory):
    """Арбитр SELLER → seller получает refund."""
    seller = await user_factory()
    buyer = await user_factory()
    arb = await user_factory(username="arb_seller")
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="500", price="80",
                              min_order_fiat="100", max_order_fiat="40000")
    trade_res = await _open_trade(db_session, buyer.id, ad_id, "100")
    trade_id = trade_res["trade_id"]
    await run_workflow(
        db_session, workflow_type="mark_paid", user_id=buyer.id,
        input_payload={"trade_id": trade_id}, handler=wf_mark_paid.handle,
    )
    open_res = await run_workflow(
        db_session, workflow_type="open_dispute", user_id=seller.id,
        input_payload={"trade_id": trade_id, "reason": "fake payment proof"},
        handler=wf_open_dispute.handle,
    )

    seller_before = await wallet.get_breakdown(db_session, seller.id, "USDT")
    await run_workflow(
        db_session, workflow_type="resolve_dispute", user_id=arb.id,
        input_payload={"dispute_id": open_res["dispute_id"], "resolution": "SELLER",
                       "arbitrator_note": "buyer obman"},
        handler=wf_resolve.handle,
        actor_role=P2PUserRole.ARBITRATOR.value,
    )

    seller_after = await wallet.get_breakdown(db_session, seller.id, "USDT")
    # escrow вернулся в available
    assert seller_after.trade_escrow == Decimal("0")
    assert seller_after.available == seller_before.available + Decimal("100")

    trade = (await db_session.execute(
        select(P2PTrade).where(P2PTrade.id == trade_id)
    )).scalar_one()
    assert trade.status == TradeStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_resolve_dispute_split(db_session, user_factory, seed_wallet, ad_factory):
    """30/70 split: buyer 30, seller 70."""
    seller = await user_factory()
    buyer = await user_factory()
    arb = await user_factory(username="arb_split")
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="500", price="80",
                              min_order_fiat="100", max_order_fiat="40000")
    trade_res = await _open_trade(db_session, buyer.id, ad_id, "100")
    trade_id = trade_res["trade_id"]
    await run_workflow(
        db_session, workflow_type="mark_paid", user_id=buyer.id,
        input_payload={"trade_id": trade_id}, handler=wf_mark_paid.handle,
    )
    open_res = await run_workflow(
        db_session, workflow_type="open_dispute", user_id=buyer.id,
        input_payload={"trade_id": trade_id, "reason": "частично оплатил"},
        handler=wf_open_dispute.handle,
    )

    seller_before = await wallet.get_breakdown(db_session, seller.id, "USDT")
    await run_workflow(
        db_session, workflow_type="resolve_dispute", user_id=arb.id,
        input_payload={
            "dispute_id": open_res["dispute_id"],
            "resolution": "SPLIT",
            "amount_to_buyer": "30",
            "arbitrator_note": "split 30/70",
        },
        handler=wf_resolve.handle,
        actor_role=P2PUserRole.ARBITRATOR.value,
    )

    buyer_b = await wallet.get_breakdown(db_session, buyer.id, "USDT")
    seller_after = await wallet.get_breakdown(db_session, seller.id, "USDT")

    assert buyer_b.available == Decimal("30")
    # Seller получил обратно 70 в available
    assert seller_after.available == seller_before.available + Decimal("70")


@pytest.mark.asyncio
async def test_idempotency_replay(db_session, user_factory, seed_wallet, ad_factory):
    """Повторный create_trade с тем же idempotency_key → кеш, не дубликат."""
    seller = await user_factory()
    buyer = await user_factory()
    await seed_wallet(seller.id, amount="2000")
    ad_id = await ad_factory(seller.id, amount="500", price="80",
                              min_order_fiat="100", max_order_fiat="40000")

    key = "idemp-key-trade-1"
    res1 = await run_workflow(
        db_session,
        workflow_type="create_trade",
        user_id=buyer.id,
        input_payload={"advertisement_id": ad_id, "amount_crypto": "100"},
        handler=wf_create_trade.handle,
        idempotency_key=key,
        endpoint="/api/v2/trades",
    )
    # Commit чтобы idempotency_key стал виден следующему вызову
    await db_session.commit()

    res2 = await run_workflow(
        db_session,
        workflow_type="create_trade",
        user_id=buyer.id,
        input_payload={"advertisement_id": ad_id, "amount_crypto": "100"},
        handler=wf_create_trade.handle,
        idempotency_key=key,
        endpoint="/api/v2/trades",
    )

    assert res1["trade_id"] == res2["trade_id"]

    # Trade существует ровно один
    from sqlalchemy import func
    count = (await db_session.execute(
        select(func.count(P2PTrade.id))
    )).scalar()
    assert count == 1

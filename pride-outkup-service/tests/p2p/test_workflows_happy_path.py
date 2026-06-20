"""E2E happy-path: create_ad → create_trade → mark_paid → confirm_payment."""
from __future__ import annotations
from decimal import Decimal

import pytest
from sqlalchemy import select, func

from p2p import wallet
from p2p.enums import TradeStatus
from p2p.models import (
    P2PAdvertisement, P2PTrade, P2PAuditLog, P2POutbox,
)
from p2p.orchestrator import run_workflow
from p2p.workflows import (
    create_trade as wf_create_trade,
    mark_paid as wf_mark_paid,
    confirm_payment as wf_confirm_payment,
)


@pytest.mark.asyncio
async def test_full_trade_happy(db_session, user_factory, seed_wallet, ad_factory):
    seller = await user_factory(username="seller_happy")
    buyer = await user_factory(username="buyer_happy")
    # seed seller с запасом USDT
    await seed_wallet(seller.id, amount="2000")

    # Цена 80 RUB за USDT, ad на 1000 USDT
    ad_id = await ad_factory(
        owner_id=seller.id, type="SELL",
        amount="1000", price="80",
        min_order_fiat="100", max_order_fiat="80000",
    )

    # Создать сделку на 100 USDT (8000 RUB)
    res_create = await run_workflow(
        db_session,
        workflow_type="create_trade",
        user_id=buyer.id,
        input_payload={
            "advertisement_id": ad_id,
            "amount_crypto": "100",
        },
        handler=wf_create_trade.handle,
    )
    trade_id = res_create["trade_id"]
    assert res_create["status"] == TradeStatus.WAITING_FOR_PAYMENT.value

    # Buyer mark_paid
    res_paid = await run_workflow(
        db_session,
        workflow_type="mark_paid",
        user_id=buyer.id,
        input_payload={"trade_id": trade_id},
        handler=wf_mark_paid.handle,
    )
    assert res_paid["status"] == TradeStatus.PAYMENT_MARKED.value

    # Seller confirm_payment
    res_conf = await run_workflow(
        db_session,
        workflow_type="confirm_payment",
        user_id=seller.id,
        input_payload={"trade_id": trade_id},
        handler=wf_confirm_payment.handle,
    )
    assert res_conf["status"] == TradeStatus.COMPLETED.value

    # Проверки
    trade = (await db_session.execute(
        select(P2PTrade).where(P2PTrade.id == trade_id)
    )).scalar_one()
    assert trade.status == TradeStatus.COMPLETED.value
    assert trade.completed_at is not None

    ad = (await db_session.execute(
        select(P2PAdvertisement).where(P2PAdvertisement.id == ad_id)
    )).scalar_one()
    # ad.amount_available == 900 (1000 - 100)
    assert ad.available_amount == Decimal("900")

    # Buyer получил 100 - fee
    buyer_b = await wallet.get_breakdown(db_session, buyer.id, "USDT")
    fee = trade.fee_crypto or Decimal("0")
    assert buyer_b.available == (Decimal("100") - fee)

    # Seller потерял 100 (минус — escrow ушёл buyer'у)
    seller_b = await wallet.get_breakdown(db_session, seller.id, "USDT")
    # Изначально 2000, ad reserved 1000 в ad_hold, после complete: ad_hold=900, escrow=0, avail=1000
    assert seller_b.available == Decimal("1000")
    assert seller_b.advertisement_hold == Decimal("900")
    assert seller_b.trade_escrow == Decimal("0")

    # Audit: минимум по 1 записи на каждый workflow + завершение
    audit_count = (await db_session.execute(
        select(func.count(P2PAuditLog.id))
    )).scalar()
    # create_ad (created + workflow.completed), create_trade (trade.created + wf.completed),
    # mark_paid (payment_marked + wf.completed), confirm_payment (trade.completed + wf.completed)
    assert audit_count >= 4

    # Outbox events: ADVERTISEMENT_CREATED, TRADE_CREATED, TRADE_PAYMENT_MARKED, TRADE_COMPLETED
    outbox_count = (await db_session.execute(
        select(func.count(P2POutbox.id))
    )).scalar()
    assert outbox_count >= 4

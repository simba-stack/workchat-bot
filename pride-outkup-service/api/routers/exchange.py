"""V1 Exchange — обмен с PRIDE (мы контрагент)."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from core.services import order_service, settings_kv

router = APIRouter()


@router.get("/rate")
async def get_rate(db: AsyncSession = Depends(get_db)):
    """Актуальный курс PRIDE (синкается из JARVIS каждые 30 сек)."""
    buy = await settings_kv.get_rate_buy(db)
    sell = await settings_kv.get_rate_sell(db)
    fee = await settings_kv.get_fee_v1_pct(db)
    return {
        "buy": float(buy),
        "sell": float(sell),
        "fee_pct": float(fee),
    }


@router.post("/buy_usdt")
async def buy_usdt(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Клиент платит RUB → получает USDT."""
    try:
        amount_rub = Decimal(str(payload.get("amount_rub", "0")))
    except Exception:
        raise HTTPException(400, "invalid amount_rub")
    if amount_rub <= 0:
        raise HTTPException(400, "amount_rub must be > 0")
    payment_method = (payload.get("payment_method") or "").strip()
    if not payment_method:
        raise HTTPException(400, "payment_method required")

    destination = (payload.get("destination") or "balance").strip()
    if destination not in ("balance", "trc20"):
        raise HTTPException(400, "destination must be balance or trc20")
    destination_addr = payload.get("destination_addr")
    if destination == "trc20":
        if not destination_addr:
            destination_addr = user.trc20_address
        if not destination_addr or not destination_addr.startswith("T") or len(destination_addr) < 30:
            raise HTTPException(400, "valid TRC20 address required for trc20 destination")

    order = await order_service.create_buy_order(
        db, user, amount_rub, payment_method, destination, destination_addr,
    )
    return {
        "ok": True,
        "order_id": order.id,
        "order_number": order.order_number,
        "amount_usdt": float(order.amount_usdt),
        "rate": float(order.rate_rub_per_usdt),
    }


@router.post("/sell_usdt")
async def sell_usdt(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Клиент даёт USDT → получает RUB на банк."""
    try:
        amount_usdt = Decimal(str(payload.get("amount_usdt", "0")))
    except Exception:
        raise HTTPException(400, "invalid amount_usdt")
    if amount_usdt <= 0:
        raise HTTPException(400, "amount_usdt must be > 0")

    payment_method = (payload.get("payment_method") or "").strip()
    bank = (payload.get("bank") or payment_method).strip()
    payout_target = (payload.get("payout_target") or payload.get("bank_card_or_phone") or "").strip()
    source = (payload.get("source") or "balance").strip()
    if source not in ("balance", "incoming"):
        raise HTTPException(400, "source must be balance or incoming")
    if not bank or not payout_target:
        raise HTTPException(400, "bank and payout_target required")

    try:
        order = await order_service.create_sell_order(
            db, user, amount_usdt, payment_method, bank, payout_target, source,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "ok": True,
        "order_id": order.id,
        "order_number": order.order_number,
        "amount_rub": float(order.amount_rub),
        "rate": float(order.rate_rub_per_usdt),
    }

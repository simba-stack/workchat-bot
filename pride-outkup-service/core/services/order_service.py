"""Order сервис — V1 (PRIDE контрагент).

Создание/обновление заявок типа buy_usdt | sell_usdt | business_outkup.
Расчёт сумм по курсу, генерация order_number, эмит событий в JARVIS.
"""
import asyncio
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Order, User
from core.services import jarvis_sync, settings_kv

logger = logging.getLogger(__name__)


async def _next_order_number(db: AsyncSession) -> str:
    res = await db.execute(select(func.count(Order.id)))
    n = (res.scalar() or 0) + 1
    return f"#O{n:04d}"


def _calc_usdt_for_buy(amount_rub: Decimal, rate: Decimal, pct_fee: Decimal) -> Decimal:
    """Купить USDT за RUB: USDT = RUB / rate × (1 − fee%).
    Комиссия учтена — клиент получает меньше чем по чистому курсу.
    """
    if rate <= 0:
        raise ValueError("rate must be > 0")
    gross = amount_rub / rate
    net = gross * (Decimal("1") - pct_fee / Decimal("100"))
    return net.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _calc_rub_for_sell(amount_usdt: Decimal, rate: Decimal) -> Decimal:
    """Продать USDT за RUB: RUB = USDT × sell_rate.
    Курс sell уже включает нашу маржу.
    """
    rub = amount_usdt * rate
    return rub.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def create_buy_order(
    db: AsyncSession,
    user: User,
    amount_rub: Decimal,
    payment_method: str,
    destination: str = "balance",
    destination_addr: Optional[str] = None,
) -> Order:
    """Клиент покупает USDT за RUB."""
    rate = await settings_kv.get_rate_buy(db)
    fee = await settings_kv.get_fee_v1_pct(db)
    amount_usdt = _calc_usdt_for_buy(amount_rub, rate, fee)

    order = Order(
        order_number=await _next_order_number(db),
        user_id=user.id,
        kind="buy_usdt",
        amount_rub=amount_rub,
        amount_rub_remaining=amount_rub,
        rate_rub_per_usdt=rate,
        amount_usdt=amount_usdt,
        pct_fee=fee,
        destination=destination,
        destination_addr=destination_addr,
        payment_method=payment_method,
        status="pending",
        source="miniapp",
    )
    db.add(order)
    await db.flush()

    # Webhook в JARVIS — fire & forget
    asyncio.create_task(jarvis_sync.send_event("order.created", {
        "order_id": order.id,
        "order_number": order.order_number,
        "user": {"tg_id": user.tg_id, "username": user.username, "is_partner": user.is_partner},
        "kind": order.kind,
        "amount_rub": float(order.amount_rub),
        "amount_usdt": float(order.amount_usdt),
        "rate": float(order.rate_rub_per_usdt),
        "payment_method": order.payment_method,
        "destination": order.destination,
        "status": order.status,
    }))

    logger.info("[order] created %s: buy %s₽ → %sUSDT", order.order_number, amount_rub, amount_usdt)
    return order


async def create_sell_order(
    db: AsyncSession,
    user: User,
    amount_usdt: Decimal,
    payment_method: str,
    bank: str,
    payout_target: str,
    source: str = "balance",
) -> Order:
    """Клиент продаёт USDT за RUB.

    source: 'balance' (списываем с user.balance_usdt) или 'incoming'
            (клиент пришлёт на наш hot-wallet — мы тогда зачислим).
    """
    if source == "balance":
        if user.balance_usdt < amount_usdt:
            raise ValueError(f"insufficient balance: have {user.balance_usdt}, need {amount_usdt}")

    rate = await settings_kv.get_rate_sell(db)
    fee = await settings_kv.get_fee_v1_pct(db)
    amount_rub = _calc_rub_for_sell(amount_usdt, rate)

    order = Order(
        order_number=await _next_order_number(db),
        user_id=user.id,
        kind="sell_usdt",
        amount_rub=amount_rub,
        amount_rub_remaining=amount_rub,
        rate_rub_per_usdt=rate,
        amount_usdt=amount_usdt,
        pct_fee=fee,
        destination="rub",  # destination для sell — это RUB на банк
        bank_out=bank,
        payment_method=payment_method,
        payout_target=payout_target,
        status="pending",
        source="miniapp",
        extra={"source_usdt": source},
    )
    db.add(order)

    # Списываем USDT с баланса в hold (через operations_log + balance)
    if source == "balance":
        from core.models import OperationLog
        balance_before = user.balance_usdt
        user.balance_usdt -= amount_usdt
        db.add(OperationLog(
            user_id=user.id, type="hold",
            amount_usdt=-amount_usdt,
            balance_before=balance_before, balance_after=user.balance_usdt,
            ref_table="orders", note=f"sell hold for order {order.order_number}",
        ))

    await db.flush()

    asyncio.create_task(jarvis_sync.send_event("order.created", {
        "order_id": order.id,
        "order_number": order.order_number,
        "user": {"tg_id": user.tg_id, "username": user.username},
        "kind": "sell_usdt",
        "amount_rub": float(amount_rub),
        "amount_usdt": float(amount_usdt),
        "rate": float(rate),
        "bank": bank,
        "payout_target": payout_target,
    }))

    logger.info("[order] sell %s: %sUSDT → %s₽ on %s", order.order_number, amount_usdt, amount_rub, bank)
    return order


async def create_business_outkup_order(
    db: AsyncSession,
    user: User,
    amount_rub: Decimal,
    bank_in: str,
    destination: str = "balance",
    destination_addr: Optional[str] = None,
) -> Order:
    """Откуп бизнес-счёта — большой объём с разделением на части."""
    rate = await settings_kv.get_rate_buy(db)
    fee = await settings_kv.get_fee_v1_pct(db)
    amount_usdt = _calc_usdt_for_buy(amount_rub, rate, fee)

    order = Order(
        order_number=await _next_order_number(db),
        user_id=user.id,
        kind="business_outkup",
        amount_rub=amount_rub,
        amount_rub_remaining=amount_rub,
        rate_rub_per_usdt=rate,
        amount_usdt=amount_usdt,
        pct_fee=fee,
        destination=destination,
        destination_addr=destination_addr,
        bank_in=bank_in,
        status="pending",
        source="miniapp",
    )
    db.add(order)
    await db.flush()

    asyncio.create_task(jarvis_sync.send_event("order.created", {
        "order_id": order.id,
        "order_number": order.order_number,
        "user": {"tg_id": user.tg_id, "username": user.username, "is_partner": user.is_partner},
        "kind": "business_outkup",
        "amount_rub": float(amount_rub),
        "amount_usdt": float(amount_usdt),
        "rate": float(rate),
        "bank_in": bank_in,
    }))

    logger.info("[order] business_outkup %s: %s₽ → %sUSDT", order.order_number, amount_rub, amount_usdt)
    return order


async def cancel_order(db: AsyncSession, order: Order, reason: str = "user") -> None:
    if order.status in ("done", "cancelled"):
        return
    order.status = "cancelled"
    order.cancelled_reason = reason

    # Возвращаем USDT если был hold
    if order.kind == "sell_usdt" and (order.extra or {}).get("source_usdt") == "balance":
        from core.models import OperationLog
        res = await db.execute(select(User).where(User.id == order.user_id))
        user = res.scalar_one()
        balance_before = user.balance_usdt
        user.balance_usdt += order.amount_usdt
        db.add(OperationLog(
            user_id=user.id, type="refund",
            amount_usdt=order.amount_usdt,
            balance_before=balance_before, balance_after=user.balance_usdt,
            ref_table="orders", ref_id=order.id,
            note=f"refund cancelled order {order.order_number}",
        ))

    await db.flush()
    asyncio.create_task(jarvis_sync.send_event("order.cancelled", {
        "order_id": order.id, "order_number": order.order_number, "reason": reason,
    }))

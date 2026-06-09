"""V1 Orders — заявки клиент ↔ PRIDE."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import Order, OrderPayment, User
from core.services import order_service

router = APIRouter()


def _order_to_dict(o: Order, payments: list[OrderPayment] | None = None) -> dict:
    return {
        "id": o.id,
        "order_number": o.order_number,
        "kind": o.kind,
        "amount_rub": float(o.amount_rub),
        "amount_rub_remaining": float(o.amount_rub_remaining),
        "amount_usdt": float(o.amount_usdt),
        "rate": float(o.rate_rub_per_usdt),
        "pct_fee": float(o.pct_fee),
        "destination": o.destination,
        "destination_addr": o.destination_addr,
        "bank_in": o.bank_in,
        "bank_out": o.bank_out,
        "payment_method": o.payment_method,
        "payout_target": o.payout_target,
        "status": o.status,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
        "completed_at": o.completed_at.isoformat() if o.completed_at else None,
        "payments": [
            {
                "id": p.id,
                "payment_number": p.payment_number,
                "bank": p.bank,
                "phone_or_card": p.phone_or_card,
                "receiver_name": p.receiver_name,
                "amount_rub": float(p.amount_rub),
                "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                "receipt_url": p.receipt_url,
                "status": p.status,
                "created_at": p.created_at.isoformat(),
            }
            for p in (payments or [])
        ],
    }


@router.get("")
async def list_orders(
    status: str | None = None,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список моих заявок."""
    q = select(Order).where(Order.user_id == user.id)
    if status:
        if status == "active":
            q = q.where(Order.status.in_(["pending", "accepted", "partial", "awaiting_receipts"]))
        elif status == "done":
            q = q.where(Order.status == "done")
        elif status == "cancelled":
            q = q.where(Order.status == "cancelled")
    q = q.order_by(desc(Order.created_at)).limit(max(1, min(limit, 200)))
    res = await db.execute(q)
    items = res.scalars().all()
    return {"items": [_order_to_dict(o) for o in items], "count": len(items)}


@router.get("/{order_id}")
async def get_order(
    order_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Order).where(Order.id == order_id, Order.user_id == user.id)
    )
    order = res.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "order not found")
    pres = await db.execute(
        select(OrderPayment).where(OrderPayment.order_id == order.id).order_by(OrderPayment.id)
    )
    payments = pres.scalars().all()
    return {"order": _order_to_dict(order, payments)}


@router.post("/business_outkup")
async def create_business_outkup(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        amount_rub = Decimal(str(payload.get("amount_rub", "0")))
    except Exception:
        raise HTTPException(400, "invalid amount_rub")
    if amount_rub < Decimal("10000"):
        raise HTTPException(400, "business_outkup минимум 10 000 ₽")
    bank_in = (payload.get("bank_in") or "").strip()
    if not bank_in:
        raise HTTPException(400, "bank_in required")
    destination = (payload.get("destination") or "balance").strip()
    destination_addr = payload.get("destination_addr")
    if destination == "trc20" and not destination_addr:
        destination_addr = user.trc20_address

    order = await order_service.create_business_outkup_order(
        db, user, amount_rub, bank_in, destination, destination_addr,
    )
    return {
        "ok": True,
        "order_id": order.id,
        "order_number": order.order_number,
        "amount_usdt": float(order.amount_usdt),
    }


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: int,
    payload: dict | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Order).where(Order.id == order_id, Order.user_id == user.id)
    )
    order = res.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "order not found")
    reason = (payload or {}).get("reason", "user_cancelled")
    await order_service.cancel_order(db, order, reason)
    return {"ok": True}


@router.post("/{order_id}/upload_receipt")
async def upload_receipt(
    order_id: int,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Загрузить чек по конкретному payment.

    Простой вариант — передаём URL уже загруженного файла (Telegram file_id или внешний URL).
    Полноценный multipart upload — отдельный endpoint в Phase A3.
    """
    pay_id = payload.get("payment_id")
    receipt_url = payload.get("receipt_url")
    if not (pay_id and receipt_url):
        raise HTTPException(400, "payment_id and receipt_url required")
    res = await db.execute(
        select(OrderPayment).join(Order).where(
            OrderPayment.id == pay_id,
            Order.id == order_id,
            Order.user_id == user.id,
        )
    )
    pay = res.scalar_one_or_none()
    if not pay:
        raise HTTPException(404, "payment not found")
    pay.receipt_url = receipt_url
    pay.status = "receipt_uploaded"
    from datetime import datetime, timezone
    pay.receipt_uploaded_at = datetime.now(timezone.utc)
    await db.flush()

    # webhook в JARVIS
    import asyncio
    from core.services import jarvis_sync
    asyncio.create_task(jarvis_sync.send_event("order.receipt_uploaded", {
        "order_id": order_id, "payment_id": pay.id, "receipt_url": receipt_url,
    }))
    return {"ok": True}

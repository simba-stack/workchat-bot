"""P2P — реквизиты пользователя (payment methods).

Продавец один раз сохраняет свои реквизиты для каждого банка/СБП,
эти данные подставляются автоматически при создании Deal со стороны seller.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User, UserPaymentMethod, PAYMENT_TYPES

router = APIRouter()


class PaymentMethodIn(BaseModel):
    type: str = Field(..., description="sbp|tinkoff|sber|alpha|ozon|raif|vtb|gazprom|cash")
    bank_name: str = Field(..., min_length=1, max_length=64)
    card_or_phone: str = Field(..., min_length=4, max_length=64)
    receiver_name: str = Field(..., min_length=2, max_length=128)
    is_active: bool = True
    extra: Optional[str] = Field(None, max_length=128)


@router.get("")
async def list_my_methods(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(UserPaymentMethod)
        .where(UserPaymentMethod.user_id == user.id)
        .order_by(UserPaymentMethod.is_active.desc(), UserPaymentMethod.id.desc())
    )
    items = [m.to_dict() for m in res.scalars().all()]
    return {"items": items}


@router.post("")
async def create_method(
    payload: PaymentMethodIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    t = (payload.type or "").lower().strip()
    if t not in PAYMENT_TYPES:
        raise HTTPException(400, f"Unknown payment type. Allowed: {', '.join(sorted(PAYMENT_TYPES))}")
    m = UserPaymentMethod(
        user_id=user.id,
        type=t,
        bank_name=payload.bank_name.strip(),
        card_or_phone=payload.card_or_phone.strip(),
        receiver_name=payload.receiver_name.strip(),
        is_active=bool(payload.is_active),
        extra=(payload.extra or None),
    )
    db.add(m)
    await db.flush()
    await db.commit()
    return {"ok": True, "item": m.to_dict()}


class PaymentMethodPatch(BaseModel):
    bank_name: Optional[str] = Field(None, min_length=1, max_length=64)
    card_or_phone: Optional[str] = Field(None, min_length=4, max_length=64)
    receiver_name: Optional[str] = Field(None, min_length=2, max_length=128)
    is_active: Optional[bool] = None
    extra: Optional[str] = Field(None, max_length=128)


@router.patch("/{method_id}")
async def update_method(
    method_id: int,
    payload: PaymentMethodPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = await db.get(UserPaymentMethod, method_id)
    if not m or m.user_id != user.id:
        raise HTTPException(404, "Метод не найден")
    if payload.bank_name is not None:
        m.bank_name = payload.bank_name.strip()
    if payload.card_or_phone is not None:
        m.card_or_phone = payload.card_or_phone.strip()
    if payload.receiver_name is not None:
        m.receiver_name = payload.receiver_name.strip()
    if payload.is_active is not None:
        m.is_active = bool(payload.is_active)
    if payload.extra is not None:
        m.extra = payload.extra or None
    await db.commit()
    return {"ok": True, "item": m.to_dict()}


@router.delete("/{method_id}")
async def delete_method(
    method_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = await db.get(UserPaymentMethod, method_id)
    if not m or m.user_id != user.id:
        raise HTTPException(404, "Метод не найден")
    await db.delete(m)
    await db.commit()
    return {"ok": True}


@router.get("/seller/{seller_id}")
async def list_seller_active_methods(
    seller_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Активные реквизиты конкретного продавца — для подстановки при создании сделки.

    Возвращаем только методы которые seller сделал is_active=True.
    Sensitive поля (card_or_phone, receiver_name) показываются только участникам
    активной сделки с этим продавцом — это контролируется в /deals/{id}/info,
    тут отдаём список доступных типов для выбора способа оплаты.
    """
    res = await db.execute(
        select(UserPaymentMethod)
        .where(UserPaymentMethod.user_id == seller_id)
        .where(UserPaymentMethod.is_active == True)  # noqa: E712
        .order_by(UserPaymentMethod.id.desc())
    )
    items = []
    for m in res.scalars().all():
        items.append({
            "id": m.id,
            "type": m.type,
            "bank_name": m.bank_name,
            # реквизиты НЕ возвращаем тут — только в активной сделке
        })
    return {"items": items}

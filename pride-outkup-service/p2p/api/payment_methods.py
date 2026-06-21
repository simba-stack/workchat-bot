"""P2P Payment Methods CRUD (ТЗ Том 17).

Создание/удаление/обновление способов оплаты юзера.
Карты хранятся ТОЛЬКО маскированно. Если приходит полный номер — обрезаем.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, desc, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import audit
from p2p.enums import PaymentMethodType, PaymentMethodStatus
from p2p.models import P2PPaymentMethod

logger = logging.getLogger("p2p.api.pm")
router = APIRouter(prefix="/api/v2/p2p/payment-methods", tags=["p2p-payment-methods"])


def _mask_card(num: str | None) -> str | None:
    """4111 2222 3333 4444 → 4111 ** **** 4444"""
    if not num:
        return None
    digits = "".join(c for c in num if c.isdigit())
    if len(digits) < 8:
        return num[:4] + "***"
    return f"{digits[:4]} ** **** {digits[-4:]}"


def _pm_to_dict(pm: P2PPaymentMethod) -> dict:
    return {
        "id": pm.id,
        "user_id": pm.user_id,
        "type": pm.type,
        "bank_name": pm.bank_name,
        "account_holder": pm.account_holder,
        "card_number_masked": pm.card_number_masked,
        "phone": pm.phone,
        "iban": pm.iban,
        "country": pm.country,
        "priority": pm.priority,
        "status": pm.status,
        "created_at": pm.created_at.isoformat() if pm.created_at else None,
        "version": pm.version,
    }


# ============= LIST =============
@router.get("")
async def list_payment_methods(
    include_inactive: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(P2PPaymentMethod).where(
        P2PPaymentMethod.user_id == user.id,
        P2PPaymentMethod.deleted_at.is_(None),
    )
    if not include_inactive:
        q = q.where(P2PPaymentMethod.status == PaymentMethodStatus.ACTIVE.value)
    q = q.order_by(desc(P2PPaymentMethod.priority), desc(P2PPaymentMethod.created_at))
    r = await db.execute(q)
    return {"items": [_pm_to_dict(p) for p in r.scalars().all()]}


# ============= GET ONE =============
@router.get("/{pm_id}")
async def get_payment_method(
    pm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(P2PPaymentMethod).where(P2PPaymentMethod.id == pm_id))
    pm = r.scalar_one_or_none()
    if not pm or pm.deleted_at is not None:
        raise HTTPException(404, "payment method not found")
    if pm.user_id != user.id:
        raise HTTPException(403, "not your payment method")
    return _pm_to_dict(pm)


# ============= CREATE =============
@router.post("")
async def create_payment_method(
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pm_type = (payload.get("type") or "").upper()
    valid_types = {t.value for t in PaymentMethodType}
    if pm_type not in valid_types:
        raise HTTPException(422, f"type must be one of {sorted(valid_types)}")

    bank_name = (payload.get("bank_name") or "").strip()[:64]
    account_holder = (payload.get("account_holder") or "").strip()[:128]
    if not bank_name or not account_holder:
        raise HTTPException(422, "bank_name and account_holder required")

    card_full = payload.get("card_number")
    masked = _mask_card(card_full) if card_full else None
    # Не храним полный номер
    card_full_safe = masked  # alias — храним только masked

    phone = (payload.get("phone") or None)
    if phone:
        phone = str(phone)[:32]
    iban = (payload.get("iban") or None)
    if iban:
        iban = str(iban)[:64]
    country = (payload.get("country") or None)
    if country:
        country = str(country).upper()[:8]
    priority = int(payload.get("priority", 0))

    # Лимит: 20 PM на юзера
    cnt_r = await db.execute(
        select(P2PPaymentMethod).where(
            P2PPaymentMethod.user_id == user.id,
            P2PPaymentMethod.deleted_at.is_(None),
        )
    )
    if len(cnt_r.scalars().all()) >= 20:
        raise HTTPException(409, "max 20 payment methods per user")

    pm = P2PPaymentMethod(
        user_id=user.id, type=pm_type,
        bank_name=bank_name, account_holder=account_holder,
        card_number_masked=masked, card_number_full=card_full_safe,
        phone=phone, iban=iban, country=country,
        priority=priority, status=PaymentMethodStatus.ACTIVE.value,
        version=1,
    )
    db.add(pm)
    await db.flush()

    await audit.log(
        db, action="payment_method.created", entity_type="payment_method", entity_id=pm.id,
        actor_id=user.id,
        new_state={"type": pm_type, "bank": bank_name, "masked": masked},
        source="miniapp",
    )
    await db.commit()
    return _pm_to_dict(pm)


# ============= UPDATE =============
@router.patch("/{pm_id}")
async def update_payment_method(
    pm_id: str,
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(P2PPaymentMethod).where(P2PPaymentMethod.id == pm_id))
    pm = r.scalar_one_or_none()
    if not pm or pm.deleted_at is not None:
        raise HTTPException(404, "payment method not found")
    if pm.user_id != user.id:
        raise HTTPException(403, "not your payment method")

    changes = {}
    if "bank_name" in payload and payload["bank_name"]:
        pm.bank_name = str(payload["bank_name"])[:64]
        changes["bank_name"] = pm.bank_name
    if "account_holder" in payload and payload["account_holder"]:
        pm.account_holder = str(payload["account_holder"])[:128]
        changes["account_holder"] = pm.account_holder
    if "card_number" in payload:
        m = _mask_card(payload["card_number"])
        pm.card_number_masked = m
        pm.card_number_full = m
        changes["card_number_masked"] = m
    if "phone" in payload:
        pm.phone = str(payload["phone"])[:32] if payload["phone"] else None
        changes["phone"] = pm.phone
    if "iban" in payload:
        pm.iban = str(payload["iban"])[:64] if payload["iban"] else None
    if "country" in payload:
        pm.country = str(payload["country"]).upper()[:8] if payload["country"] else None
    if "priority" in payload:
        pm.priority = int(payload["priority"])
        changes["priority"] = pm.priority
    if "status" in payload:
        val = str(payload["status"]).upper()
        if val in (PaymentMethodStatus.ACTIVE.value, PaymentMethodStatus.DISABLED.value):
            pm.status = val
            changes["status"] = val

    pm.version += 1
    await db.flush()

    await audit.log(
        db, action="payment_method.updated", entity_type="payment_method", entity_id=pm.id,
        actor_id=user.id, new_state=changes,
        source="miniapp",
    )
    await db.commit()
    return _pm_to_dict(pm)


# ============= DELETE (soft) =============
@router.delete("/{pm_id}")
async def delete_payment_method(
    pm_id: str,
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(P2PPaymentMethod).where(P2PPaymentMethod.id == pm_id))
    pm = r.scalar_one_or_none()
    if not pm or pm.deleted_at is not None:
        raise HTTPException(404, "payment method not found")
    if pm.user_id != user.id:
        raise HTTPException(403, "not your payment method")

    pm.deleted_at = datetime.now(timezone.utc)
    pm.status = PaymentMethodStatus.DISABLED.value
    pm.version += 1
    await db.flush()

    await audit.log(
        db, action="payment_method.deleted", entity_type="payment_method", entity_id=pm.id,
        actor_id=user.id,
        source="miniapp",
    )
    await db.commit()
    return {"ok": True, "id": pm.id}

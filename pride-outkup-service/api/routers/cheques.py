"""Cheques — виртуальные чеки в Crypto-Bot стиле.

Flow:
1. POST /create  — юзер создаёт чек на N монет → списываем с баланса в "escrow" → выдаём code
2. Шлёт ссылку https://t.me/PrideP2P_bot?start=cheque_<code>
3. Получатель открывает Mini-App → автоматически redeem → монеты зачисляются ему
4. Или POST /{code}/cancel — создатель отменяет → возврат монет на баланс

Чек = одноразовый. После redeem статус = redeemed.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import Cheque, User
from core.services import balance_service

router = APIRouter()


def _gen_code() -> str:
    """Случайный URL-safe код, 16 символов."""
    return secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:16]


@router.post("/create")
async def create_cheque(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать чек. payload: {coin, amount, comment?}"""
    coin = (payload.get("coin") or "USDT").upper()
    try:
        amount = Decimal(str(payload.get("amount") or "0"))
    except Exception:
        raise HTTPException(400, "invalid amount")
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")

    # Списываем с баланса юзера
    try:
        await balance_service.debit(
            db, user.id, coin, amount,
            op_type="cheque_create",
            note=f"Cheque created",
        )
    except Exception as e:
        raise HTTPException(400, f"debit failed: {e}")

    code = _gen_code()
    # Проверка уникальности
    while (await db.execute(select(Cheque).where(Cheque.code == code))).scalar_one_or_none():
        code = _gen_code()

    cq = Cheque(
        creator_user_id=user.id,
        coin_code=coin,
        amount=amount,
        code=code,
        comment=(payload.get("comment") or "")[:500],
        status="active",
    )
    db.add(cq)
    await db.commit()
    await db.refresh(cq)
    return {
        "ok": True, "id": cq.id, "code": code,
        "coin": coin, "amount": float(amount),
        "link": f"https://t.me/PrideP2P_bot?start=cheque_{code}",
    }


@router.get("/my")
async def my_cheques(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список моих чеков (созданных + полученных)."""
    rows = (await db.execute(
        select(Cheque)
        .where((Cheque.creator_user_id == user.id) | (Cheque.redeemed_by_user_id == user.id))
        .order_by(desc(Cheque.created_at))
        .limit(200)
    )).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": c.id, "code": c.code,
                "coin": c.coin_code, "amount": float(c.amount),
                "comment": c.comment, "status": c.status,
                "is_mine": c.creator_user_id == user.id,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "redeemed_at": c.redeemed_at.isoformat() if c.redeemed_at else None,
            }
            for c in rows
        ],
    }


@router.get("/{code}")
async def get_cheque_info(
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """Публичная инфо о чеке (без auth, для preview)."""
    cq = (await db.execute(select(Cheque).where(Cheque.code == code))).scalar_one_or_none()
    if not cq:
        raise HTTPException(404, "cheque not found")
    creator = await db.get(User, cq.creator_user_id)
    return {
        "ok": True,
        "code": cq.code, "coin": cq.coin_code, "amount": float(cq.amount),
        "comment": cq.comment, "status": cq.status,
        "creator_username": creator.username if creator else None,
    }


@router.post("/{code}/redeem")
async def redeem_cheque(
    code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Принять чек: зачисляем на баланс получателя."""
    cq = (await db.execute(select(Cheque).where(Cheque.code == code))).scalar_one_or_none()
    if not cq:
        raise HTTPException(404, "cheque not found")
    if cq.status != "active":
        raise HTTPException(400, f"cheque already {cq.status}")
    if cq.creator_user_id == user.id:
        raise HTTPException(400, "can't redeem your own cheque")

    await balance_service.credit(
        db, user.id, cq.coin_code, cq.amount,
        op_type="cheque_redeem",
        note=f"Cheque #{cq.id} redeemed",
        ref_table="cheques", ref_id=cq.id,
    )
    cq.status = "redeemed"
    cq.redeemed_by_user_id = user.id
    cq.redeemed_at = datetime.now(timezone.utc)
    await db.commit()

    # Уведомить создателя
    try:
        from bot.main import notify_user
        creator = await db.get(User, cq.creator_user_id)
        if creator:
            await notify_user(
                creator.tg_id,
                f"📜 Чек на <b>{float(cq.amount)} {cq.coin_code}</b> получен @{user.username or user.tg_id}",
            )
    except Exception:
        pass

    return {
        "ok": True, "id": cq.id, "code": code,
        "coin": cq.coin_code, "amount": float(cq.amount),
    }


@router.post("/{code}/cancel")
async def cancel_cheque(
    code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Отменить активный чек (только создатель). Возврат на баланс."""
    cq = (await db.execute(select(Cheque).where(Cheque.code == code))).scalar_one_or_none()
    if not cq:
        raise HTTPException(404, "cheque not found")
    if cq.creator_user_id != user.id:
        raise HTTPException(403, "not your cheque")
    if cq.status != "active":
        raise HTTPException(400, f"cheque already {cq.status}")

    # Вернуть на баланс
    await balance_service.credit(
        db, user.id, cq.coin_code, cq.amount,
        op_type="cheque_refund",
        note=f"Cheque #{cq.id} cancelled",
        ref_table="cheques", ref_id=cq.id,
    )
    cq.status = "cancelled"
    cq.cancelled_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "status": "cancelled"}

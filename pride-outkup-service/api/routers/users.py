"""User-эндпоинты — me, balance, operations, withdraw."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import OperationLog, User

router = APIRouter()


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "tg_id": user.tg_id,
        "username": user.username,
        "full_name": user.full_name,
        "kyc_level": user.kyc_level,
        "kyc_status": user.kyc_status,
        "is_partner": user.is_partner,
        "trust_score": user.trust_score,
        "balance_usdt": float(user.balance_usdt),
        "trc20_address": user.trc20_address,
        "anti_phishing_code": user.anti_phishing_code,
        "language": user.language,
        "notifications_enabled": user.notifications_enabled,
        "stats": {
            "total_deals": user.total_deals,
            "completed_deals": user.completed_deals,
            "cancelled_deals": user.cancelled_deals,
            "completion_rate_pct": user.completion_rate_pct,
            "avg_release_time_sec": user.avg_release_time_sec,
            "total_volume_usdt": float(user.total_volume_usdt),
        },
    }


@router.patch("/me")
async def update_me(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    allowed = {"trc20_address", "anti_phishing_code", "language", "notifications_enabled"}
    for k, v in payload.items():
        if k in allowed and v is not None:
            setattr(user, k, v)
    await db.flush()
    return {"ok": True}


@router.get("/me/balance")
async def get_balance(user: User = Depends(get_current_user)):
    # TODO: вычислить total_earned/paid/pending из operations_log
    return {
        "balance_usdt": float(user.balance_usdt),
        "total_earned": 0.0,
        "total_paid": 0.0,
        "pending_usdt": 0.0,
    }


@router.get("/me/operations")
async def get_operations(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    limit = max(1, min(limit, 200))
    res = await db.execute(
        select(OperationLog)
        .where(OperationLog.user_id == user.id)
        .order_by(desc(OperationLog.created_at))
        .limit(limit)
    )
    items = res.scalars().all()
    return {
        "items": [
            {
                "id": op.id,
                "type": op.type,
                "amount_usdt": float(op.amount_usdt),
                "balance_after": float(op.balance_after) if op.balance_after else None,
                "ref_table": op.ref_table,
                "ref_id": op.ref_id,
                "txid": op.txid,
                "note": op.note,
                "created_at": op.created_at.isoformat(),
            }
            for op in items
        ],
    }


@router.post("/me/withdraw")
async def withdraw(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запрос на вывод USDT с баланса на TRC20."""
    if user.kyc_status != "verified":
        raise HTTPException(403, "KYC required")
    amount = Decimal(str(payload.get("amount_usdt", "0")))
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    if amount > user.balance_usdt:
        raise HTTPException(400, "insufficient balance")
    address = payload.get("trc20_address") or user.trc20_address
    if not address or not address.startswith("T") or len(address) < 30:
        raise HTTPException(400, "valid TRC20 address required")

    # TODO Phase A5: enqueue в tron_payouts → автоматическая отправка
    # Пока — заглушка: только запись в лог
    user.balance_usdt -= amount
    op = OperationLog(
        user_id=user.id,
        type="withdraw",
        amount_usdt=-amount,
        balance_after=user.balance_usdt,
        ref_table="tron_outbound_log",
        note=f"withdraw to {address}",
    )
    db.add(op)
    await db.flush()

    return {"ok": True, "amount_usdt": float(amount), "to_address": address, "status": "pending"}

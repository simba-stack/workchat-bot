"""User-эндпоинты — me, balance, operations, KYC, deposit, withdraw, orders."""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user, require_verified
from core.db import get_db
from core.models import OperationLog, Order, User
from core.services import jarvis_sync

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


@router.get("/me/orders")
async def my_orders(
    status_: str | None = None,
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """История заявок текущего пользователя."""
    limit = max(1, min(limit, 100))
    q = select(Order).where(Order.user_id == user.id).order_by(desc(Order.created_at)).limit(limit)
    if status_:
        q = select(Order).where(Order.user_id == user.id, Order.status == status_).order_by(desc(Order.created_at)).limit(limit)
    res = await db.execute(q)
    items = res.scalars().all()
    return {
        "items": [
            {
                "id": o.id,
                "order_number": o.order_number,
                "kind": o.kind,
                "amount_rub": float(o.amount_rub),
                "amount_rub_remaining": float(o.amount_rub_remaining),
                "amount_usdt": float(o.amount_usdt),
                "rate_rub_per_usdt": float(o.rate_rub_per_usdt),
                "pct_fee": float(o.pct_fee),
                "destination": o.destination,
                "destination_addr": o.destination_addr,
                "status": o.status,
                "bank_in": o.bank_in,
                "bank_out": o.bank_out,
                "payment_method": o.payment_method,
                "created_at": o.created_at.isoformat(),
            }
            for o in items
        ],
    }


# ─── KYC ──────────────────────────────────────────────────────────────
@router.post("/me/kyc")
async def submit_kyc(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit KYC level 1 (паспортные данные + телефон).

    payload: {first_name, last_name, birthdate (YYYY-MM-DD), passport_series,
              passport_number, phone, country}
    """
    if user.kyc_status == "verified":
        raise HTTPException(400, "уже верифицирован")
    if user.kyc_status == "banned":
        raise HTTPException(403, "забанен")

    required = ("first_name", "last_name", "birthdate", "phone")
    for k in required:
        if not (payload.get(k) or "").strip():
            raise HTTPException(400, f"поле '{k}' обязательно")

    user.kyc_data = {
        **(user.kyc_data or {}),
        "first_name": payload.get("first_name", "").strip(),
        "last_name": payload.get("last_name", "").strip(),
        "birthdate": payload.get("birthdate", "").strip(),
        "passport_series": (payload.get("passport_series") or "").strip(),
        "passport_number": (payload.get("passport_number") or "").strip(),
        "country": (payload.get("country") or "RU").strip(),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    user.phone = (payload.get("phone") or "").strip()
    user.full_name = f"{payload['first_name']} {payload['last_name']}".strip()
    user.kyc_status = "pending_review"
    await db.flush()

    # Уведомляем JARVIS — админ может одобрить через webhook
    try:
        await jarvis_sync.send_event("kyc_requested", {
            "user_id": user.id,
            "tg_id": user.tg_id,
            "username": user.username,
            "kyc_level": 1,
            "data": user.kyc_data,
        })
    except Exception:
        pass

    return {"ok": True, "kyc_status": "pending_review"}


@router.post("/me/deposit_address")
async def get_deposit_address(
    user: User = Depends(require_verified),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает (или генерирует) TRC20-адрес для депозита USDT.

    В Phase A5 — реальная генерация через tron service. Сейчас — placeholder:
    использует общий hot-wallet адрес из config с memo.
    """
    from core.config import settings as cfg
    if not cfg.tron_hot_wallet_address:
        raise HTTPException(503, "TRON wallet not configured yet")

    # Один общий адрес для депозитов + tracking по сумме (Phase A5: per-user addr)
    return {
        "address": cfg.tron_hot_wallet_address,
        "network": "TRC20",
        "note": f"При переводе укажите memo: user_{user.id}",
        "user_memo": f"user_{user.id}",
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

    # Списываем баланс
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

    # Если TRON настроен — реальная отправка, иначе pending для ручной обработки
    from core.services import tron_service
    tx_id = None
    status_ = "pending"
    if tron_service.is_configured():
        # auto-send только маленькие суммы (≤ 100 USDT). Большие — через JARVIS 2FA approval.
        if amount <= 100:
            res = await tron_service.send_usdt(address, amount)
            if res.get("ok"):
                tx_id = res.get("tx_id")
                status_ = "sent"
                op.txid = tx_id
                await db.flush()
            else:
                # откат
                user.balance_usdt += amount
                op.note += f" — auto-send failed: {res.get('error')}"
                status_ = "failed"
                await db.flush()
                return {"ok": False, "error": res.get("error"), "status": status_}
        else:
            status_ = "pending_2fa"

    # Уведомляем JARVIS чтобы попало в Бухгалтерия → Выплаты
    try:
        from core.services import jarvis_sync as _js
        await _js.send_event("withdraw_requested", {
            "user_id": user.id, "tg_id": user.tg_id,
            "amount_usdt": float(amount), "to_address": address,
            "tx_id": tx_id, "status": status_,
        })
    except Exception:
        pass

    return {
        "ok": True,
        "amount_usdt": float(amount),
        "to_address": address,
        "status": status_,
        "tx_id": tx_id,
    }

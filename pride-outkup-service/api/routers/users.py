"""User-эндпоинты — me, balance, operations, KYC, deposit, withdraw, orders."""
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user, require_verified
from core.db import get_db
from core.models import DepositRequest, OperationLog, Order, User
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


@router.get("/me/deposit_address")
async def my_deposit_address(
    coin: str = "USDT",
    network: str = "TRC20",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает (или создаёт) персональный депозитный адрес юзера.

    HD-wallet: адрес деривируется из master_derivation_key + user_id детерминистически.
    Один юзер — один адрес для (coin, network).
    """
    if user.kyc_status == "banned":
        raise HTTPException(403, "Аккаунт заблокирован")
    from core.services.wallet_derive import get_or_create_user_address
    try:
        address, idx = await get_or_create_user_address(db, user.id, coin, network)
    except NotImplementedError as e:
        raise HTTPException(400, f"Сеть пока не поддерживается: {e}")
    except Exception as e:
        raise HTTPException(500, f"Не удалось создать адрес: {e}")
    return {
        "coin": coin.upper(),
        "network": network.upper(),
        "address": address,
        "min_deposit": 1,
        "warning": "Отправляй только эту монету и только в эту сеть. Любая сумма ≥ минимума.",
    }


@router.post("/me/deposits/request")
async def create_deposit_request(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Crypto-Bot-стиль депозит. Юзер вводит сумму, мы возвращаем точную сумму
    с микро-отклонением (для матчинга) + адрес + TTL 15 мин.

    Депозиты разрешены без KYC — это просто приём средств.
    KYC требуется для вывода/P2P/обмена.

    payload: {amount_usdt: float}
    """
    if user.kyc_status == "banned":
        raise HTTPException(403, "Аккаунт заблокирован")
    from core.config import settings as cfg
    if not cfg.tron_hot_wallet_address:
        raise HTTPException(503, "TRON-кошелёк ещё не настроен")
    try:
        base = Decimal(str(payload.get("amount_usdt") or 0))
    except Exception:
        raise HTTPException(400, "bad amount_usdt")
    if base < Decimal("1"):
        raise HTTPException(400, "минимум 1 USDT")
    if base > Decimal("100000"):
        raise HTTPException(400, "максимум 100000 USDT за раз")

    # Подбираем уникальное exact_amount среди активных pending
    # (base + 0.0001*N где N=1..9999, до 4 знаков после запятой)
    res = await db.execute(
        select(DepositRequest.exact_amount).where(DepositRequest.status == "pending")
    )
    taken = {r[0] for r in res.all()}
    base_q = base.quantize(Decimal("0.0001"))
    exact = None
    for n in range(1, 10000):
        candidate = (base_q + Decimal(n) / Decimal(10000)).quantize(Decimal("0.0001"))
        if candidate not in taken:
            exact = candidate
            break
    if exact is None:
        raise HTTPException(503, "не удалось сгенерировать уникальную сумму, попробуйте позже")

    dr = DepositRequest(
        user_id=user.id,
        base_amount=base_q,
        exact_amount=exact,
        to_address=cfg.tron_hot_wallet_address,
        network="TRC20",
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    db.add(dr)
    await db.flush()
    return {
        "id": dr.id,
        "address": cfg.tron_hot_wallet_address,
        "network": "TRC20",
        "exact_amount": float(exact),
        "base_amount": float(base_q),
        "expires_at": dr.expires_at.isoformat(),
        "status": dr.status,
        "warning": "Переводите ровно эту сумму. Иначе зачисление вручную.",
    }


@router.get("/me/deposits/{request_id}")
async def get_deposit_status(
    request_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dr = await db.get(DepositRequest, request_id)
    if not dr or dr.user_id != user.id:
        raise HTTPException(404, "deposit request not found")
    return {
        "id": dr.id,
        "status": dr.status,
        "exact_amount": float(dr.exact_amount),
        "base_amount": float(dr.base_amount),
        "address": dr.to_address,
        "matched_tx_id": dr.matched_tx_id,
        "matched_at": dr.matched_at.isoformat() if dr.matched_at else None,
        "matched_amount": float(dr.matched_amount) if dr.matched_amount else None,
        "expires_at": dr.expires_at.isoformat(),
    }


@router.get("/me/deposits")
async def list_my_deposits(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(DepositRequest)
        .where(DepositRequest.user_id == user.id)
        .order_by(desc(DepositRequest.created_at))
        .limit(max(1, min(limit, 100)))
    )
    return {
        "items": [
            {
                "id": dr.id, "status": dr.status,
                "exact_amount": float(dr.exact_amount),
                "base_amount": float(dr.base_amount),
                "matched_tx_id": dr.matched_tx_id,
                "created_at": dr.created_at.isoformat(),
                "expires_at": dr.expires_at.isoformat(),
            }
            for dr in res.scalars().all()
        ]
    }


@router.post("/me/withdraw")
async def withdraw(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запрос на вывод USDT с баланса на TRC20."""
    # KYC опциональный — не блокируем withdraw (только banned)
    if user.kyc_status == "banned":
        raise HTTPException(403, "Account banned")
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

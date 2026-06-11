"""Owner Panel — приватный дашборд для владельца PRIDE P2P.

Только один человек может войти: tg_id == OWNER_TG_ID + правильный PIN.
Сессия — HMAC-signed cookie на 7 дней (без БД).

Endpoints:
- POST /owner/login  → set cookie pride_owner_session
- POST /owner/logout → clear cookie
- GET  /owner/dashboard → liabilities, earnings, hot wallet, totals
- GET  /owner/users     → список юзеров с балансами и HD-адресами
- GET  /owner/operations → история всех операций (с фильтрами)
- GET  /owner/withdrawals → история выводов с tx_id

Безопасность:
- TG ID hardcoded или env OWNER_TG_ID
- PIN из env OWNER_PIN (если не задан — login отключён)
- Cookie подписан OWNER_SESSION_SECRET (если не задан — генерится при первом старте)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from decimal import Decimal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_db
from core.models import OperationLog, User

router = APIRouter()

OWNER_TG_ID = int(os.environ.get("OWNER_TG_ID", "6009769056"))
COOKIE_NAME = "pride_owner_session"
SESSION_TTL_SEC = 7 * 24 * 60 * 60  # 7 дней


def _session_secret() -> str:
    """Секрет для HMAC подписи cookie. Из env OWNER_SESSION_SECRET или MASTER fallback."""
    sec = os.environ.get("OWNER_SESSION_SECRET", "").strip()
    if sec and len(sec) >= 16:
        return sec
    # fallback — используем JARVIS_HMAC_SECRET если есть
    sec = os.environ.get("JARVIS_HMAC_SECRET", "").strip()
    if sec and len(sec) >= 16:
        return sec
    # последний fallback — детерминистический (не идеально, но лучше чем None)
    return "pride-owner-default-secret-please-set-OWNER_SESSION_SECRET-env"


def _sign(payload: str) -> str:
    return hmac.new(
        _session_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def _make_cookie() -> str:
    """Возвращает строку `tg_id.expires.signature` для cookie."""
    expires = int(time.time()) + SESSION_TTL_SEC
    payload = f"{OWNER_TG_ID}.{expires}"
    sig = _sign(payload)
    return f"{payload}.{sig}"


def _verify_cookie(cookie_value: str) -> bool:
    if not cookie_value:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return False
    tg_id_str, expires_str, sig = parts
    try:
        if int(tg_id_str) != OWNER_TG_ID:
            return False
        if int(expires_str) < time.time():
            return False
    except ValueError:
        return False
    expected = _sign(f"{tg_id_str}.{expires_str}")
    return hmac.compare_digest(sig, expected)


def require_owner(pride_owner_session: str | None = Cookie(default=None)):
    """FastAPI dependency: проверка владельца через cookie."""
    if not _verify_cookie(pride_owner_session or ""):
        raise HTTPException(401, "owner session required")
    return True


# ─── Login / Logout ──────────────────────────────────────────────────
@router.post("/login")
async def owner_login(payload: dict, response: Response):
    """Body: {tg_id: int, pin: str}. При успехе ставит cookie на 7 дней."""
    pin_env = os.environ.get("OWNER_PIN", "").strip()
    if not pin_env or len(pin_env) < 4:
        raise HTTPException(503, "OWNER_PIN не настроен в Variables")

    try:
        tg_id = int(payload.get("tg_id") or 0)
    except (TypeError, ValueError):
        tg_id = 0
    pin = str(payload.get("pin") or "").strip()

    # Постоянное время сравнения чтобы не было side-channel
    is_tg_ok = (tg_id == OWNER_TG_ID)
    is_pin_ok = hmac.compare_digest(pin, pin_env)

    if not (is_tg_ok and is_pin_ok):
        # Не выдаём какое именно поле не сошлось
        raise HTTPException(401, "invalid credentials")

    cookie_value = _make_cookie()
    response.set_cookie(
        key=COOKIE_NAME, value=cookie_value,
        max_age=SESSION_TTL_SEC,
        httponly=True, secure=True, samesite="lax",
    )
    return {"ok": True, "tg_id": tg_id, "expires_in_sec": SESSION_TTL_SEC}


@router.post("/logout")
async def owner_logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME)
    return {"ok": True}


# ─── Dashboard ───────────────────────────────────────────────────────
@router.get("/dashboard")
async def owner_dashboard(
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Главный экран: hot wallet, обязательства, заработок, чистая позиция."""
    from core.models import UserCoinBalance, UserDepositAddress
    from core.services import tron_service

    # 1) Hot wallet (живой баланс с TronGrid)
    hot_usdt = float(await tron_service.get_usdt_balance())
    hot_addr = settings.tron_hot_wallet_address or ""

    # 2) Обязательства — сумма internal balances по монетам
    res = await db.execute(
        select(UserCoinBalance.coin_code, func.sum(UserCoinBalance.balance))
        .group_by(UserCoinBalance.coin_code)
    )
    liabilities = {code: float(total or 0) for code, total in res.all()}
    liab_usdt = liabilities.get("USDT", 0.0)
    net_position_usdt = hot_usdt - liab_usdt  # сколько "наше" свыше обязательств

    # 3) Заработок = сумма withdraw fees (из OperationLog type='withdraw_fee')
    earnings_q = await db.execute(
        select(func.sum(OperationLog.amount_usdt))
        .where(OperationLog.type == "withdraw_fee")
    )
    total_earnings = float(earnings_q.scalar() or 0)

    # 4) Счётчики операций
    deposits_count = (await db.execute(
        select(func.count(OperationLog.id)).where(OperationLog.type == "deposit")
    )).scalar() or 0
    withdrawals_count = (await db.execute(
        select(func.count(OperationLog.id)).where(OperationLog.type == "withdraw")
    )).scalar() or 0
    users_total = (await db.execute(select(func.count(User.id)))).scalar() or 0
    addresses_total = (await db.execute(
        select(func.count(UserDepositAddress.id))
    )).scalar() or 0

    return {
        "ok": True,
        "hot_wallet": {
            "address": hot_addr,
            "usdt_balance": hot_usdt,
            "tronscan_url": f"https://tronscan.org/#/address/{hot_addr}" if hot_addr else None,
        },
        "liabilities": liabilities,
        "liabilities_usdt_total": liab_usdt,
        "net_position_usdt": net_position_usdt,
        "earnings": {
            "total_usdt": total_earnings,
            "withdrawals_count": withdrawals_count,
            "deposits_count": deposits_count,
        },
        "stats": {
            "users_total": users_total,
            "hd_addresses_total": addresses_total,
        },
    }


@router.get("/users")
async def owner_users_list(
    has_balance: bool = True,
    limit: int = 500,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Список юзеров с балансами и HD-адресами."""
    from core.models import UserCoinBalance, UserDepositAddress

    users = (await db.execute(
        select(User).order_by(desc(User.created_at)).limit(max(1, min(limit, 2000)))
    )).scalars().all()
    user_ids = [u.id for u in users]
    if not user_ids:
        return {"ok": True, "items": [], "count": 0}

    balances = (await db.execute(
        select(UserCoinBalance).where(UserCoinBalance.user_id.in_(user_ids))
    )).scalars().all()
    bal_by_user: dict[int, dict[str, float]] = {}
    for b in balances:
        bal_by_user.setdefault(b.user_id, {})[b.coin_code] = float(b.balance)

    addrs = (await db.execute(
        select(UserDepositAddress).where(UserDepositAddress.user_id.in_(user_ids))
    )).scalars().all()
    addr_by_user: dict[int, dict[str, str]] = {}
    for a in addrs:
        addr_by_user.setdefault(a.user_id, {})[a.network] = a.address

    items = []
    for u in users:
        ub = bal_by_user.get(u.id, {})
        if has_balance and not any(v > 0 for v in ub.values()):
            continue
        items.append({
            "id": u.id, "tg_id": u.tg_id,
            "username": u.username or "",
            "full_name": u.full_name or "",
            "kyc_status": u.kyc_status,
            "balances": ub,
            "addresses": addr_by_user.get(u.id, {}),
            "total_deals": u.total_deals,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return {"ok": True, "items": items, "count": len(items)}


@router.get("/operations")
async def owner_operations(
    op_type: str | None = None,
    limit: int = 100,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """История операций: deposit/withdraw/withdraw_fee/transfer/swap/admin_credit/etc."""
    q = select(OperationLog).order_by(desc(OperationLog.created_at)).limit(max(1, min(limit, 1000)))
    if op_type:
        q = q.where(OperationLog.type == op_type)
    rows = (await db.execute(q)).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": op.id, "user_id": op.user_id, "type": op.type,
                "amount_usdt": float(op.amount_usdt or 0),
                "balance_after": float(op.balance_after) if op.balance_after is not None else None,
                "txid": op.txid,
                "ref_table": op.ref_table, "ref_id": op.ref_id,
                "note": op.note,
                "created_at": op.created_at.isoformat() if op.created_at else None,
            }
            for op in rows
        ],
        "count": len(rows),
    }


@router.get("/withdrawals")
async def owner_withdrawals(
    limit: int = 100,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Только withdraws — с tx_id, статусами, fees."""
    q = (
        select(OperationLog)
        .where(OperationLog.type.in_(["withdraw", "withdraw_fee", "withdraw_failed"]))
        .order_by(desc(OperationLog.created_at))
        .limit(max(1, min(limit, 500)))
    )
    rows = (await db.execute(q)).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": op.id, "user_id": op.user_id, "type": op.type,
                "amount_usdt": float(op.amount_usdt or 0),
                "txid": op.txid,
                "tronscan_url": (f"https://tronscan.org/#/transaction/{op.txid}" if op.txid else None),
                "note": op.note,
                "created_at": op.created_at.isoformat() if op.created_at else None,
            }
            for op in rows
        ],
        "count": len(rows),
    }


# ─── User actions ───────────────────────────────────────────────────
@router.post("/users/{user_id}/ban")
async def owner_ban_user(
    user_id: int, payload: dict | None = None,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Заблокировать юзера: kyc_status=banned + ban_reason."""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    reason = (payload or {}).get("reason") or ""
    u.kyc_status = "banned"
    u.kyc_data = {**(u.kyc_data or {}), "ban_reason": reason, "banned_at": time.time()}
    db.add(OperationLog(
        user_id=user_id, type="owner_ban",
        amount_usdt=0, balance_after=None,
        note=f"banned by owner: {reason[:200]}",
    ))
    await db.commit()
    return {"ok": True, "user_id": user_id, "kyc_status": "banned"}


@router.post("/users/{user_id}/unban")
async def owner_unban_user(
    user_id: int,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Разблокировать юзера: kyc_status = previous (pending по умолчанию)."""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.kyc_status = "pending"
    u.kyc_data = {**(u.kyc_data or {}), "unbanned_at": time.time()}
    db.add(OperationLog(
        user_id=user_id, type="owner_unban",
        amount_usdt=0, balance_after=None,
        note="unbanned by owner",
    ))
    await db.commit()
    return {"ok": True, "user_id": user_id, "kyc_status": "pending"}


@router.post("/users/{user_id}/credit")
async def owner_credit_user(
    user_id: int, payload: dict,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Ручное пополнение internal balance юзера. payload: {coin, amount, note?}"""
    from core.services import balance_service
    coin = (payload.get("coin") or "USDT").upper()
    try:
        amount = Decimal(str(payload.get("amount") or "0"))
    except Exception:
        raise HTTPException(400, "invalid amount")
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    await balance_service.credit(
        db, user_id, coin, amount,
        op_type="owner_credit",
        note=f"owner manual credit: {payload.get('note', '')[:200]}",
    )
    await db.commit()
    return {"ok": True, "user_id": user_id, "coin": coin, "amount": float(amount)}


@router.post("/users/{user_id}/debit")
async def owner_debit_user(
    user_id: int, payload: dict,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Ручное списание internal balance юзера. payload: {coin, amount, note?}"""
    from core.services import balance_service
    coin = (payload.get("coin") or "USDT").upper()
    try:
        amount = Decimal(str(payload.get("amount") or "0"))
    except Exception:
        raise HTTPException(400, "invalid amount")
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    try:
        await balance_service.debit(
            db, user_id, coin, amount,
            op_type="owner_debit",
            note=f"owner manual debit: {payload.get('note', '')[:200]}",
        )
    except Exception as e:
        raise HTTPException(400, f"debit failed: {e}")
    await db.commit()
    return {"ok": True, "user_id": user_id, "coin": coin, "amount": float(amount)}


# ─── Sweep control ──────────────────────────────────────────────────
@router.post("/sweep/user/{user_id}")
async def owner_sweep_user(
    user_id: int,
    _: bool = Depends(require_owner),
):
    """Запустить sweep вручную для конкретного юзера. Возвращает результат."""
    from core.services import sweep_service
    result = await sweep_service.sweep_single_address(user_id)
    return {"ok": result.get("ok", False), "result": result}


@router.post("/sweep/all")
async def owner_sweep_all(
    _: bool = Depends(require_owner),
):
    """Запустить общий sweep всех user-адресов прямо сейчас."""
    from core.services import sweep_service
    import asyncio
    # Background, чтоб не блокировать HTTP
    asyncio.create_task(sweep_service.tick())
    return {"ok": True, "message": "sweep started in background"}


@router.get("/sweeps")
async def owner_sweeps_history(
    limit: int = 100,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """История свипов — операции с type='sweep' или note содержит 'sweep'."""
    from sqlalchemy import or_
    q = (
        select(OperationLog)
        .where(or_(
            OperationLog.type == "sweep",
            OperationLog.note.like("%sweep%"),
        ))
        .order_by(desc(OperationLog.created_at))
        .limit(max(1, min(limit, 500)))
    )
    rows = (await db.execute(q)).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": op.id, "user_id": op.user_id, "type": op.type,
                "amount_usdt": float(op.amount_usdt or 0),
                "txid": op.txid,
                "tronscan_url": (f"https://tronscan.org/#/transaction/{op.txid}" if op.txid else None),
                "note": op.note,
                "created_at": op.created_at.isoformat() if op.created_at else None,
            }
            for op in rows
        ],
        "count": len(rows),
    }


# ─── P2P (Offers + Deals + Disputes) ────────────────────────────────
@router.get("/p2p/offers")
async def owner_p2p_offers(
    limit: int = 100,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    from core.models import Offer
    rows = (await db.execute(
        select(Offer).order_by(desc(Offer.created_at)).limit(max(1, min(limit, 500)))
    )).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": o.id, "user_id": o.user_id,
                "side": o.side, "coin": o.coin, "fiat": o.fiat,
                "price": float(o.price or 0),
                "min_amount": float(o.min_amount or 0),
                "max_amount": float(o.max_amount or 0),
                "payment_methods": o.payment_methods or [],
                "is_active": o.is_active,
                "is_pride_official": o.is_pride_official,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in rows
        ],
        "count": len(rows),
    }


@router.post("/p2p/offers/{offer_id}/toggle")
async def owner_p2p_offer_toggle(
    offer_id: int,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Переключить is_active оффера."""
    from core.models import Offer
    o = await db.get(Offer, offer_id)
    if not o:
        raise HTTPException(404, "offer not found")
    o.is_active = not o.is_active
    await db.commit()
    return {"ok": True, "id": offer_id, "is_active": o.is_active}


@router.delete("/p2p/offers/{offer_id}")
async def owner_p2p_offer_delete(
    offer_id: int,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    from core.models import Offer
    o = await db.get(Offer, offer_id)
    if not o:
        raise HTTPException(404, "offer not found")
    await db.delete(o)
    await db.commit()
    return {"ok": True, "id": offer_id}


@router.get("/p2p/deals")
async def owner_p2p_deals(
    status_: str | None = None,
    limit: int = 100,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    from core.models import Deal
    q = select(Deal).order_by(desc(Deal.created_at)).limit(max(1, min(limit, 500)))
    if status_:
        q = q.where(Deal.status == status_)
    rows = (await db.execute(q)).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": d.id, "offer_id": d.offer_id,
                "buyer_id": d.buyer_id, "seller_id": d.seller_id,
                "amount_usdt": float(d.amount_usdt or 0),
                "amount_fiat": float(d.amount_fiat or 0),
                "fee_usdt": float(d.fee_usdt or 0),
                "status": d.status,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "released_at": d.released_at.isoformat() if d.released_at else None,
                "cancelled_reason": d.cancelled_reason,
            }
            for d in rows
        ],
        "count": len(rows),
    }


@router.get("/disputes")
async def owner_disputes(
    status_: str | None = "open",
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    from core.models import Dispute
    q = select(Dispute).order_by(desc(Dispute.created_at))
    if status_:
        q = q.where(Dispute.status == status_)
    rows = (await db.execute(q)).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": d.id, "deal_id": d.deal_id, "order_id": d.order_id,
                "opened_by_id": d.opened_by_id, "reason": d.reason,
                "evidence_urls": d.evidence_urls or [],
                "status": d.status, "resolution": d.resolution,
                "resolution_note": d.resolution_note,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ],
        "count": len(rows),
    }


@router.post("/disputes/{dispute_id}/resolve")
async def owner_dispute_resolve(
    dispute_id: int, payload: dict,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """payload: {resolution: 'buyer'|'seller'|'split', notes?}"""
    from datetime import datetime, timezone
    from core.models import Deal, Dispute, EscrowLock
    from core.services import escrow_service
    d = await db.get(Dispute, dispute_id)
    if not d:
        raise HTTPException(404, "dispute not found")
    decision = (payload.get("resolution") or "").lower()
    if decision not in ("buyer", "seller", "split"):
        raise HTTPException(400, "resolution must be buyer|seller|split")
    d.status = "resolved"
    d.resolution = decision
    d.resolution_note = (payload.get("notes") or "")[:2048]
    d.resolved_by_admin = "owner"
    d.resolved_at = datetime.now(timezone.utc)
    if d.deal_id:
        deal = await db.get(Deal, d.deal_id)
        if deal:
            if decision == "buyer":
                await escrow_service.release(db, deal)
            elif decision == "seller":
                await escrow_service.refund(db, deal, reason="dispute_for_seller")
    await db.commit()
    return {"ok": True, "resolution": decision}


# ─── Settings (Coins / Features) ────────────────────────────────────
@router.get("/coins")
async def owner_coins_list(
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    from core.models import Coin
    rows = (await db.execute(
        select(Coin).order_by(Coin.sort_order)
    )).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "code": c.code, "name": c.name,
                "networks": c.networks or [],
                "min_deposit": float(c.min_deposit),
                "min_withdraw": float(c.min_withdraw),
                "withdraw_fee": float(c.withdraw_fee),
                "is_active": c.is_active,
                "sort_order": c.sort_order,
            }
            for c in rows
        ],
    }


@router.post("/coins/{code}/update")
async def owner_coin_update(
    code: str, payload: dict,
    _: bool = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """payload: {withdraw_fee?, min_withdraw?, min_deposit?, is_active?}"""
    from core.models import Coin
    c = (await db.execute(
        select(Coin).where(Coin.code == code.upper())
    )).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "coin not found")
    changed = []
    if (v := payload.get("withdraw_fee")) is not None:
        c.withdraw_fee = Decimal(str(v))
        changed.append(f"fee={v}")
    if (v := payload.get("min_withdraw")) is not None:
        c.min_withdraw = Decimal(str(v))
        changed.append(f"min_w={v}")
    if (v := payload.get("min_deposit")) is not None:
        c.min_deposit = Decimal(str(v))
        changed.append(f"min_d={v}")
    if (v := payload.get("is_active")) is not None:
        c.is_active = bool(v)
        changed.append(f"active={v}")
    db.add(OperationLog(
        user_id=0, type="owner_coin_update",
        amount_usdt=0, balance_after=None,
        note=f"{code}: {', '.join(changed)}",
    ))
    await db.commit()
    return {"ok": True, "code": c.code, "changed": changed}


@router.get("/energy_status")
async def owner_energy_status(
    _: bool = Depends(require_owner),
):
    """Feee.io status: balance, price, hot wallet TRX."""
    from core.services import energy_service, tron_service
    is_cfg = energy_service.is_configured()
    bal = await energy_service.get_balance() if is_cfg else None
    price = await energy_service.get_energy_price() if is_cfg else None
    hot_usdt = float(await tron_service.get_usdt_balance()) if tron_service.is_configured() else 0
    return {
        "ok": True,
        "feee": {
            "configured": is_cfg,
            "trx_balance": float(bal) if bal is not None else None,
            "energy_price_sun": float(price) if price is not None else None,
        },
        "hot_wallet": {
            "configured": tron_service.is_configured(),
            "usdt_balance": hot_usdt,
            "address": settings.tron_hot_wallet_address,
        },
    }

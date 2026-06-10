"""Admin endpoints — модерация. Доступ только tg_id ∈ ADMIN_TG_IDS.

Используется как из JARVIS (через REST с Bearer JARVIS_API_TOKEN), так и
непосредственно админом-пользователем (через initData в Mini-App).
"""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_admin
from core.config import settings
from core.db import get_db
from core.models import Deal, Dispute, OperationLog, User
from core.services import escrow_service, jarvis_sync, settings_kv

router = APIRouter()


async def _admin_or_jarvis_token(
    x_jarvis_token: str | None = Header(None),
    user: User | None = None,
) -> bool:
    if x_jarvis_token and settings.jarvis_api_token and x_jarvis_token == settings.jarvis_api_token:
        return True
    if user is not None:
        return True
    raise HTTPException(403, "admin only")


@router.get("/users")
async def list_users(
    kyc_status: str | None = None,
    limit: int = 100,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(User).order_by(desc(User.created_at)).limit(max(1, min(limit, 500)))
    if kyc_status:
        q = q.where(User.kyc_status == kyc_status)
    res = await db.execute(q)
    return {
        "items": [
            {
                "id": u.id, "tg_id": u.tg_id, "username": u.username,
                "full_name": u.full_name, "kyc_status": u.kyc_status,
                "kyc_level": u.kyc_level, "is_partner": u.is_partner,
                "trust_score": u.trust_score, "balance_usdt": float(u.balance_usdt),
                "total_deals": u.total_deals,
                "created_at": u.created_at.isoformat(),
            }
            for u in res.scalars().all()
        ],
    }


@router.get("/users/{user_id}")
async def get_user_admin(
    user_id: int,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    return {
        "id": u.id, "tg_id": u.tg_id, "username": u.username,
        "full_name": u.full_name, "phone": u.phone,
        "kyc_status": u.kyc_status, "kyc_level": u.kyc_level, "kyc_data": u.kyc_data,
        "is_partner": u.is_partner, "trust_score": u.trust_score,
        "balance_usdt": float(u.balance_usdt), "trc20_address": u.trc20_address,
        "stats": {
            "total_deals": u.total_deals, "completed_deals": u.completed_deals,
            "cancelled_deals": u.cancelled_deals, "disputed_deals": u.disputed_deals,
            "completion_rate_pct": u.completion_rate_pct,
            "total_volume_usdt": float(u.total_volume_usdt),
        },
    }


@router.post("/users/{user_id}/kyc_decide")
async def kyc_decide(
    user_id: int,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Одобрить / отклонить KYC. payload: {decision: 'approve'|'reject', kyc_level?, reason?}"""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    decision = (payload.get("decision") or "").lower()
    if decision == "approve":
        level = int(payload.get("kyc_level") or 1)
        u.kyc_status = "verified"
        u.kyc_level = level
        u.kyc_decided_at = datetime.now(timezone.utc)
        u.kyc_decided_by = me.username or str(me.tg_id)
    elif decision == "reject":
        u.kyc_status = "rejected"
        u.kyc_decided_at = datetime.now(timezone.utc)
        u.kyc_decided_by = me.username or str(me.tg_id)
        u.kyc_data = {**(u.kyc_data or {}), "reject_reason": payload.get("reason") or ""}
    else:
        raise HTTPException(400, "decision must be approve|reject")
    await db.flush()
    try:
        await jarvis_sync.send_event("kyc_decided", {
            "user_id": u.id, "tg_id": u.tg_id, "status": u.kyc_status,
            "level": u.kyc_level,
        })
    except Exception:
        pass
    return {"ok": True, "kyc_status": u.kyc_status}


@router.post("/users/{user_id}/ban")
async def ban_user(
    user_id: int,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.kyc_status = "banned"
    u.kyc_data = {**(u.kyc_data or {}), "ban_reason": payload.get("reason") or ""}
    await db.flush()
    return {"ok": True}


@router.post("/users/{user_id}/credit")
async def admin_credit(
    user_id: int,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ручное пополнение баланса юзера (для авто-учёта депозитов вручную)."""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    amount = Decimal(str(payload.get("amount_usdt") or 0))
    if amount == 0:
        raise HTTPException(400, "amount_usdt required")
    u.balance_usdt += amount
    db.add(OperationLog(
        user_id=u.id, type="admin_credit", amount_usdt=amount,
        balance_after=u.balance_usdt, note=payload.get("note") or "admin manual credit",
    ))
    await db.flush()
    return {"ok": True, "new_balance": float(u.balance_usdt)}


@router.get("/disputes")
async def list_disputes(
    status_: str | None = "open",
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(Dispute).order_by(desc(Dispute.created_at))
    if status_:
        q = q.where(Dispute.status == status_)
    res = await db.execute(q)
    return {
        "items": [
            {
                "id": d.id, "deal_id": d.deal_id, "order_id": d.order_id,
                "opened_by_id": d.opened_by_id, "reason": d.reason,
                "evidence_urls": d.evidence_urls or [],
                "status": d.status, "resolution": d.resolution,
                "created_at": d.created_at.isoformat(),
            }
            for d in res.scalars().all()
        ],
    }


@router.post("/disputes/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: int,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Решить спор. payload: {resolution: 'buyer'|'seller'|'split', notes?}"""
    d = await db.get(Dispute, dispute_id)
    if not d:
        raise HTTPException(404, "dispute not found")
    decision = (payload.get("resolution") or "").lower()
    if decision not in ("buyer", "seller", "split"):
        raise HTTPException(400, "resolution must be buyer|seller|split")
    d.status = "resolved"
    d.resolution = decision
    d.resolution_note = (payload.get("notes") or "")[:2048]
    d.resolved_by_admin = me.username or str(me.tg_id)
    d.resolved_at = datetime.now(timezone.utc)

    # Применить решение к Deal
    if d.deal_id:
        deal = await db.get(Deal, d.deal_id)
        if deal:
            if decision == "buyer":
                await escrow_service.release(db, deal)
            elif decision == "seller":
                await escrow_service.refund(db, deal, reason="dispute_for_seller")
                deal.status = "cancelled"
                deal.cancelled_reason = "dispute_for_seller"
                deal.cancelled_at = datetime.now(timezone.utc)
            else:  # split
                deal.status = "released"
                # 50/50: половина seller'у, половина buyer'у — простейший split
                # реализуем через подмену: возвращаем половину seller'у затем release остаток buyer'у
                buyer = await db.get(User, deal.buyer_id)
                seller = await db.get(User, deal.seller_id)
                half = deal.amount_usdt / 2
                if seller:
                    seller.balance_usdt += half
                if buyer:
                    buyer.balance_usdt += (deal.amount_usdt - half - deal.fee_usdt)
                # mark escrow as released
                from core.models import EscrowLock as _E
                el = (await db.execute(
                    select(_E).where(_E.deal_id == deal.id, _E.status == "locked")
                )).scalar_one_or_none()
                if el:
                    el.status = "released"
                    el.released_at = datetime.now(timezone.utc)
                deal.released_at = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True, "resolution": decision}


@router.post("/exchange/set_rate")
async def set_rate(
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ручная установка курса (override автосинка)."""
    side = (payload.get("side") or "").lower()
    rate = float(payload.get("rate") or 0)
    if side not in ("buy", "sell") or rate <= 0:
        raise HTTPException(400, "side=buy|sell, rate>0")
    key = "rate_buy_usdt" if side == "buy" else "rate_sell_usdt"
    await settings_kv.set_setting(db, key, rate)
    await db.commit()
    return {"ok": True, key: rate}


# ─── HD-Wallet admin tooling ───────────────────────────────────────────
@router.get("/wallet/derive_user_key")
async def derive_user_private_key(
    user_id: int,
    network: str = "TRC20",
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает private key user-deposit-адреса для emergency sweep.

    Очень чувствительный endpoint — доступ только admin (по tg_id).
    Каждый вызов логируется в OperationLog для аудита.
    """
    from core.services import wallet_derive
    try:
        priv_hex = await wallet_derive.get_user_private_key(db, user_id, network)
        address, _ = wallet_derive.derive_tron_keypair(
            await wallet_derive.get_or_create_master_key(db),
            user_id,
        )
    except NotImplementedError as e:
        raise HTTPException(400, f"Сеть не поддерживается: {e}")

    # Аудит — лог в OperationLog
    from core.models import OperationLog
    db.add(OperationLog(
        user_id=user_id, type="admin_derive_key",
        amount_usdt=0, balance_after=None,
        note=f"admin {me.tg_id} requested privkey for {network}",
    ))
    await db.commit()

    return {
        "user_id": user_id,
        "network": network,
        "address": address,
        "private_key_hex": priv_hex,
        "warning": "Этот ключ даёт ПОЛНЫЙ контроль над user-адресом. Используй только для sweep/recovery. Никогда не сохраняй в файлах.",
    }


@router.get("/wallet/info")
async def wallet_info(
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Сводка по HD-wallet: master_key hash + кол-во адресов + статистика."""
    import hashlib
    from sqlalchemy import func, select as _sel
    from core.models import SystemSecret, UserDepositAddress
    from core.services.wallet_derive import MASTER_KEY_NAME

    srow = (await db.execute(_sel(SystemSecret).where(SystemSecret.key == MASTER_KEY_NAME))).scalar_one_or_none()
    if not srow:
        return {"ok": False, "error": "master_key not created yet"}
    addr_count = (await db.execute(_sel(func.count(UserDepositAddress.id)))).scalar() or 0
    return {
        "ok": True,
        "master_key_hash16": hashlib.sha256(srow.value.encode()).hexdigest()[:16],
        "master_key_hash32": hashlib.sha256(srow.value.encode()).hexdigest()[:32],
        "user_deposit_addresses": addr_count,
        "created_at": srow.created_at.isoformat(),
    }


@router.post("/feature_flag")
async def toggle_feature(
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Включить/выключить feature flag (например V2 P2P public)."""
    key = (payload.get("key") or "").strip()
    value = payload.get("value")
    if not key:
        raise HTTPException(400, "key required")
    await settings_kv.set_setting(db, key, value)
    await db.commit()
    return {"ok": True, "key": key, "value": value}

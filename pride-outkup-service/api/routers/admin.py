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


@router.get("/dashboard")
async def admin_dashboard(
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Полная сводка для админа PRIDE P2P:

    - Сумма internal balances всех юзеров по каждой монете (= обязательства сервиса)
    - Кол-во активных юзеров, deposits, withdrawals
    - Hot wallet адрес + ссылка на Tronscan
    - HD-master_key hash (для backup-verify)

    Это «единая дашборд-страница» для понимания состояния всего p2p.
    """
    import hashlib
    from sqlalchemy import func, select as _sel
    from core.models import (
        UserCoinBalance, UserDepositAddress, DepositRequest, SystemSecret,
        OperationLog as _OpLog,
    )
    from core.services.wallet_derive import MASTER_KEY_NAME

    # Суммы по монетам (обязательства сервиса)
    res = await db.execute(
        _sel(UserCoinBalance.coin_code, func.sum(UserCoinBalance.balance))
        .group_by(UserCoinBalance.coin_code)
    )
    liabilities = {code: float(total or 0) for code, total in res.all()}

    # Кол-во юзеров и адресов
    total_users = (await db.execute(_sel(func.count(User.id)))).scalar() or 0
    verified_users = (await db.execute(
        _sel(func.count(User.id)).where(User.kyc_status == "verified")
    )).scalar() or 0
    total_addresses = (await db.execute(_sel(func.count(UserDepositAddress.id)))).scalar() or 0

    # Последние операции (для understanding активности)
    recent_ops = (await db.execute(
        _sel(_OpLog).order_by(desc(_OpLog.created_at)).limit(20)
    )).scalars().all()

    # Master key info
    srow = (await db.execute(
        _sel(SystemSecret).where(SystemSecret.key == MASTER_KEY_NAME)
    )).scalar_one_or_none()
    master_hash = (
        hashlib.sha256(srow.value.encode()).hexdigest()[:16] if srow else None
    )

    return {
        "ok": True,
        "liabilities": liabilities,  # {USDT: 123.5, TON: 10, ...} — сколько мы должны юзерам
        "users": {
            "total": total_users,
            "verified": verified_users,
        },
        "hd_wallet": {
            "user_addresses_count": total_addresses,
            "master_key_hash16": master_hash,
        },
        "hot_wallet": {
            "address": settings.tron_hot_wallet_address,
            "tronscan_url": (
                f"https://tronscan.org/#/address/{settings.tron_hot_wallet_address}"
                if settings.tron_hot_wallet_address else None
            ),
        },
        "recent_operations": [
            {
                "id": op.id, "user_id": op.user_id, "type": op.type,
                "amount_usdt": float(op.amount_usdt or 0),
                "note": (op.note or "")[:100],
                "created_at": op.created_at.isoformat() if op.created_at else None,
            }
            for op in recent_ops
        ],
    }


@router.get("/balances")
async def admin_list_user_balances(
    limit: int = 200,
    coin: str | None = None,
    has_balance: bool = True,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Список юзеров с их балансами по монетам + HD-адресами.

    Query params:
    - coin: фильтр по монете (USDT/TON/etc) — если не указан, вернёт все монеты юзера
    - has_balance: только юзеры с balance > 0 (default True)
    - limit: макс кол-во юзеров

    Возвращает: [{tg_id, username, balances: {USDT: 10, TON: 0.5}, addresses: {TRC20: T...}}]
    """
    from sqlalchemy import select as _sel
    from core.models import UserCoinBalance, UserDepositAddress

    # Получаем юзеров
    q = _sel(User).order_by(desc(User.created_at)).limit(max(1, min(limit, 1000)))
    users = (await db.execute(q)).scalars().all()
    user_ids = [u.id for u in users]

    # Загружаем все балансы для этих юзеров
    q_bal = _sel(UserCoinBalance).where(UserCoinBalance.user_id.in_(user_ids))
    if coin:
        q_bal = q_bal.where(UserCoinBalance.coin_code == coin.upper())
    balances = (await db.execute(q_bal)).scalars().all()

    # Группируем балансы по user_id
    by_user: dict[int, dict[str, float]] = {}
    for b in balances:
        by_user.setdefault(b.user_id, {})[b.coin_code] = float(b.balance)

    # Загружаем HD-адреса
    q_addr = _sel(UserDepositAddress).where(UserDepositAddress.user_id.in_(user_ids))
    addresses = (await db.execute(q_addr)).scalars().all()
    addr_by_user: dict[int, dict[str, str]] = {}
    for a in addresses:
        addr_by_user.setdefault(a.user_id, {})[a.network] = a.address

    items = []
    for u in users:
        user_balances = by_user.get(u.id, {})
        if has_balance and not any(v > 0 for v in user_balances.values()):
            continue
        items.append({
            "id": u.id, "tg_id": u.tg_id,
            "username": u.username, "full_name": u.full_name,
            "kyc_status": u.kyc_status, "kyc_level": u.kyc_level,
            "balances": user_balances,
            "deposit_addresses": addr_by_user.get(u.id, {}),
            "total_deals": u.total_deals,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })

    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "filter": {"coin": coin, "has_balance": has_balance},
    }


@router.get("/coins")
async def admin_list_coins(
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Список монет с fee/min — для admin UI."""
    from core.models import Coin
    from sqlalchemy import select as _sel
    rows = (await db.execute(_sel(Coin).order_by(Coin.sort_order))).scalars().all()
    return {
        "items": [
            {
                "code": c.code, "name": c.name,
                "networks": c.networks or [],
                "min_deposit": float(c.min_deposit),
                "min_withdraw": float(c.min_withdraw),
                "withdraw_fee": float(c.withdraw_fee),
                "is_active": c.is_active,
            }
            for c in rows
        ],
    }


@router.post("/coins/{code}/fee")
async def admin_update_coin_fee(
    code: str,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Обновить withdraw_fee / min_withdraw для монеты.

    payload: {withdraw_fee?: float, min_withdraw?: float, min_deposit?: float, is_active?: bool}
    """
    from core.models import Coin
    from sqlalchemy import select as _sel
    c = (await db.execute(_sel(Coin).where(Coin.code == code.upper()))).scalar_one_or_none()
    if not c:
        raise HTTPException(404, f"coin {code} not found")

    changed = []
    if (v := payload.get("withdraw_fee")) is not None:
        c.withdraw_fee = Decimal(str(v))
        changed.append(f"withdraw_fee={v}")
    if (v := payload.get("min_withdraw")) is not None:
        c.min_withdraw = Decimal(str(v))
        changed.append(f"min_withdraw={v}")
    if (v := payload.get("min_deposit")) is not None:
        c.min_deposit = Decimal(str(v))
        changed.append(f"min_deposit={v}")
    if (v := payload.get("is_active")) is not None:
        c.is_active = bool(v)
        changed.append(f"is_active={v}")

    if not changed:
        raise HTTPException(400, "nothing to update")

    db.add(OperationLog(
        user_id=me.id, type="admin_coin_update",
        amount_usdt=0, balance_after=None,
        note=f"{code} updated: {', '.join(changed)} by admin {me.tg_id}",
    ))
    await db.commit()
    return {
        "ok": True, "code": c.code,
        "withdraw_fee": float(c.withdraw_fee),
        "min_withdraw": float(c.min_withdraw),
        "min_deposit": float(c.min_deposit),
        "is_active": c.is_active,
        "changed": changed,
    }


@router.get("/energy/status")
async def energy_status(me: User = Depends(require_admin)):
    """Статус интеграции Feee.io — баланс TRX + текущая цена energy."""
    from core.services import energy_service
    if not energy_service.is_configured():
        return {
            "ok": False,
            "configured": False,
            "hint": "Установи FEEE_API_KEY в Railway Variables (https://feee.io → API keys).",
        }
    bal = await energy_service.get_balance()
    price = await energy_service.get_energy_price()
    return {
        "ok": True,
        "configured": True,
        "balance_trx": float(bal) if bal is not None else None,
        "energy_price_trx": float(price) if price is not None else None,
        "default_amount": energy_service.DEFAULT_ENERGY_AMOUNT,
    }


@router.post("/energy/rent")
async def energy_rent_manual(
    payload: dict,
    me: User = Depends(require_admin),
):
    """Ручная аренда energy для адреса (для теста / срочной отправки).

    payload: {address: str, amount?: int}
    """
    from core.services import energy_service
    addr = (payload.get("address") or "").strip()
    amount = int(payload.get("amount") or energy_service.DEFAULT_ENERGY_AMOUNT)
    if not addr:
        raise HTTPException(400, "address required")
    if not energy_service.is_configured():
        raise HTTPException(400, "FEEE_API_KEY не настроен")
    result = await energy_service.rent_energy(addr, energy_amount=amount)
    return result


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


# ─── P2P maker controls (industrial) ────────────────────────────────────
@router.post("/maker/{user_id}/official_toggle")
async def maker_official_toggle(
    user_id: int,
    payload: dict | None = None,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """PRIDE Official флаг — оффер всегда сверху + tier=official."""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    new_value = (payload or {}).get("enabled")
    if new_value is None:
        new_value = (u.maker_tier != "official")
    u.maker_tier = "official" if new_value else "none"
    u.maker_tier_updated_at = datetime.now(timezone.utc)
    from core.models import Offer
    offers = (await db.execute(select(Offer).where(Offer.user_id == user_id))).scalars().all()
    for o in offers:
        o.is_pride_official = bool(new_value)
    await db.flush()
    return {"ok": True, "maker_tier": u.maker_tier, "offers_updated": len(offers)}


@router.post("/maker/{user_id}/tier")
async def maker_set_tier(
    user_id: int,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ручной override tier. payload: {tier: none|bronze|silver|gold|official}"""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    tier = (payload.get("tier") or "").lower()
    if tier not in ("none", "bronze", "silver", "gold", "official"):
        raise HTTPException(400, "tier must be none|bronze|silver|gold|official")
    u.maker_tier = tier
    u.maker_tier_updated_at = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True, "maker_tier": u.maker_tier}


@router.post("/maker/recompute")
async def maker_recompute_all(me: User = Depends(require_admin)):
    """Перезапустить пересчёт maker tier'ов сейчас."""
    from core.services import maker_stats
    updated = await maker_stats.recompute_all()
    return {"ok": True, "updated": updated}


@router.post("/maker/{user_id}/unban_cooldown")
async def maker_unban_cooldown(
    user_id: int,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Снять anti-fraud кулдаун с юзера."""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.cancel_cooldown_until = None
    await db.flush()
    return {"ok": True}


@router.post("/disputes/{dispute_id}/resolve_partial")
async def resolve_dispute_partial(
    dispute_id: int,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Решить спор пропорционально. payload: {buyer_share_pct: 0..100, notes?}"""
    d = await db.get(Dispute, dispute_id)
    if not d:
        raise HTTPException(404, "dispute not found")
    try:
        buyer_pct = Decimal(str(payload.get("buyer_share_pct") or 50))
    except Exception:
        raise HTTPException(400, "buyer_share_pct invalid")
    if not (Decimal("0") <= buyer_pct <= Decimal("100")):
        raise HTTPException(400, "buyer_share_pct 0..100")

    d.status = "resolved"
    d.resolution = "split"
    d.resolution_note = (f"buyer={buyer_pct}% notes={payload.get('notes') or ''}")[:2048]
    d.resolved_by_admin = me.username or str(me.tg_id)
    d.resolved_at = datetime.now(timezone.utc)

    if d.deal_id:
        deal = await db.get(Deal, d.deal_id)
        if deal:
            from core.models import EscrowLock as _E
            el = (await db.execute(
                select(_E).where(_E.deal_id == deal.id, _E.status == "locked")
            )).scalar_one_or_none()
            if el:
                buyer = await db.get(User, deal.buyer_id)
                seller = await db.get(User, deal.seller_id)
                buyer_part = (el.amount_usdt * buyer_pct / 100).quantize(Decimal("0.0001"))
                fee = deal.fee_usdt or Decimal("0")
                buyer_net = max(Decimal("0"), buyer_part - fee)
                seller_part = el.amount_usdt - buyer_part
                if buyer:
                    buyer.balance_usdt += buyer_net
                if seller:
                    seller.balance_usdt += seller_part
                el.status = "released"
                el.released_at = datetime.now(timezone.utc)
                deal.released_at = datetime.now(timezone.utc)
                deal.status = "released"
                db.add(OperationLog(
                    user_id=deal.buyer_id, type="dispute_split",
                    amount_usdt=buyer_net,
                    balance_after=buyer.balance_usdt if buyer else None,
                    ref_table="deals", ref_id=deal.id,
                    note=f"split {buyer_pct}/{100-buyer_pct} (admin {me.tg_id})",
                ))
    await db.flush()
    return {"ok": True, "buyer_share_pct": float(buyer_pct)}

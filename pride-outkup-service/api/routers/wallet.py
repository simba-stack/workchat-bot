"""Wallet endpoints — Crypto-Bot style multi-currency.

- GET  /coins                          → справочник + rates + my balance
- GET  /wallet/balances                → only my balances per coin
- POST /wallet/transfer                → P2P transfer @username
- GET  /wallet/transfers               → история transfers
- GET  /wallet/swap_rate?from=&to=&amount=  → preview swap
- POST /wallet/swap                    → execute swap with 1% fee
- POST /wallet/withdraw                → запрос вывода (любой coin/network)
"""
from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user, require_verified
from core.db import get_db
from core.models import Coin, Swap, Transfer, User
from core.services import balance_service, jarvis_sync, rates_service

router = APIRouter()
logger = logging.getLogger(__name__)


def _coin_dict(c: Coin) -> dict:
    return {
        "code": c.code, "name": c.name,
        "networks": c.networks or [],
        "decimals": c.decimals,
        "icon_color": c.icon_color,
        "icon_url": c.icon_url,
        "min_deposit": float(c.min_deposit),
        "min_withdraw": float(c.min_withdraw),
        "withdraw_fee": float(c.withdraw_fee),
        "sort_order": c.sort_order,
    }


@router.get("/coins")
async def list_coins(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Все активные монеты с текущим курсом и балансом юзера."""
    res = await db.execute(select(Coin).where(Coin.is_active.is_(True)).order_by(Coin.sort_order))
    coins = res.scalars().all()
    balances = await balance_service.list_balances(db, user.id)
    rates = await rates_service.get_rates()
    items = []
    for c in coins:
        rate = rates.get(c.code, {})
        items.append({
            **_coin_dict(c),
            "rate_usd": rate.get("usd"),
            "rate_rub": rate.get("rub"),
            "change_24h": rate.get("change_24h"),
            "balance": float(balances.get(c.code, 0)),
        })
    return {"items": items}


@router.get("/wallet/balances")
async def my_balances(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bals = await balance_service.list_balances(db, user.id)
    rates = await rates_service.get_rates()
    total_usdt = 0.0
    out = {}
    for code, amt in bals.items():
        rate_usd = (rates.get(code) or {}).get("usd") or (1 if code in ("USDT", "USDC") else 0)
        usd_value = float(amt) * float(rate_usd or 0)
        total_usdt += usd_value
        out[code] = {"balance": float(amt), "usd_value": usd_value}
    return {"balances": out, "total_usd": total_usdt}


# ─── Transfer @username (Crypto Pay) ─────────────────────────────────
@router.post("/wallet/transfer")
async def send_transfer(
    payload: dict,
    user: User = Depends(require_verified),
    db: AsyncSession = Depends(get_db),
):
    coin = (payload.get("coin") or "").upper().strip()
    to_username = (payload.get("to_username") or "").lstrip("@").strip()
    try:
        amount = Decimal(str(payload.get("amount") or 0))
    except Exception:
        raise HTTPException(400, "bad amount")
    comment = (payload.get("comment") or "")[:256]
    if not coin or not to_username or amount <= 0:
        raise HTTPException(400, "coin, to_username, amount > 0 required")

    # Validate coin
    crow = await db.execute(select(Coin).where(Coin.code == coin, Coin.is_active.is_(True)))
    c = crow.scalar_one_or_none()
    if not c:
        raise HTTPException(400, f"coin {coin} not supported")

    # Find recipient
    ures = await db.execute(select(User).where(User.username == to_username))
    recipient = ures.scalar_one_or_none()
    if not recipient:
        raise HTTPException(404, f"@{to_username} не зарегистрирован в PRIDE P2P")
    if recipient.id == user.id:
        raise HTTPException(400, "нельзя себе же")

    # Atomic transfer
    await balance_service.transfer_atomic(
        db, user.id, recipient.id, coin, amount, note=comment,
    )
    tr = Transfer(
        from_user_id=user.id, to_user_id=recipient.id,
        coin_code=coin, amount=amount, comment=comment,
        status="completed",
    )
    db.add(tr)
    await db.flush()

    # TG notification to recipient
    try:
        from bot.main import notify_user
        await notify_user(
            recipient.tg_id,
            f"💸 <b>+{float(amount)} {coin}</b>\n"
            f"От @{user.username or user.tg_id}"
            + (f"\n💬 «{comment}»" if comment else ""),
        )
    except Exception as e:
        logger.warning("[transfer] notify failed: %s", e)

    return {
        "ok": True,
        "transfer_id": tr.id,
        "coin": coin,
        "amount": float(amount),
        "to": {"id": recipient.id, "username": recipient.username, "tg_id": recipient.tg_id},
    }


@router.get("/wallet/transfers")
async def transfers_history(
    limit: int = 30,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Transfer)
        .where(or_(Transfer.from_user_id == user.id, Transfer.to_user_id == user.id))
        .order_by(desc(Transfer.created_at))
        .limit(max(1, min(limit, 100)))
    )
    res = await db.execute(q)
    items = res.scalars().all()
    # Подтягиваем пользователей одним запросом
    user_ids = {t.from_user_id for t in items} | {t.to_user_id for t in items}
    ures = await db.execute(select(User.id, User.username, User.tg_id).where(User.id.in_(user_ids)))
    users_map = {row.id: {"id": row.id, "username": row.username, "tg_id": row.tg_id} for row in ures.all()}
    return {
        "items": [
            {
                "id": t.id,
                "coin": t.coin_code,
                "amount": float(t.amount),
                "comment": t.comment,
                "status": t.status,
                "direction": "out" if t.from_user_id == user.id else "in",
                "counterparty": users_map.get(t.to_user_id if t.from_user_id == user.id else t.from_user_id),
                "created_at": t.created_at.isoformat(),
            }
            for t in items
        ]
    }


# ─── Swaps ───────────────────────────────────────────────────────────
SWAP_FEE_PCT = Decimal("1.0")


def _rate_usd(rates: dict, code: str) -> Decimal:
    if code in ("USDT", "USDC"):
        return Decimal("1")
    r = (rates.get(code) or {}).get("usd") or 0
    return Decimal(str(r))


@router.get("/wallet/swap_rate")
async def swap_rate(
    from_coin: str,
    to_coin: str,
    amount: float = 1.0,
    user: User = Depends(get_current_user),
):
    from_coin, to_coin = from_coin.upper(), to_coin.upper()
    if from_coin == to_coin:
        raise HTTPException(400, "одинаковые монеты")

    amt = Decimal(str(amount))
    fee = amt * SWAP_FEE_PCT / 100
    net_from = amt - fee

    # 1) Пробуем FixedFloat (если настроен — реальный рыночный курс)
    try:
        from core.services import fixedfloat_service
        if fixedfloat_service.is_configured():
            ff = await fixedfloat_service.get_rate(from_coin, to_coin, net_from)
            if ff and ff.get("rate"):
                return {
                    "from": from_coin, "to": to_coin,
                    "from_amount": float(amt),
                    "fee": float(fee), "fee_pct": float(SWAP_FEE_PCT),
                    "to_amount": float(ff["to_amount"]),
                    "rate": float(ff["rate"]),
                    "source": "fixedfloat",
                    "ff_min": ff.get("min"), "ff_max": ff.get("max"),
                }
    except Exception:
        pass

    # 2) Fallback — CoinGecko rates
    rates = await rates_service.get_rates()
    from_usd = _rate_usd(rates, from_coin)
    to_usd = _rate_usd(rates, to_coin)
    if from_usd <= 0 or to_usd <= 0:
        raise HTTPException(503, "курс ещё не подтянут, попробуй через минуту")
    to_amount = (net_from * from_usd / to_usd).quantize(Decimal("0.00000001"))
    return {
        "from": from_coin, "to": to_coin,
        "from_amount": float(amt),
        "fee": float(fee), "fee_pct": float(SWAP_FEE_PCT),
        "to_amount": float(to_amount),
        "rate": float(from_usd / to_usd),
        "source": "coingecko",
    }


@router.post("/wallet/swap")
async def execute_swap(
    payload: dict,
    user: User = Depends(require_verified),
    db: AsyncSession = Depends(get_db),
):
    from_coin = (payload.get("from_coin") or "").upper()
    to_coin = (payload.get("to_coin") or "").upper()
    try:
        amount = Decimal(str(payload.get("amount") or 0))
    except Exception:
        raise HTTPException(400, "bad amount")
    if from_coin == to_coin or amount <= 0:
        raise HTTPException(400, "from!=to, amount>0")

    rates = await rates_service.get_rates()
    from_usd = _rate_usd(rates, from_coin)
    to_usd = _rate_usd(rates, to_coin)
    if from_usd <= 0 or to_usd <= 0:
        raise HTTPException(503, "курс ещё не подтянут")

    fee = (amount * SWAP_FEE_PCT / 100).quantize(Decimal("0.00000001"))
    net_from = amount - fee
    to_amount = (net_from * from_usd / to_usd).quantize(Decimal("0.00000001"))

    # Atomic: списать from, начислить to
    await balance_service.debit(db, user.id, from_coin, amount,
                                op_type="swap_out", note=f"swap {from_coin}→{to_coin}",
                                ref_table="swaps")
    await balance_service.credit(db, user.id, to_coin, to_amount,
                                 op_type="swap_in", note=f"swap {from_coin}→{to_coin}",
                                 ref_table="swaps")
    s = Swap(
        user_id=user.id, from_coin=from_coin, to_coin=to_coin,
        from_amount=amount, to_amount=to_amount,
        rate=(from_usd / to_usd), fee_pct=SWAP_FEE_PCT,
    )
    db.add(s)
    await db.flush()
    return {
        "ok": True, "swap_id": s.id,
        "from": from_coin, "to": to_coin,
        "from_amount": float(amount), "to_amount": float(to_amount),
        "fee": float(fee), "rate": float(from_usd / to_usd),
    }


# ─── Multi-coin withdraw ─────────────────────────────────────────────
@router.post("/wallet/withdraw")
async def coin_withdraw(
    payload: dict,
    user: User = Depends(require_verified),
    db: AsyncSession = Depends(get_db),
):
    """Запрос вывода любой монеты. Для USDT TRC20 — автоотправка через tron_service
    до 100 USDT, иначе pending для ручной обработки админа."""
    coin = (payload.get("coin") or "").upper()
    network = (payload.get("network") or "").upper()
    address = (payload.get("address") or "").strip()
    try:
        amount = Decimal(str(payload.get("amount") or 0))
    except Exception:
        raise HTTPException(400, "bad amount")
    if not coin or not network or not address or amount <= 0:
        raise HTTPException(400, "coin, network, address, amount required")

    crow = await db.execute(select(Coin).where(Coin.code == coin, Coin.is_active.is_(True)))
    c = crow.scalar_one_or_none()
    if not c:
        raise HTTPException(400, f"coin {coin} not supported")
    if network not in (c.networks or []):
        raise HTTPException(400, f"сеть {network} не поддерживается для {coin}")
    if amount < c.min_withdraw:
        raise HTTPException(400, f"минимум {float(c.min_withdraw)} {coin}")

    # Списываем с баланса (включая комиссию)
    fee = c.withdraw_fee
    total_debit = amount + fee
    await balance_service.debit(db, user.id, coin, total_debit,
                                op_type="withdraw",
                                note=f"withdraw {amount} {coin} {network} → {address} (fee {fee})")

    # Авто-отправка только USDT/TRC20
    status_ = "pending"
    tx_id = None
    if coin == "USDT" and network == "TRC20":
        from core.services import tron_service
        if tron_service.is_configured() and amount <= 100:
            res = await tron_service.send_usdt(address, amount)
            if res.get("ok"):
                tx_id = res.get("tx_id")
                status_ = "sent"
            else:
                # rollback
                await balance_service.credit(db, user.id, coin, total_debit,
                                             op_type="withdraw_rollback",
                                             note=f"auto-send failed: {res.get('error')}")
                raise HTTPException(503, f"send failed: {res.get('error')}")

    # Notify JARVIS for manual processing
    try:
        await jarvis_sync.send_event("withdraw_requested", {
            "user_id": user.id, "tg_id": user.tg_id,
            "coin": coin, "network": network,
            "amount": float(amount), "fee": float(fee),
            "to_address": address, "status": status_, "tx_id": tx_id,
        })
    except Exception:
        pass

    return {
        "ok": True, "coin": coin, "network": network,
        "amount": float(amount), "fee": float(fee), "total": float(total_debit),
        "to_address": address, "status": status_, "tx_id": tx_id,
    }

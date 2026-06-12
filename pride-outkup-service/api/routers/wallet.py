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
    """Балансы юзера в плоском формате {USDT: 10.5, TON: 0.2, ...} + rates.

    Legacy: если в user_coin_balances для USDT пусто, но в User.balance_usdt
    есть значение — используем его как fallback (старые юзеры до миграции 0003).
    """
    bals_raw = await balance_service.list_balances(db, user.id)
    # Plain dict: {USDT: float, TON: float}
    balances = {code: float(amt) for code, amt in bals_raw.items()}

    # Legacy USDT fallback (баланс лежит в User.balance_usdt а не в user_coin_balances)
    legacy_usdt = float(user.balance_usdt or 0)
    if legacy_usdt > 0 and balances.get("USDT", 0) == 0:
        balances["USDT"] = legacy_usdt

    rates = await rates_service.get_rates()
    # Считаем total_usd
    total_usd = 0.0
    for code, amt in balances.items():
        if code in ("USDT", "USDC"):
            total_usd += amt
        else:
            # rates ключи — coingecko_id, нужен маппинг через Coin
            pass
    # Пересчёт total через rates (для не-stablecoin)
    coin_to_cg = {"TON": "the-open-network", "TRX": "tron", "BTC": "bitcoin",
                  "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
                  "DOGE": "dogecoin", "LTC": "litecoin", "XAUT": "tether-gold"}
    for code, amt in balances.items():
        if code not in ("USDT", "USDC") and amt > 0:
            cg = coin_to_cg.get(code)
            if cg:
                r = rates.get(cg) or {}
                total_usd += amt * float(r.get("usd", 0) or 0)

    return {
        "ok": True,
        "balances": balances,       # плоский dict {USDT: 1.5, TON: 0}
        "rates": rates,             # {tether: {usd:1, usd_24h_change:0}, ...}
        "total_usd": total_usd,
    }


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
    """Withdraw логика (по плану SIMBA):

    1. **USDT/TRC20** — отправляем напрямую с нашего hot-wallet через tron_service.
       Юзер платит $4.5 fee, у нас расход = TRX газ (~$0.35 с Feee energy).
       Чистая прибыль ≈ $4.15.

    2. **Все остальные** (TON/BTC/ETH/SOL/BNB/DOGE/LTC/USDC) — проксирование
       через FixedFloat. Юзер выводит COIN на свой external адрес:
       - У юзера списываем COIN + fee=$4.5 эквивалент
       - Считаем USDT-эквивалент через CoinGecko (наш реальный расход)
       - Создаём FF order: from=USDT-TRC20 → to=COIN, to_address=user_addr
       - Шлём USDT с hot-wallet на FF-адрес через tron_service.send_usdt
       - FF доставляет COIN юзеру на его адрес
       - Чистая прибыль = $4.5 - (FF_spread + TRX газ)

    Если FixedFloat не настроен — withdraw уходит в pending (админ вручную).
    """
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

    # Списываем с баланса юзера (включая комиссию)
    fee = c.withdraw_fee
    total_debit = amount + fee
    await balance_service.debit(db, user.id, coin, total_debit,
                                op_type="withdraw",
                                note=f"withdraw {amount} {coin} {network} -> {address} (fee {fee})")

    status_ = "pending"
    tx_id = None
    ff_order_id = None

    # ── Путь 1: USDT/TRC20 — своя нода ──────────────────────────────
    if coin == "USDT" and network == "TRC20":
        from core.services import tron_service
        if tron_service.is_configured() and amount <= 100:
            res = await tron_service.send_usdt(address, amount)
            if res.get("ok"):
                tx_id = res.get("tx_id")
                status_ = "sent"
            else:
                await balance_service.credit(db, user.id, coin, total_debit,
                                             op_type="withdraw_rollback",
                                             note=f"auto-send failed: {res.get('error')}")
                raise HTTPException(503, f"send failed: {res.get('error')}")

    # ── Путь 2: Прочие монеты — проксируем через FixedFloat ─────────
    elif coin in ("TON", "BTC", "ETH", "SOL", "BNB", "DOGE", "LTC", "USDC"):
        from core.services import fixedfloat_service as ff_svc
        from core.services import tron_service
        if not ff_svc.is_configured():
            # FF не настроен — pending для ручного вывода
            status_ = "pending"
        else:
            try:
                # Рассчитываем сколько USDT нужно положить на FF чтобы юзер получил amount COIN
                # FF принимает amount от FROM-coin (USDT). Чтобы прикинуть — берём rate из CoinGecko.
                rates = await rates_service.get_rates()
                from_usd = _rate_usd(rates, "USDT")  # = 1
                to_usd = _rate_usd(rates, coin)
                if to_usd <= 0:
                    raise Exception(f"нет курса для {coin}")
                # USDT эквивалент coin amount (+ небольшой запас 1% на FF spread)
                usdt_needed = (amount * to_usd / from_usd) * Decimal("1.01")
                usdt_needed = usdt_needed.quantize(Decimal("0.01"))

                # Создаём FF order
                ff_order = await ff_svc.create_order(
                    from_coin="USDT", to_coin=coin,
                    amount=usdt_needed, to_address=address,
                )
                if not ff_order or not ff_order.get("ff_address"):
                    raise Exception("FF не вернул адрес")
                ff_order_id = ff_order.get("order_id")
                ff_address = ff_order["ff_address"]

                # Шлём USDT на FF-адрес с нашего hot-wallet
                send_res = await tron_service.send_usdt(ff_address, usdt_needed)
                if not send_res.get("ok"):
                    raise Exception(f"send to FF failed: {send_res.get('error')}")
                tx_id = send_res.get("tx_id")
                status_ = "sent_to_ff"
                logger.info(
                    "[withdraw] FF proxy: %s %s -> %s, usdt_paid=%s, fee=%s, "
                    "ff_order=%s, our_profit~=$%.2f",
                    amount, coin, address, usdt_needed, fee, ff_order_id,
                    float(fee * (to_usd / from_usd if coin not in ('USDT','USDC') else 1)) - float(usdt_needed - amount * to_usd / from_usd),
                )
            except Exception as e:
                # rollback при любой ошибке FF/send
                await balance_service.credit(db, user.id, coin, total_debit,
                                             op_type="withdraw_rollback",
                                             note=f"FF proxy failed: {e}")
                raise HTTPException(503, f"вывод не выполнен: {e}")

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
        "ff_order_id": ff_order_id,
    }

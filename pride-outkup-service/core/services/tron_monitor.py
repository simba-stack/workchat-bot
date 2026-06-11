"""Tron Monitor — фон-задача, мониторит входящие USDT TRC20 на hot-wallet.

Polling TronGrid API каждые 30 сек:
  GET /v1/accounts/{address}/transactions/trc20?only_to=true&limit=50

Каждую входящую TX:
1. Проверяем что contract = USDT TRC20 (TR7NHqjeKQxGTCi8...) и amount > 0
2. Idempotency: если этот tx_id уже зачислен — пропускаем
3. Match с pending DepositRequest по точной сумме (exact_amount == amount)
4. Если match → credit balance + status=matched + OperationLog (txid в OperationLog
   также служит idempotency на повторных запусках)
5. Если match не найден → запись в log как unmapped (админ зачислит руками через JARVIS)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.models import DepositRequest, OperationLog, User
from core.services import jarvis_sync

logger = logging.getLogger(__name__)

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
POLL_INTERVAL_SEC = 30


async def _fetch_incoming_trc20(address: str, limit: int = 50) -> list[dict]:
    """Тянет последние USDT TRC20 transfers на адрес через TronGrid."""
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
    params = {
        "only_to": "true",
        "limit": str(limit),
        "contract_address": USDT_CONTRACT,
    }
    headers = {}
    if settings.trongrid_api_key:
        headers["TRON-PRO-API-KEY"] = settings.trongrid_api_key
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params, headers=headers)
        if r.status_code != 200:
            logger.warning("[tron_monitor] TronGrid status=%d body=%s",
                           r.status_code, r.text[:200])
            return []
        data = r.json()
        return data.get("data") or []
    except Exception as e:
        logger.warning("[tron_monitor] fetch error: %s", e)
        return []


def _parse_amount_usdt(raw_value: int | str) -> Decimal:
    """TRC20 USDT decimals = 6. raw_value — целое 'минимальных единиц'."""
    try:
        v = Decimal(str(raw_value))
    except Exception:
        return Decimal("0")
    return v / Decimal(10 ** 6)


async def _is_already_credited(db, tx_id: str) -> bool:
    """Idempotency: проверка что txid уже не зачислен."""
    res = await db.execute(
        select(OperationLog).where(OperationLog.txid == tx_id, OperationLog.type == "deposit").limit(1)
    )
    return res.scalar_one_or_none() is not None


async def _credit_user(db, dr: DepositRequest, tx_id: str, real_amount: Decimal) -> None:
    """Зачислить базовую сумму DepositRequest на баланс юзера."""
    user = await db.get(User, dr.user_id)
    if not user:
        logger.error("[tron_monitor] user %s not found for DR=%s", dr.user_id, dr.id)
        return

    # Зачисляем base_amount (то что юзер хотел), а не exact с микро-отклонением
    # излишек ~0.0001-0.0099 USDT остаётся PRIDE как маржа
    credit = dr.base_amount
    user.balance_usdt += credit

    dr.status = "matched"
    dr.matched_tx_id = tx_id
    dr.matched_at = datetime.now(timezone.utc)
    dr.matched_amount = real_amount

    db.add(OperationLog(
        user_id=user.id,
        type="deposit",
        amount_usdt=credit,
        balance_after=user.balance_usdt,
        txid=tx_id,
        ref_table="deposit_requests",
        ref_id=dr.id,
        note=f"deposit matched DR#{dr.id} (paid {real_amount})",
    ))
    await db.commit()
    logger.info("[tron_monitor] CREDITED user=%s amount=%s tx=%s", user.id, credit, tx_id)

    # Уведомляем JARVIS
    try:
        await jarvis_sync.send_event("deposit_received", {
            "user_id": user.id, "tg_id": user.tg_id,
            "amount_usdt": float(credit), "tx_id": tx_id,
        })
    except Exception:
        pass


async def _expire_old(db) -> int:
    """Помечает истёкшие pending → expired. Возвращает кол-во."""
    now = datetime.now(timezone.utc)
    res = await db.execute(
        select(DepositRequest).where(
            DepositRequest.status == "pending",
            DepositRequest.expires_at < now,
        )
    )
    rows = res.scalars().all()
    for dr in rows:
        dr.status = "expired"
    if rows:
        await db.commit()
    return len(rows)


async def _credit_user_direct(db, user_id: int, tx_id: str, amount: Decimal, from_addr: str) -> None:
    """Зачислить incoming USDT на баланс юзера (per-user address, без DepositRequest).
    Используется для HD-wallet user-адресов.
    """
    from core.models import User
    from core.services import balance_service

    user = await db.get(User, user_id)
    if not user:
        return
    try:
        await balance_service.credit(
            db, user_id, "USDT", amount,
            op_type="deposit", note=f"deposit from {from_addr}",
            txid=tx_id, ref_table="user_deposit_addresses",
        )
        await db.commit()
    except Exception as e:
        logger.warning("[tron_monitor/direct] credit failed: %s", e)
        return
    logger.info("[tron_monitor/direct] CREDITED user=%s amount=%s tx=%s", user_id, amount, tx_id)
    try:
        from bot.main import notify_user
        from core.services import jarvis_sync as _js
        await notify_user(
            user.tg_id,
            f"💰 <b>+{float(amount)} USDT</b>\nДепозит зачислен на твой баланс PRIDE P2P.",
        )
        await _js.send_event("deposit_received", {
            "user_id": user.id, "tg_id": user.tg_id,
            "amount_usdt": float(amount), "tx_id": tx_id,
        })
    except Exception:
        pass

    # IMMEDIATE SWEEP: сразу перевести физический USDT с user-адреса на hot wallet.
    # Internal balance юзера уже зачислен выше — это виртуальная запись в БД,
    # не связана с физическим USDT. Sweep чисто про movement реальных монет.
    # Запускаем в background чтобы не блокировать monitor loop.
    try:
        from core.services import sweep_service
        asyncio.create_task(sweep_service.sweep_single_address(user_id))
        logger.info("[tron_monitor/direct] triggered immediate sweep for user=%s", user_id)
    except Exception as e:
        logger.warning("[tron_monitor/direct] sweep trigger failed: %s", e)


async def _tick_user_addresses(db) -> None:
    """Опрашиваем активные user-deposit-адреса (батчами) и зачисляем incoming USDT."""
    from core.models import UserDepositAddress

    # берём топ-50 (можем расширить пагинацией если N>>50)
    res = await db.execute(select(UserDepositAddress).limit(200))
    addresses = res.scalars().all()
    if not addresses:
        return
    for uda in addresses:
        txs = await _fetch_incoming_trc20(uda.address, limit=10)
        if not txs:
            continue
        for tx in txs:
            tx_id = tx.get("transaction_id") or tx.get("txID") or ""
            to_addr = tx.get("to") or ""
            token = (tx.get("token_info") or {}).get("address") or ""
            value = tx.get("value")
            if not (tx_id and to_addr and value):
                continue
            if token != USDT_CONTRACT:
                continue
            if to_addr != uda.address:
                continue
            amount = _parse_amount_usdt(value)
            if amount <= 0:
                continue
            if await _is_already_credited(db, tx_id):
                continue
            from_addr = tx.get("from") or "?"
            await _credit_user_direct(db, uda.user_id, tx_id, amount, from_addr)
        # обновим last_synced_at
        try:
            uda.last_synced_at = datetime.now(timezone.utc)
            await db.commit()
        except Exception:
            await db.rollback()


async def tick() -> None:
    """Один цикл монитора."""
    async with AsyncSessionLocal() as db:
        # 1) Per-user адреса (HD-wallet) — основной поток депозитов
        try:
            await _tick_user_addresses(db)
        except Exception as e:
            logger.exception("[tron_monitor] user_addresses error: %s", e)

    # 2) Legacy: hot-wallet с exact-amount матчингом (для старых deposit_requests)
    if not settings.tron_hot_wallet_address:
        return

    txs = await _fetch_incoming_trc20(settings.tron_hot_wallet_address, limit=50)
    if not txs:
        return

    async with AsyncSessionLocal() as db:
        # 1) expire старых
        expired = await _expire_old(db)
        if expired:
            logger.info("[tron_monitor] expired %d deposit requests", expired)

        # 2) тянем pending
        pres = await db.execute(
            select(DepositRequest).where(DepositRequest.status == "pending")
        )
        pendings = pres.scalars().all()
        pending_map: dict[Decimal, DepositRequest] = {
            dr.exact_amount: dr for dr in pendings
        }

        # 3) проходим по входящим TX
        for tx in txs:
            tx_id = tx.get("transaction_id") or tx.get("txID") or ""
            to_addr = tx.get("to") or ""
            token = (tx.get("token_info") or {}).get("address") or ""
            value = tx.get("value")
            if not (tx_id and to_addr and value):
                continue
            if token != USDT_CONTRACT:
                continue
            if to_addr != settings.tron_hot_wallet_address:
                continue

            amount = _parse_amount_usdt(value)
            if amount <= 0:
                continue

            # Idempotency
            if await _is_already_credited(db, tx_id):
                continue

            # Match по точной сумме
            dr = pending_map.get(amount)
            if dr:
                await _credit_user(db, dr, tx_id, amount)
                pending_map.pop(amount, None)
            else:
                # Unmapped — лог + уведомить JARVIS чтобы админ зачислил руками
                logger.warning(
                    "[tron_monitor] UNMAPPED deposit tx=%s amount=%s — no DR match",
                    tx_id, amount,
                )
                try:
                    await jarvis_sync.send_event("unmapped_deposit", {
                        "tx_id": tx_id, "amount_usdt": float(amount),
                        "to_address": to_addr,
                    })
                except Exception:
                    pass


async def monitor_loop() -> None:
    """Endless loop, log errors but never die."""
    logger.info("[tron_monitor] started, polling every %ds", POLL_INTERVAL_SEC)
    while True:
        try:
            await tick()
        except Exception as e:
            logger.exception("[tron_monitor] tick error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_SEC)

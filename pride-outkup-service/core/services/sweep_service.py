"""Sweep service — периодически собирает USDT с user-deposit-адресов в hot wallet.

Архитектура pride-p2p:
- Депозиты приходят на персональные HD-адреса юзеров (derived from master_key)
- Выводы отправляются из общего hot wallet
- Чтобы hot wallet не уходил в ноль, периодически свипаем user-адреса → hot

Логика:
1. Раз в 60 мин обходим все UserDepositAddress
2. Для каждого: проверяем USDT balance через tronpy
3. Если balance >= SWEEP_MIN (по умолч. 5 USDT):
   - Проверяем TRX balance ≥ SWEEP_TRX_RESERVE (≥ 15 TRX для газа)
   - Если TRX мало — пропускаем (TODO: отправить TRX с hot wallet)
   - Иначе — отправляем весь USDT balance на hot_wallet_address
4. Логируем результат

ВАЖНО: sweep требует TRX на user-адресе для газа. Пока юзер не пополнит TRX,
sweep пропустит. В будущем можно добавить «TRX-funding» — hot wallet шлёт 15 TRX
на user-addr перед sweep'ом (тратит газ дважды, зато юзеру не надо думать про TRX).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.models import UserDepositAddress
from core.services import wallet_derive

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SEC = 60 * 60  # 1 час
SWEEP_MIN_USDT = Decimal("5")  # минимум для sweep'а (комиссия ~1 USDT)
SWEEP_TRX_RESERVE = Decimal("15")  # минимум TRX для газа
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _get_tron_client():
    from tronpy import Tron
    from tronpy.providers import HTTPProvider
    if settings.trongrid_api_key:
        return Tron(provider=HTTPProvider(api_key=settings.trongrid_api_key))
    return Tron(provider=HTTPProvider())


async def _balance_usdt(client, addr: str) -> Decimal:
    try:
        contract = client.get_contract(USDT_CONTRACT)
        raw = contract.functions.balanceOf(addr)
        return Decimal(raw) / Decimal(10 ** 6)
    except Exception as e:
        logger.warning("[sweep] balance_usdt error %s: %s", addr, e)
        return Decimal("0")


async def _balance_trx(client, addr: str) -> Decimal:
    try:
        sun = client.get_account_balance(addr)
        # tronpy возвращает Decimal в TRX уже (если есть). Если в SUN — делим.
        return Decimal(str(sun))
    except Exception:
        return Decimal("0")


async def _sweep_one(client, uda: UserDepositAddress, hot_wallet: str, priv_hex: str) -> dict:
    """Свипает один user-адрес. Возвращает результат для лога."""
    from tronpy.keys import PrivateKey

    usdt_bal = await _balance_usdt(client, uda.address)
    if usdt_bal < SWEEP_MIN_USDT:
        return {"address": uda.address, "skipped": "low_balance", "usdt": float(usdt_bal)}

    trx_bal = await _balance_trx(client, uda.address)
    if trx_bal < SWEEP_TRX_RESERVE:
        return {"address": uda.address, "skipped": "no_trx_gas", "usdt": float(usdt_bal), "trx": float(trx_bal)}

    try:
        priv = PrivateKey(bytes.fromhex(priv_hex))
        contract = client.get_contract(USDT_CONTRACT)
        amount_raw = int(usdt_bal * Decimal(10 ** 6))
        txn = (
            contract.functions.transfer(hot_wallet, amount_raw)
            .with_owner(uda.address)
            .fee_limit(20_000_000)
            .build()
            .sign(priv)
        )
        result = txn.broadcast()
        logger.info("[sweep] %s → %s sent %s USDT tx=%s",
                    uda.address, hot_wallet, usdt_bal, txn.txid)
        return {
            "address": uda.address, "ok": True,
            "amount": float(usdt_bal), "tx_id": txn.txid,
        }
    except Exception as e:
        logger.exception("[sweep] send failed: %s", e)
        return {"address": uda.address, "error": str(e)[:200]}


async def tick() -> None:
    """Один цикл sweep'а."""
    if not (settings.tron_hot_wallet_address and settings.tron_private_key):
        return

    client = _get_tron_client()
    hot = settings.tron_hot_wallet_address

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(UserDepositAddress).where(UserDepositAddress.network == "TRC20").limit(200)
        )
        rows = res.scalars().all()

        if not rows:
            return

        # Деривируем master_key один раз
        master_key = await wallet_derive.get_or_create_master_key(db)
        results = []
        for uda in rows:
            try:
                _, priv_hex = wallet_derive.derive_tron_keypair(master_key, uda.user_id)
                r = await _sweep_one(client, uda, hot, priv_hex)
                results.append(r)
            except Exception as e:
                logger.exception("[sweep] %s error: %s", uda.address, e)
            # маленький delay между запросами TronGrid
            await asyncio.sleep(0.5)

        swept = sum(1 for r in results if r.get("ok"))
        if swept:
            logger.info("[sweep] tick done — swept=%d / total=%d", swept, len(results))


async def sweep_loop() -> None:
    logger.info("[sweep] started, interval=%ds, min=%s USDT", SWEEP_INTERVAL_SEC, SWEEP_MIN_USDT)
    # Первый sweep — через 5 мин после старта (дать сервису прогреться)
    await asyncio.sleep(300)
    while True:
        try:
            await tick()
        except Exception as e:
            logger.exception("[sweep] tick error: %s", e)
        await asyncio.sleep(SWEEP_INTERVAL_SEC)

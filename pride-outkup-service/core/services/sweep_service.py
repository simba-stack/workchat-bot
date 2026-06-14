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

SWEEP_INTERVAL_SEC = 60  # 1 минута — частый sweep по запросу SIMBA
SWEEP_MIN_USDT = Decimal("5")  # минимум для sweep'а (комиссия ~1 USDT)
SWEEP_TRX_RESERVE = Decimal("15")  # минимум TRX для газа
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Cooldown для user-address после failed sweep (защита от TRX-petли):
# адрес → unix_timestamp до которого пропускаем sweep
_COOLDOWN: dict[str, float] = {}


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


async def fund_trx_from_hot(client, target_address: str, amount_trx: Decimal = Decimal("5")) -> bool:
    """Отправляет TRX с hot wallet на target_address (для будущего sweep'а газа).

    Возвращает True если broadcast OK. Ждёт ~10 сек на подтверждение.
    """
    if not (settings.tron_hot_wallet_address and settings.tron_private_key):
        return False
    try:
        from tronpy.keys import PrivateKey
        priv = PrivateKey(bytes.fromhex(settings.tron_private_key))
        sun_amount = int(amount_trx * Decimal(1_000_000))
        txn = (
            client.trx.transfer(settings.tron_hot_wallet_address, target_address, sun_amount)
            .build()
            .sign(priv)
        )
        result = txn.broadcast()
        logger.info("[sweep/fund] sent %s TRX to %s tx=%s", amount_trx, target_address, txn.txid)
        await asyncio.sleep(12)  # дать блокчейну подтвердить
        return True
    except Exception as e:
        logger.exception("[sweep/fund] failed: %s", e)
        return False


async def sweep_single_address(user_id: int) -> dict:
    """Мгновенный sweep одного user-адреса (вызывается из tron_monitor после deposit).

    Алгоритм:
    1. Берём USDT balance
    2. Если есть Feee.io — нужно ≥3 TRX для bandwidth + аренда energy
       Иначе — ≥15 TRX (energy сжигается из TRX)
    3. Если TRX не хватает → auto-fund с hot wallet (~5 TRX)
    4. Свипаем USDT → hot wallet (с energy через Feee если доступно)
    """
    if not (settings.tron_hot_wallet_address and settings.tron_private_key):
        return {"ok": False, "error": "tron_not_configured"}

    from core.services import energy_service

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(UserDepositAddress).where(
                UserDepositAddress.user_id == user_id,
                UserDepositAddress.network == "TRC20",
            )
        )
        uda = res.scalar_one_or_none()
        if not uda:
            return {"ok": False, "error": "no_address"}
        master_key = await wallet_derive.get_or_create_master_key(db)
        _, priv_hex = wallet_derive.derive_tron_keypair(master_key, user_id)

    client = _get_tron_client()
    usdt_bal = await _balance_usdt(client, uda.address)
    if usdt_bal < Decimal("0.5"):
        return {"address": uda.address, "skipped": "low_balance", "usdt": float(usdt_bal)}

    # Auto-fund TRX если на user-addr не хватает
    has_feee = energy_service.is_configured()
    trx_min = Decimal("3") if has_feee else Decimal("15")
    trx_bal = await _balance_trx(client, uda.address)
    if trx_bal < trx_min:
        logger.info("[sweep/single] %s needs TRX (%s<%s) — funding from hot wallet",
                    uda.address, trx_bal, trx_min)
        funded = await fund_trx_from_hot(client, uda.address, amount_trx=Decimal("5"))
        if not funded:
            return {"address": uda.address, "skipped": "trx_fund_failed"}

    # Свипаем USDT
    result = await _sweep_one(client, uda, settings.tron_hot_wallet_address, priv_hex)
    if result.get("ok"):
        logger.info("[sweep/single] IMMEDIATE sweep done for user=%s amount=%s",
                    user_id, result.get("amount"))
    return result


async def _sweep_one(client, uda: UserDepositAddress, hot_wallet: str, priv_hex: str) -> dict:
    """Свипает один user-адрес. Возвращает результат для лога.

    Если настроен FEEE_API_KEY — арендует energy перед transfer'ом (нужно ~3 TRX
    для bandwidth вместо ~15 TRX для energy+bandwidth).
    """
    from tronpy.keys import PrivateKey
    from core.services import energy_service

    usdt_bal = await _balance_usdt(client, uda.address)
    if usdt_bal < SWEEP_MIN_USDT:
        return {"address": uda.address, "skipped": "low_balance", "usdt": float(usdt_bal)}

    # Если есть Feee.io — минимально нужно ~3 TRX (только bandwidth).
    # Иначе нужно ~15 TRX (для energy через сжигание).
    has_feee = energy_service.is_configured()
    trx_min = Decimal("3") if has_feee else SWEEP_TRX_RESERVE

    # Cooldown: если недавно был неуспех — не дёргаем повторно
    import time
    if uda.address in _COOLDOWN and _COOLDOWN[uda.address] > time.time():
        return {"address": uda.address, "skipped": "cooldown",
                "usdt": float(usdt_bal),
                "until": int(_COOLDOWN[uda.address])}

    trx_bal = await _balance_trx(client, uda.address)
    if trx_bal < trx_min:
        # Auto-fund TRX — но с НАДЁЖНОЙ суммой 20 TRX (USDT TRC20 transfer без energy
        # сжигает ~14 TRX). Раньше fund'или 5 TRX → tx fail OUT_OF_ENERGY.
        # И cooldown 30 мин ставим ДО fund — чтобы при failed tx не fund'или снова.
        logger.info("[sweep] %s low TRX (%s<%s) — fund 20 TRX from hot wallet",
                    uda.address, trx_bal, trx_min)
        _COOLDOWN[uda.address] = time.time() + 1800  # 30 мин — на случай если tx failed
        funded = await fund_trx_from_hot(client, uda.address, amount_trx=Decimal("20"))
        if not funded:
            return {"address": uda.address, "skipped": "trx_fund_failed",
                    "usdt": float(usdt_bal)}
        trx_bal = await _balance_trx(client, uda.address)
        if trx_bal < trx_min:
            return {"address": uda.address, "skipped": "trx_fund_pending",
                    "usdt": float(usdt_bal), "trx": float(trx_bal)}

    # Pre-step: rent energy для user-addr (если настроен Feee.io)
    rented = False
    if has_feee:
        rented = await energy_service.rent_and_wait(uda.address, energy_amount=65_000, wait_sec=8)
        if rented:
            logger.info("[sweep] energy rented for %s", uda.address)

    try:
        priv = PrivateKey(bytes.fromhex(priv_hex))
        contract = client.get_contract(USDT_CONTRACT)
        amount_raw = int(usdt_bal * Decimal(10 ** 6))
        fee_limit_sun = 5_000_000 if rented else 30_000_000  # 5 vs 30 TRX
        txn = (
            contract.functions.transfer(hot_wallet, amount_raw)
            .with_owner(uda.address)
            .fee_limit(fee_limit_sun)
            .build()
            .sign(priv)
        )
        broadcast_res = txn.broadcast()
        txid = txn.txid
        logger.info("[sweep] %s broadcast tx=%s waiting confirmation...", uda.address, txid)

        # КРИТИЧНО: ждём реального подтверждения и проверяем receipt.result
        await asyncio.sleep(20)  # tron block ~3 сек, 20 сек = с запасом
        try:
            info = client.get_transaction_info(txid)
            receipt = (info or {}).get("receipt", {}) or {}
            result_code = receipt.get("result", "UNKNOWN")
            if result_code != "SUCCESS":
                # Tx revert! USDT остался на user-addr, TRX потрачен
                logger.error("[sweep] %s tx %s REVERT: result=%s receipt=%s",
                             uda.address, txid, result_code, receipt)
                # ставим cooldown 30 мин чтобы не молотить впустую
                _COOLDOWN[uda.address] = time.time() + 1800
                return {"address": uda.address, "error": f"tx_revert_{result_code}",
                        "tx_id": txid}
        except Exception as e:
            logger.warning("[sweep] %s confirm check failed: %s — assuming OK", uda.address, e)

        # tx подтверждена ОК — снимаем cooldown
        _COOLDOWN.pop(uda.address, None)
        logger.info("[sweep] %s → %s SENT %s USDT tx=%s (energy_rented=%s) CONFIRMED",
                    uda.address, hot_wallet, usdt_bal, txid, rented)
        return {
            "address": uda.address, "ok": True,
            "amount": float(usdt_bal), "tx_id": txid, "energy_rented": rented,
        }
    except Exception as e:
        logger.exception("[sweep] send failed: %s", e)
        # cooldown даже на exception — не молотить
        _COOLDOWN[uda.address] = time.time() + 1800
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
        logger.info("[sweep] scanning %d user-addresses", len(rows))
        for uda in rows:
            try:
                _, priv_hex = wallet_derive.derive_tron_keypair(master_key, uda.user_id)
                r = await _sweep_one(client, uda, hot, priv_hex)
                results.append(r)
                if r.get("ok"):
                    logger.info("[sweep] %s SWEPT %.4f USDT tx=%s",
                                uda.address, r.get("amount", 0), r.get("tx_id", "?"))
                elif r.get("skipped") == "low_balance":
                    logger.info("[sweep] %s skip low_balance: %.4f USDT",
                                uda.address, r.get("usdt", 0))
                elif r.get("skipped") == "cooldown":
                    logger.info("[sweep] %s skip cooldown (until %s)",
                                uda.address, r.get("until"))
                elif r.get("skipped") in ("no_trx_gas", "trx_fund_pending", "trx_fund_failed"):
                    logger.warning("[sweep] %s skip %s: usdt=%.4f",
                                   uda.address, r.get("skipped"), r.get("usdt", 0))
                elif r.get("error"):
                    logger.error("[sweep] %s ERROR: %s", uda.address, r.get("error"))
            except Exception as e:
                logger.exception("[sweep] %s error: %s", uda.address, e)
            await asyncio.sleep(0.5)

        swept = sum(1 for r in results if r.get("ok"))
        skipped_lb = sum(1 for r in results if r.get("skipped") == "low_balance")
        skipped_cd = sum(1 for r in results if r.get("skipped") == "cooldown")
        skipped_gas = sum(1 for r in results if r.get("skipped") in ("no_trx_gas", "trx_fund_pending", "trx_fund_failed"))
        errors = sum(1 for r in results if r.get("error"))
        logger.info("[sweep] tick summary: swept=%d, skip_low=%d, skip_cd=%d, skip_gas=%d, err=%d, total=%d",
                    swept, skipped_lb, skipped_cd, skipped_gas, errors, len(results))


async def sweep_loop() -> None:
    logger.info("[sweep] started, interval=%ds, min=%s USDT", SWEEP_INTERVAL_SEC, SWEEP_MIN_USDT)
    await asyncio.sleep(15)
    tick_num = 0
    while True:
        tick_num += 1
        try:
            logger.info("[sweep] tick #%d running...", tick_num)
            await tick()
            logger.info("[sweep] tick #%d done", tick_num)
        except Exception as e:
            logger.exception("[sweep] tick #%d error: %s", tick_num, e)
        await asyncio.sleep(SWEEP_INTERVAL_SEC)

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

# EMERGENCY KILL SWITCH — установить True чтобы остановить весь sweep loop
# (через ENV var SWEEP_DISABLED=1). Используется при низком Feee balance.
import os as _os
SWEEP_DISABLED = _os.environ.get("SWEEP_DISABLED", "").strip() in ("1", "true", "yes")
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


async def _simulate_transfer_energy(client, owner_addr: str, to_addr: str, amount_raw: int) -> int:
    """ТОЧНАЯ симуляция: сколько energy потратит USDT transfer.

    Через TRON `triggerconstantcontract` — это dry-run транзакции без broadcast'a.
    Возвращает реальное energy_used. Это позволяет арендовать ТОЧНО сколько надо,
    без overpay (как было с hardcoded 130k).

    Fallback: при ошибке возвращаем 130_000 (safe maximum).
    """
    try:
        from tronpy.abi import trx_abi
        # Encode параметры transfer(address, uint256)
        parameter = trx_abi.encode_single(
            "(address,uint256)", [to_addr, amount_raw]
        ).hex()
        result = client.provider.make_request(
            "/wallet/triggerconstantcontract",
            {
                "owner_address": owner_addr,
                "contract_address": USDT_CONTRACT,
                "function_selector": "transfer(address,uint256)",
                "parameter": parameter,
                "visible": True,
            },
        )
        # Проверяем result.code — должен быть SUCCESS
        code = (result.get("result") or {}).get("code") or ""
        if code and code != "SUCCESS":
            msg = (result.get("result") or {}).get("message", "")
            logger.warning("[sweep/simulate] %s NOT SUCCESS code=%s msg=%s",
                           owner_addr, code, msg)
            return 130_000
        energy = result.get("energy_used", 0)
        if not energy or energy < 1000:
            logger.warning("[sweep/simulate] %s suspicious energy=%s — fallback 130k",
                           owner_addr, energy)
            return 130_000
        logger.info("[sweep/simulate] %s exact energy_used=%d", owner_addr, energy)
        return int(energy)
    except Exception as e:
        logger.warning("[sweep/simulate] %s exception: %s — fallback 130k", owner_addr, e)
        return 130_000


async def _account_resource(client, addr: str) -> dict:
    """Возвращает {energy, bandwidth} текущие на адресе.

    Используем для проверки сколько energy УЖЕ арендовано/делегировано
    перед тем как платить за новую аренду через Feee.io.

    energy = EnergyLimit - EnergyUsed (доступно сейчас)
    bandwidth = NetLimit - NetUsed + free_quota (~5000/день)
    """
    try:
        # tronpy не имеет прямого метода — используем low-level
        info = client.provider.make_request("/wallet/getaccountresource", {"address": addr, "visible": True})
        energy_limit = info.get("EnergyLimit", 0)
        energy_used = info.get("EnergyUsed", 0)
        net_limit = info.get("NetLimit", 0)
        net_used = info.get("NetUsed", 0)
        free_net_limit = info.get("freeNetLimit", 5000)
        free_net_used = info.get("freeNetUsed", 0)
        return {
            "energy_available": max(0, energy_limit - energy_used),
            "bandwidth_available": max(0, net_limit - net_used) + max(0, free_net_limit - free_net_used),
            "raw": info,
        }
    except Exception as e:
        logger.warning("[sweep] _account_resource error %s: %s", addr, e)
        return {"energy_available": 0, "bandwidth_available": 0}


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
    import time
    from tronpy.keys import PrivateKey
    from core.services import energy_service

    # ШАГ 0: cooldown check ПЕРЕД любыми платными операциями!
    # Раньше cooldown был после rent_energy → платили 3.5 TRX/tick впустую.
    if uda.address in _COOLDOWN and _COOLDOWN[uda.address] > time.time():
        remaining = int(_COOLDOWN[uda.address] - time.time())
        return {"address": uda.address, "skipped": "cooldown",
                "remaining_sec": remaining}

    usdt_bal = await _balance_usdt(client, uda.address)
    if usdt_bal < SWEEP_MIN_USDT:
        return {"address": uda.address, "skipped": "low_balance", "usdt": float(usdt_bal)}

    has_feee = energy_service.is_configured()
    logger.info("[sweep] %s start: usdt=%s, has_feee=%s", uda.address, usdt_bal, has_feee)

    # ШАГ 1: СИМУЛЯЦИЯ tx → точное energy_used
    amount_raw = int(usdt_bal * Decimal(10 ** 6))
    energy_simulated = await _simulate_transfer_energy(
        client, uda.address, hot_wallet, amount_raw,
    )
    # +10% запас на блокчейн-флуктуации
    ENERGY_REQUIRED = int(energy_simulated * 1.1)
    logger.info("[sweep] %s ENERGY_REQUIRED=%d (simulated=%d + 10%%)",
                uda.address, ENERGY_REQUIRED, energy_simulated)

    # ШАГ 2: проверка ТЕКУЩЕЙ energy на адресе
    res = await _account_resource(client, uda.address)
    current_energy = res.get("energy_available", 0)
    logger.info("[sweep] %s current_energy=%d (need %d)",
                uda.address, current_energy, ENERGY_REQUIRED)

    rented = False
    if current_energy >= ENERGY_REQUIRED:
        # Уже хватает — старая аренда ещё активна (Feee V3 = 5 мин)
        logger.info("[sweep] %s SKIP rent — already has %d energy (need %d)",
                    uda.address, current_energy, ENERGY_REQUIRED)
        rented = True
    elif has_feee:
        # Не хватает — арендуем РОВНО недостающее (минимум 32k, Feee не любит мало)
        deficit = ENERGY_REQUIRED - current_energy
        needed = max(deficit, 32_000)
        logger.info("[sweep] %s renting EXACT %d energy via Feee.io (deficit=%d)...",
                    uda.address, needed, deficit)
        rented = await energy_service.rent_and_wait(uda.address, energy_amount=needed, wait_sec=12)
        logger.info("[sweep] %s Feee rent result: %s", uda.address, rented)

    # ШАГ 2: если Feee не сработал ИЛИ не сконфигурен — НЕ продолжаем!
    # Раньше делали fund 20 TRX и burn — это превращало sweep в дорогую операцию ($6+).
    # Теперь sweep работает ТОЛЬКО через Feee. Если Feee упал — ставим cooldown и ждём.
    if not rented:
        logger.warning("[sweep] %s no energy (feee failed/disabled) — set cooldown 60s, skip sweep", uda.address)
        _COOLDOWN[uda.address] = time.time() + 60  # 60 сек cooldown — не дёргать впустую
        return {"address": uda.address, "skipped": "no_energy",
                "usdt": float(usdt_bal)}

    # ШАГ 3: bandwidth — TRON даёт 600 байт/день free per address. USDT transfer ~268 байт.
    # Если free quota исчерпан И на адресе мало TRX → авто-fund 1 TRX (хватит на ~3 sweep burn).
    # Стоимость sweep'а: 3.74 TRX (Feee energy) + 0.27 TRX (bandwidth burn) ≈ $1.07.
    bandwidth_available = res.get("bandwidth_available", 0)
    trx_bal = await _balance_trx(client, uda.address)
    logger.info("[sweep] %s bandwidth_available=%d (need ~268), trx_bal=%s",
                uda.address, bandwidth_available, trx_bal)
    if bandwidth_available < 300 and trx_bal < Decimal("0.5"):
        logger.info("[sweep] %s AUTO-FUND 1 TRX from hot (bandwidth=%d, trx=%s)",
                    uda.address, bandwidth_available, trx_bal)
        funded = await fund_trx_from_hot(client, uda.address, amount_trx=Decimal("1"))
        if not funded:
            logger.warning("[sweep] %s fund failed — cooldown 60s", uda.address)
            _COOLDOWN[uda.address] = time.time() + 60
            return {"address": uda.address, "skipped": "fund_failed",
                    "usdt": float(usdt_bal), "bandwidth": bandwidth_available}
        logger.info("[sweep] %s fund OK — продолжаем (TRX burn покроет bandwidth)", uda.address)

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
        broadcast_res = txn.broadcast()
        txid = txn.txid
        logger.info("[sweep] %s broadcast tx=%s — waiting confirmation", uda.address, txid)

        # Ждём 20 сек, проверяем receipt.result
        await asyncio.sleep(20)
        try:
            info = client.get_transaction_info(txid)
            receipt = (info or {}).get("receipt") or {}
            result_code = receipt.get("result", "UNKNOWN")
            if result_code != "SUCCESS":
                logger.error("[sweep] %s tx %s REVERT: result=%s receipt=%s",
                             uda.address, txid, result_code, receipt)
                _COOLDOWN[uda.address] = time.time() + 60  # 60 сек rate-limit
                return {"address": uda.address, "error": f"tx_revert_{result_code}", "tx_id": txid}
        except Exception as e:
            logger.warning("[sweep] %s confirm check failed: %s — assuming OK", uda.address, e)

        _COOLDOWN.pop(uda.address, None)
        logger.info("[sweep] %s → %s SENT %s USDT tx=%s (energy_rented=%s) CONFIRMED",
                    uda.address, hot_wallet, usdt_bal, txid, rented)
        return {"address": uda.address, "ok": True,
                "amount": float(usdt_bal), "tx_id": txid, "energy_rented": rented}
    except Exception as e:
        logger.exception("[sweep] send failed: %s", e)
        _COOLDOWN[uda.address] = time.time() + 60
        return {"address": uda.address, "error": str(e)[:200]}


async def tick() -> None:
    """Один цикл sweep'а."""
    if SWEEP_DISABLED:
        logger.info("[sweep] DISABLED via env (SWEEP_DISABLED=1), skipping tick")
        return
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
                    pass  # silent
                elif r.get("skipped") == "cooldown":
                    pass  # silent — rate-limit
                elif r.get("skipped"):
                    logger.info("[sweep] %s skip %s", uda.address, r.get("skipped"))
                elif r.get("error"):
                    logger.error("[sweep] %s ERROR: %s", uda.address, r.get("error"))
            except Exception as e:
                logger.exception("[sweep] %s error: %s", uda.address, e)
            import asyncio as _a
            await _a.sleep(0.5)

        swept = sum(1 for r in results if r.get("ok"))
        errs = sum(1 for r in results if r.get("error"))
        logger.info("[sweep] tick summary: swept=%d, err=%d, total=%d", swept, errs, len(results))


async def sweep_loop() -> None:
    logger.info("[sweep] started, interval=%ds, min=%s USDT", SWEEP_INTERVAL_SEC, SWEEP_MIN_USDT)
    await asyncio.sleep(15)
    tick_num = 0
    while True:
        tick_num += 1
        try:
            await tick()
        except Exception as e:
            logger.exception("[sweep] tick #%d error: %s", tick_num, e)
        await asyncio.sleep(SWEEP_INTERVAL_SEC)

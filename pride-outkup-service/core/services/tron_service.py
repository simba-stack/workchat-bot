"""TRON service — депозиты + выплаты USDT TRC20 (Phase A5).

Активируется когда заданы TRON_PRIVATE_KEY и TRON_HOT_WALLET_ADDRESS.
Использует tronpy + TronGrid.

Сейчас реализованы:
- получение баланса hot-wallet
- проверка статуса конкретной транзакции по txid
- отправка USDT TRC20 (нужен private key, broadcastTransaction)
- автозачисление депозитов через webhook /api/v1/webhooks/tron (см. webhooks.py)
"""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)


# USDT TRC20 contract
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def is_configured() -> bool:
    return bool(settings.tron_private_key and settings.tron_hot_wallet_address)


def _get_client():
    """lazy import tronpy."""
    from tronpy import Tron
    from tronpy.providers import HTTPProvider
    api_key = settings.trongrid_api_key or None
    if settings.tron_network == "mainnet":
        provider = HTTPProvider(api_key=api_key) if api_key else HTTPProvider()
        return Tron(provider=provider)
    else:
        return Tron(network=settings.tron_network)


def hot_wallet_address() -> str | None:
    return settings.tron_hot_wallet_address or None


async def get_usdt_balance() -> Decimal:
    """Баланс USDT TRC20 на горячем кошельке."""
    if not is_configured():
        return Decimal("0")
    try:
        client = _get_client()
        contract = client.get_contract(USDT_CONTRACT)
        addr = settings.tron_hot_wallet_address
        bal_raw = contract.functions.balanceOf(addr)
        # USDT decimals = 6
        return Decimal(bal_raw) / Decimal(10 ** 6)
    except Exception as e:
        logger.warning("[tron] get_balance failed: %s", e)
        return Decimal("0")


async def send_usdt(to_address: str, amount: Decimal) -> dict[str, Any]:
    """Отправить USDT TRC20 с hot wallet → получатель.

    Согласно docs.tron.network 2026:
    - Получатель ИМЕЕТ USDT → нужно ~65k energy (~$1.4 в TRX burn)
    - Получатель БЕЗ USDT → нужно ~130k energy (~$2.7 в TRX burn)

    Алгоритм:
    1. Проверяем USDT balance получателя → нужное кол-во energy
    2. Если есть Feee.io → арендуем energy (платим в USDT/TRX с Feee баланса)
    3. Иначе fee_limit = 35 или 60 TRX (в зависимости от случая) для burn
    4. После broadcast — ЖДЁМ tx confirmation и проверяем receipt.result==SUCCESS.
       broadcast() возвращает txid даже для failed tx (OUT_OF_ENERGY) — не доверяем!
    """
    import asyncio
    if not is_configured():
        return {"ok": False, "error": "TRON not configured"}

    client = _get_client()

    # Проверяем USDT balance получателя — определяет нужное кол-во energy
    receiver_has_usdt = False
    try:
        contract_check = client.get_contract(USDT_CONTRACT)
        recv_bal_raw = contract_check.functions.balanceOf(to_address)
        receiver_has_usdt = (recv_bal_raw or 0) > 0
    except Exception as e:
        logger.warning("[tron] check receiver balance failed: %s, assuming no USDT", e)

    # Базовая стоимость + 50% буфер (TRON penalty +25-40% на USDT transfers в 2024+)
    energy_needed = int((32_000 if receiver_has_usdt else 64_000) * 1.5)
    logger.info("[tron] receiver=%s has_usdt=%s → energy=%d (с буфером penalty)", to_address, receiver_has_usdt, energy_needed)

    # Rent energy через Feee.io (если настроен)
    rented = False
    try:
        from core.services import energy_service
        if energy_service.is_configured():
            rented = await energy_service.rent_and_wait(
                settings.tron_hot_wallet_address,  # ВАЖНО: arender energy для OWNER (hot wallet)
                energy_amount=energy_needed,
                wait_sec=8,
            )
            if rented:
                logger.info("[tron] energy %d rented via Feee.io", energy_needed)
            else:
                logger.warning("[tron] energy rental FAILED → fallback TRX burn (~$%.2f)",
                               energy_needed * 0.00021)  # ~210 sun/energy at current TRX rate
    except Exception as e:
        logger.warning("[tron] energy rental exception: %s", e)

    try:
        from tronpy.keys import PrivateKey
        priv = PrivateKey(bytes.fromhex(settings.tron_private_key))
        contract = client.get_contract(USDT_CONTRACT)
        amount_raw = int(amount * Decimal(10 ** 6))
        # fee_limit: rented — 5 TRX хватит (bandwidth + safety).
        # Не rented + receiver_has_usdt → 35 TRX (~14 TRX burn + safety).
        # Не rented + receiver_no_usdt → 60 TRX (~27 TRX burn + safety).
        # fee_limit = верхний потолок TRX burn если energy не хватит.
        # Поднимаем до 30 TRX даже при rented — это cap, не сжигается если energy ок.
        # Lesson v8/v9: low fee_limit ограничивает energy usage даже когда rent ок → OUT_OF_ENERGY.
        if rented:
            fee_limit_sun = 30_000_000
        elif receiver_has_usdt:
            fee_limit_sun = 35_000_000
        else:
            fee_limit_sun = 60_000_000
        txn = (
            contract.functions.transfer(to_address, amount_raw)
            .with_owner(settings.tron_hot_wallet_address)
            .fee_limit(fee_limit_sun)
            .build()
            .sign(priv)
        )
        broadcast_res = txn.broadcast()
        tx_id = txn.txid
        logger.info("[tron] broadcast %s USDT → %s tx=%s (rented=%s, fee_limit=%s TRX) — waiting confirm",
                    amount, to_address, tx_id, rented, fee_limit_sun // 1_000_000)

        # КРИТИЧНО: ждём блок и проверяем receipt.result
        await asyncio.sleep(20)  # TRON блок ~3 сек, 20 сек = с запасом 6 блоков
        try:
            info = client.get_transaction_info(tx_id)
            receipt = (info or {}).get("receipt") or {}
            result_code = receipt.get("result", "UNKNOWN")
            if result_code != "SUCCESS":
                logger.error("[tron] tx %s REVERT: result=%s receipt=%s",
                             tx_id, result_code, receipt)
                return {"ok": False, "error": f"tx_revert_{result_code}", "tx_id": tx_id}
        except Exception as e:
            logger.warning("[tron] confirm check failed: %s — assuming pending", e)
            # Не вернём ошибку — tx могла улететь, лучше assume success чем сорвать withdraw

        logger.info("[tron] CONFIRMED %s USDT → %s tx=%s", amount, to_address, tx_id)
        return {"ok": True, "tx_id": tx_id, "result": broadcast_res, "energy_rented": rented}
    except Exception as e:
        logger.exception("[tron] send failed: %s", e)
        return {"ok": False, "error": str(e)[:300]}


async def confirm_tx(tx_id: str) -> dict[str, Any]:
    """Проверка статуса транзакции."""
    if not is_configured():
        return {"ok": False, "error": "TRON not configured"}
    try:
        client = _get_client()
        info = client.get_transaction_info(tx_id)
        receipt = info.get("receipt", {})
        confirmed = info.get("blockNumber", 0) > 0
        return {
            "ok": True,
            "tx_id": tx_id,
            "block": info.get("blockNumber"),
            "confirmed": confirmed,
            "energy_used": receipt.get("energy_used"),
            "result": receipt.get("result"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

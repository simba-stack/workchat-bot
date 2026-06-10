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
    """Отправить USDT TRC20 → возвращает {'tx_id', 'status'}.

    Если настроен FEEE_API_KEY — сначала арендуем energy через Feee.io
    (экономия 90%+ на газе: ~$0.05 vs $1+ без energy).
    Если аренда не удалась — fallback на сжигание TRX (fee_limit=20 TRX).
    """
    if not is_configured():
        return {"ok": False, "error": "TRON not configured"}

    # Pre-step: rent energy через Feee.io (если настроен)
    rented = False
    try:
        from core.services import energy_service
        if energy_service.is_configured():
            rented = await energy_service.rent_and_wait(
                settings.tron_hot_wallet_address,
                energy_amount=65_000,
                wait_sec=8,
            )
            if rented:
                logger.info("[tron] energy rented via Feee.io for %s", to_address)
            else:
                logger.warning("[tron] energy rental failed, falling back to TRX burn")
    except Exception as e:
        logger.warning("[tron] energy rental skipped: %s", e)

    try:
        from tronpy.keys import PrivateKey
        client = _get_client()
        priv = PrivateKey(bytes.fromhex(settings.tron_private_key))
        contract = client.get_contract(USDT_CONTRACT)
        amount_raw = int(amount * Decimal(10 ** 6))
        # fee_limit: если energy не арендована — даём запас 30 TRX чтобы покрыть burn
        fee_limit_sun = 5_000_000 if rented else 30_000_000  # 5 TRX vs 30 TRX
        txn = (
            contract.functions.transfer(to_address, amount_raw)
            .with_owner(settings.tron_hot_wallet_address)
            .fee_limit(fee_limit_sun)
            .build()
            .sign(priv)
        )
        result = txn.broadcast()
        tx_id = txn.txid
        logger.info("[tron] send %s USDT to %s tx=%s (energy_rented=%s)",
                    amount, to_address, tx_id, rented)
        return {"ok": True, "tx_id": tx_id, "result": result, "energy_rented": rented}
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

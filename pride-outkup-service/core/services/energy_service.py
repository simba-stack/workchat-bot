"""Feee.io energy rental integration.

Покупает energy для USDT TRC20 transfer'ов вместо сжигания TRX.

Экономика:
- Без energy: USDT transfer = ~13-30 TRX сжигается (~$1-2)
- С energy rental: ~65k energy на 1ч = ~$0.05-0.15
- Экономия 90%+

Алгоритм:
1. Перед каждым USDT transfer (withdraw или sweep) → rent_energy(target_address, 65000, '1h')
2. Ждём ~5-10 сек пока energy зачислится
3. Отправляем USDT — газ покрывается арендованной energy
4. Energy уходит через 1ч (если не использована — пропадает)

Документация: https://feee.io
Base URL: https://api.feee.io/v1/
"""
from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FEEE_BASE_URL = "https://api.feee.io/v1"
DEFAULT_ENERGY_AMOUNT = 65_000  # хватает на 2 USDT transfer'а с запасом
DEFAULT_RENT_PERIOD = "1h"      # минимальный обычно 1h
MIN_USDT_TRANSFER_ENERGY = 32_000  # один USDT transfer требует ~32k energy


def is_configured() -> bool:
    """Доступна ли интеграция с Feee.io (стоит ли env var)."""
    return bool(os.environ.get("FEEE_API_KEY"))


def _client() -> httpx.AsyncClient:
    api_key = os.environ.get("FEEE_API_KEY", "")
    headers = {
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",  # некоторые сервисы используют bearer
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return httpx.AsyncClient(base_url=FEEE_BASE_URL, headers=headers, timeout=30.0)


async def get_balance() -> Optional[Decimal]:
    """Баланс TRX на Feee.io аккаунте (для аренды)."""
    if not is_configured():
        return None
    try:
        async with _client() as cli:
            r = await cli.get("/account/balance")
            r.raise_for_status()
            data = r.json()
            # Try common response shapes
            for key in ("balance", "trx_balance", "available_balance", "data"):
                v = data.get(key) if isinstance(data, dict) else None
                if isinstance(v, dict):
                    for k2 in ("balance", "trx", "available"):
                        if v.get(k2) is not None:
                            return Decimal(str(v.get(k2)))
                elif v is not None:
                    return Decimal(str(v))
    except Exception as e:
        logger.warning("[feee] get_balance error: %s", e)
    return None


async def get_energy_price() -> Optional[Decimal]:
    """Текущая цена energy в TRX за 1 единицу (или 1000 единиц)."""
    if not is_configured():
        return None
    try:
        async with _client() as cli:
            r = await cli.get("/energy/price")
            r.raise_for_status()
            data = r.json()
            for key in ("price", "trx_per_energy", "rate", "data"):
                v = data.get(key) if isinstance(data, dict) else None
                if isinstance(v, (int, float, str)):
                    return Decimal(str(v))
                if isinstance(v, dict):
                    for k2 in ("price", "rate"):
                        if v.get(k2) is not None:
                            return Decimal(str(v.get(k2)))
    except Exception as e:
        logger.warning("[feee] get_energy_price error: %s", e)
    return None


async def rent_energy(
    target_address: str,
    energy_amount: int = DEFAULT_ENERGY_AMOUNT,
    period: str = DEFAULT_RENT_PERIOD,
) -> dict:
    """Арендовать energy для target_address.

    Returns: {'ok': bool, 'order_id'?, 'tx_id'?, 'error'?}

    Не выкидывает исключения — graceful fallback при недоступности feee.io.
    Caller должен проверить result['ok'] и при False — отправлять с TRX-газом.
    """
    if not is_configured():
        return {"ok": False, "error": "feee_not_configured"}

    payload = {
        "receive_address": target_address,
        "energy_amount": energy_amount,
        "rent_duration": period,
        # Альтернативные имена параметров (у разных версий feee API):
        "address": target_address,
        "amount": energy_amount,
        "period": period,
    }

    try:
        async with _client() as cli:
            r = await cli.post("/order/create", json=payload)
            if r.status_code >= 400:
                logger.warning("[feee] rent_energy HTTP %d: %s", r.status_code, r.text[:200])
                return {"ok": False, "error": f"http_{r.status_code}", "detail": r.text[:200]}

            data = r.json()
            # Try various response shapes
            order_id = None
            tx_id = None
            if isinstance(data, dict):
                order_id = (data.get("order_id") or data.get("id")
                            or (data.get("data") or {}).get("order_id")
                            or (data.get("data") or {}).get("id"))
                tx_id = (data.get("tx_id") or data.get("txid")
                         or (data.get("data") or {}).get("tx_id"))

            if order_id:
                logger.info("[feee] rented %d energy for %s, order=%s",
                            energy_amount, target_address, order_id)
                return {"ok": True, "order_id": order_id, "tx_id": tx_id, "raw": data}
            else:
                logger.warning("[feee] rent_energy ambiguous response: %s", str(data)[:300])
                return {"ok": False, "error": "ambiguous_response", "raw": data}
    except Exception as e:
        logger.exception("[feee] rent_energy exception: %s", e)
        return {"ok": False, "error": str(e)[:200]}


async def rent_and_wait(
    target_address: str,
    energy_amount: int = DEFAULT_ENERGY_AMOUNT,
    wait_sec: int = 8,
) -> bool:
    """Арендовать energy и подождать ~8 сек чтобы блокчейн её зачислил.

    Возвращает True если аренда успешна, False иначе.
    Использовать перед tron_service.send_usdt() / sweep transfer.
    """
    if not is_configured():
        return False
    result = await rent_energy(target_address, energy_amount)
    if not result.get("ok"):
        return False
    # Даём время блокчейну подтвердить аренду
    await asyncio.sleep(wait_sec)
    return True

"""Feee.io energy rental integration (API v3).

Покупает energy для USDT TRC20 transfer'ов вместо сжигания TRX.

Экономика:
- Без energy: USDT transfer = ~13-30 TRX сжигается (~$1-2)
- С energy rental V3: 65k energy на 5 минут = ~$0.05-0.15
- Экономия 90%+

Алгоритм:
1. Перед каждым USDT transfer (withdraw или sweep) → rent_energy(target_address, 65000)
2. Ждём ~6-8 сек пока energy зачислится в блокчейн
3. Отправляем USDT — газ покрывается арендованной energy
4. Через 5 минут energy уходит (V3 фиксированно 5 мин — это даже хорошо: не платим за лишнее время)

API (v3):
- Base URL: https://feee.io/open
- Header: key: <api_key>
- POST /v3/order/create body: {resource_type: 1, receive_address, resource_value}
  resource_type: 0=bandwidth, 1=energy
  resource_value: integer (целое число energy)
- Response: {code: 0, msg: 'success', data: {pay_amount: 4.4, order_no: ...}}
- code == 0 — успех; иначе msg содержит ошибку

Документация: https://feee.io/doc/en-US/api/orderv3/create.html
"""
from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FEEE_BASE_URL = "https://feee.io/open"
FEEE_USER_AGENT = "PrideP2P-Bot/1.0"  # должен совпадать с UA whitelist в Feee.io
DEFAULT_ENERGY_AMOUNT = 65_000  # хватает на 2 USDT transfer'а с запасом
MIN_USDT_TRANSFER_ENERGY = 32_000  # один USDT transfer требует ~32k energy


def is_configured() -> bool:
    """Доступна ли интеграция с Feee.io (стоит ли env var)."""
    return bool(os.environ.get("FEEE_API_KEY"))


def _client() -> httpx.AsyncClient:
    api_key = os.environ.get("FEEE_API_KEY", "")
    headers = {
        "key": api_key,  # Feee.io использует header 'key', НЕ Authorization/X-API-Key
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": FEEE_USER_AGENT,
    }
    return httpx.AsyncClient(base_url=FEEE_BASE_URL, headers=headers, timeout=30.0)


async def rent_energy(
    target_address: str,
    energy_amount: int = DEFAULT_ENERGY_AMOUNT,
    period: str = "5m",  # V3 фиксированно 5 минут, параметр игнорируется
) -> dict:
    """Арендовать energy для target_address через Feee.io API v3.

    Returns: {'ok': bool, 'order_no'?, 'pay_amount'?, 'error'?}

    Не выкидывает исключения — graceful fallback при недоступности feee.io.
    Caller должен проверить result['ok'] и при False — отправлять с TRX-газом.
    """
    if not is_configured():
        return {"ok": False, "error": "feee_not_configured"}

    payload = {
        "resource_type": 1,  # 1 = energy, 0 = bandwidth
        "receive_address": target_address,
        "resource_value": int(energy_amount),
    }

    try:
        async with _client() as cli:
            r = await cli.post("/v3/order/create", json=payload)
            if r.status_code >= 500:
                logger.warning("[feee] rent_energy HTTP %d: %s", r.status_code, r.text[:200])
                return {"ok": False, "error": f"http_{r.status_code}", "detail": r.text[:200]}

            try:
                data = r.json()
            except Exception:
                return {"ok": False, "error": "non_json_response", "detail": r.text[:200]}

            code = data.get("code")
            if code != 0:
                msg = data.get("msg") or "unknown_error"
                logger.warning("[feee] rent_energy code=%s msg=%s", code, msg)
                return {"ok": False, "error": msg, "code": code, "raw": data}

            order = data.get("data") or {}
            order_no = order.get("order_no")
            pay_amount = order.get("pay_amount")
            logger.info("[feee] rented %d energy for %s, order=%s, paid=%s TRX",
                        energy_amount, target_address, order_no, pay_amount)
            return {
                "ok": True,
                "order_no": order_no,
                "pay_amount": pay_amount,
                "raw": data,
            }
    except httpx.TimeoutException:
        logger.warning("[feee] rent_energy timeout")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        logger.exception("[feee] rent_energy exception: %s", e)
        return {"ok": False, "error": str(e)[:200]}


async def rent_and_wait(
    target_address: str,
    energy_amount: int = DEFAULT_ENERGY_AMOUNT,
    wait_sec: int = 7,
) -> bool:
    """Арендовать energy и подождать ~7 сек чтобы блокчейн её зачислил.

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


async def get_balance() -> Optional[Decimal]:
    """Баланс TRX на Feee.io аккаунте.

    Endpoint берётся из docs; если 404 — возвращаем None.
    """
    if not is_configured():
        return None
    # Возможные endpoints в Feee.io — попробуем оба
    for path in ("/v3/account", "/v3/user/info", "/v2/account/balance"):
        try:
            async with _client() as cli:
                r = await cli.get(path)
                if r.status_code == 404:
                    continue
                data = r.json()
                if data.get("code") != 0:
                    continue
                d = data.get("data") or {}
                bal = d.get("trx_balance") or d.get("balance") or d.get("available_balance")
                if bal is not None:
                    return Decimal(str(bal))
        except Exception:
            continue
    return None


async def get_energy_price() -> Optional[Decimal]:
    """Текущая цена energy. Возвращается в Sun/day или Sun/unit (зависит от API)."""
    if not is_configured():
        return None
    for path in ("/v3/market/price", "/v3/price", "/v2/energy/price"):
        try:
            async with _client() as cli:
                r = await cli.get(path)
                if r.status_code == 404:
                    continue
                data = r.json()
                if data.get("code") != 0:
                    continue
                d = data.get("data") or {}
                price = d.get("price") or d.get("price_in_sun") or d.get("rate")
                if price is not None:
                    return Decimal(str(price))
        except Exception:
            continue
    return None

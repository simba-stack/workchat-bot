"""FixedFloat API integration — реальный кросс-чейн обмен крипты.

FixedFloat — централизованный обменник который позволяет менять USDT-TRC20 на
TON, BTC, ETH и т.д. с реальной доставкой на адрес.

Используется в pride-p2p для:
1. Swap rate quote — реальный курс с учётом ликвидности FF
2. (опционально) Real swap — backend создаёт order на FixedFloat и
   шлёт юзеру в указанную сеть. Пока используем виртуальный swap по rate FF.

Env:
- FIXEDFLOAT_API_KEY  (X-API-KEY header)
- FIXEDFLOAT_API_SECRET (для signature HMAC-SHA256)

Документация: https://fixedfloat.com/api
Base URL: https://ff.io/api/v2/

Без API ключа возвращаем None — caller использует fallback (CoinGecko rates).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FF_BASE = "https://ff.io/api/v2"

# Маппинг наших code → FixedFloat code+network
# (FF использует currency code + network отдельно: USDTTRC, USDT (ERC), TON, BTC, ETH...)
FF_CURRENCY_MAP = {
    "USDT": "USDTTRC",   # USDT TRC20 (default network у нас)
    "USDC": "USDCTRC",
    "TON":  "TON",
    "TRX":  "TRX",
    "BTC":  "BTC",
    "ETH":  "ETH",
    "SOL":  "SOL",
    "BNB":  "BNB",
    "DOGE": "DOGE",
    "LTC":  "LTC",
}


def is_configured() -> bool:
    return bool(os.environ.get("FIXEDFLOAT_API_KEY") and os.environ.get("FIXEDFLOAT_API_SECRET"))


def _sign(payload: str) -> str:
    secret = os.environ.get("FIXEDFLOAT_API_SECRET", "").encode()
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def _headers(payload: str) -> dict:
    return {
        "X-API-KEY": os.environ.get("FIXEDFLOAT_API_KEY", ""),
        "X-API-SIGN": _sign(payload),
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
    }


async def get_rate(from_coin: str, to_coin: str, amount: Decimal) -> Optional[dict]:
    """Возвращает {rate, to_amount, network, fee} или None если FF недоступен.

    amount — это сумма from_coin которую хочет обменять юзер.
    """
    if not is_configured():
        return None
    fc = FF_CURRENCY_MAP.get(from_coin.upper())
    tc = FF_CURRENCY_MAP.get(to_coin.upper())
    if not fc or not tc:
        return None

    body = {
        "type": "float",   # float = 0.5% fee (vs fixed=1%) — наш профит х2
        "fromCcy": fc, "toCcy": tc,
        "direction": "from", "amount": float(amount),
    }
    payload = json.dumps(body, separators=(",", ":"))

    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(f"{FF_BASE}/price", content=payload, headers=_headers(payload))
            if r.status_code != 200:
                logger.warning("[ff] /price http=%d: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            if data.get("code") != 0:
                logger.warning("[ff] /price code=%s msg=%s", data.get("code"), data.get("msg"))
                return None
            d = data.get("data") or {}
            return {
                "rate": float(d.get("to", {}).get("rate") or 0),
                "to_amount": float(d.get("to", {}).get("amount") or 0),
                "min": float(d.get("from", {}).get("min") or 0),
                "max": float(d.get("from", {}).get("max") or 0),
                "fee": float(d.get("fee") or 0),
                "raw": data,
            }
    except Exception as e:
        logger.warning("[ff] get_rate exception: %s", e)
        return None


async def create_order(
    from_coin: str, to_coin: str, amount: Decimal, to_address: str,
) -> Optional[dict]:
    """Реальный кросс-чейн обмен через FixedFloat.

    Создаёт order: юзер шлёт from_coin на FF-адрес, FF делает обмен и
    отправляет to_coin на to_address.

    Returns: {order_id, ff_address, expires_at} или None.
    """
    if not is_configured():
        return None
    fc = FF_CURRENCY_MAP.get(from_coin.upper())
    tc = FF_CURRENCY_MAP.get(to_coin.upper())
    if not fc or not tc:
        return None

    body = {
        "type": "fixed",
        "fromCcy": fc, "toCcy": tc,
        "direction": "from", "amount": float(amount),
        "toAddress": to_address,
    }
    payload = json.dumps(body, separators=(",", ":"))

    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(f"{FF_BASE}/create", content=payload, headers=_headers(payload))
            if r.status_code != 200:
                logger.warning("[ff] /create http=%d: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            if data.get("code") != 0:
                logger.warning("[ff] /create code=%s msg=%s", data.get("code"), data.get("msg"))
                return None
            d = data.get("data") or {}
            return {
                "order_id": d.get("id"),
                "ff_address": (d.get("from") or {}).get("address"),
                "expires_at": d.get("time", {}).get("expiration"),
                "raw": data,
            }
    except Exception as e:
        logger.exception("[ff] create_order exception: %s", e)
        return None


async def get_order_status(order_id: str) -> Optional[dict]:
    """Проверить статус FixedFloat order."""
    if not is_configured() or not order_id:
        return None
    body = {"id": order_id, "token": ""}
    payload = json.dumps(body, separators=(",", ":"))
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(f"{FF_BASE}/order", content=payload, headers=_headers(payload))
            if r.status_code != 200:
                return None
            return r.json().get("data")
    except Exception:
        return None

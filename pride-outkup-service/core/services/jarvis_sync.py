"""Двусторонняя синхронизация с PRIDE JARVIS (workchat-bot).

- Outgoing webhook'и: outkup → JARVIS (события orders/deals/kyc/disputes)
- Periodic pull: тянем курс/комиссии из JARVIS
- Incoming webhook handler — в api/routers/webhooks.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


def _sign(body: bytes) -> str:
    return hmac.new(
        settings.jarvis_hmac_secret.encode(), body, hashlib.sha256,
    ).hexdigest()


async def send_event(event: str, data: dict[str, Any], *, retries: int = 3) -> bool:
    """Шлём webhook в JARVIS. Retry с exp backoff. True если доставлено."""
    if not settings.jarvis_webhook_url or not settings.jarvis_hmac_secret:
        logger.warning("JARVIS webhook не настроен — пропускаем event=%s", event)
        return False

    payload = {
        "event": event,
        "version": 1,
        "timestamp": int(time.time()),
        "data": data,
    }
    body = json.dumps(payload).encode()
    sig = _sign(body)

    headers = {
        "Content-Type": "application/json",
        "X-Outkup-Signature": sig,
        "X-Outkup-Timestamp": str(payload["timestamp"]),
        "X-Outkup-Event": event,
    }

    delay = 1
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(settings.jarvis_webhook_url, content=body, headers=headers)
            if 200 <= r.status_code < 300:
                logger.info("[jarvis] event=%s delivered (attempt=%d)", event, attempt)
                return True
            logger.warning("[jarvis] event=%s status=%d body=%s", event, r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("[jarvis] event=%s attempt=%d failed: %s", event, attempt, e)
        await asyncio.sleep(delay)
        delay = min(delay * 3, 60)

    logger.error("[jarvis] event=%s FAILED после %d попыток", event, retries)
    return False


async def pull_rate_from_jarvis() -> dict[str, Any] | None:
    """Тянет актуальный курс/комиссию из JARVIS settings.

    JARVIS endpoint: GET /api/settings/outkup → возвращает {rate_rub_per_usdt, pct_fee, ...}
    """
    if not settings.jarvis_api_token:
        return None
    url = f"{settings.jarvis_base_url.rstrip('/')}/api/settings/outkup"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {settings.jarvis_api_token}"},
            )
        if r.status_code != 200:
            logger.warning("[jarvis pull rate] status=%d", r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.warning("[jarvis pull rate] error: %s", e)
        return None


async def rate_sync_loop():
    """Background task — каждые 30 сек обновляем курс из JARVIS."""
    from core.db import AsyncSessionLocal
    from core.services import settings_kv

    while True:
        try:
            data = await pull_rate_from_jarvis()
            if data:
                rate = float(data.get("rate_rub_per_usdt") or 0)
                fee = float(data.get("pct_fee") or 0)
                if rate > 0:
                    async with AsyncSessionLocal() as db:
                        # Buy rate = тот что в JARVIS
                        await settings_kv.set_setting(db, "rate_buy_usdt", rate)
                        # Sell rate = buy − комиссия×2 (наша двойная маржа)
                        sell = round(rate * (1 - fee / 100), 2)
                        await settings_kv.set_setting(db, "rate_sell_usdt", sell)
                        if fee > 0:
                            await settings_kv.set_setting(db, "pct_fee_v1", fee)
                        await db.commit()
                    logger.info("[jarvis sync] rate buy=%.2f sell=%.2f fee=%.2f%%", rate, sell, fee)
        except Exception as e:
            logger.exception("[jarvis sync] tick error: %s", e)
        await asyncio.sleep(30)

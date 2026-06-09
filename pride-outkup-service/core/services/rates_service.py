"""Rates service — пуллит курсы криптовалют из CoinGecko, кеширует в kv_settings.

CoinGecko free API: 30 req/min без ключа. Пуллим раз в 60 сек — норм.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import httpx
from sqlalchemy import select

from core.db import AsyncSessionLocal
from core.models import Coin
from core.services import settings_kv

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 60
CG_URL = "https://api.coingecko.com/api/v3/simple/price"


async def _fetch_rates(coingecko_ids: list[str]) -> dict[str, dict]:
    if not coingecko_ids:
        return {}
    params = {
        "ids": ",".join(coingecko_ids),
        "vs_currencies": "usd,rub",
        "include_24hr_change": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(CG_URL, params=params)
        if r.status_code != 200:
            logger.warning("[rates] CoinGecko %d: %s", r.status_code, r.text[:200])
            return {}
        return r.json() or {}
    except Exception as e:
        logger.warning("[rates] fetch error: %s", e)
        return {}


async def tick() -> None:
    """Один цикл — тянем курсы всех активных coin'ов с coingecko_id."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Coin).where(Coin.is_active.is_(True), Coin.coingecko_id.is_not(None))
        )
        coins = res.scalars().all()
        if not coins:
            return
        cg_ids = [c.coingecko_id for c in coins if c.coingecko_id]
        rates = await _fetch_rates(cg_ids)
        if not rates:
            return
        # Складываем в kv_settings одним блобом
        out: dict[str, dict] = {}
        for c in coins:
            data = rates.get(c.coingecko_id) if c.coingecko_id else None
            if not data:
                continue
            out[c.code] = {
                "usd": float(data.get("usd") or 0),
                "rub": float(data.get("rub") or 0),
                "change_24h": float(data.get("usd_24h_change") or 0),
            }
        if out:
            await settings_kv.set_setting(db, "crypto_rates", out)
            await db.commit()
            logger.info("[rates] updated %d coins", len(out))


async def get_rates() -> dict[str, dict]:
    async with AsyncSessionLocal() as db:
        return await settings_kv.get_setting(db, "crypto_rates", {}) or {}


async def rate_loop() -> None:
    logger.info("[rates] started, polling every %ds", POLL_INTERVAL_SEC)
    # первый pull сразу
    try:
        await tick()
    except Exception as e:
        logger.exception("[rates] initial tick error: %s", e)
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            await tick()
        except Exception as e:
            logger.exception("[rates] tick error: %s", e)

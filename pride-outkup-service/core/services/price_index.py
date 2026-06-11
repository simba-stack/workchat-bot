"""PriceIndex service — чтение/запись рыночных курсов coin/fiat.

Refresher живёт в `rates_service.tick()` (уже пуллит CoinGecko каждые 60с).
Этот модуль:
  - upsert текущего индекса в таблицу `price_indices`
  - чтение get_index(coin, fiat)
  - проверка within_band(price, coin, fiat) — для валидации офферов

Цена в `price_indices` хранится в виде number — например USDT/RUB = 95.50.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import PriceIndex
from core.services import settings_kv

logger = logging.getLogger(__name__)

# Default fiats currently supported in P2P
SUPPORTED_FIATS = ("RUB", "USD", "EUR")
# Default coins (must match CoinGecko ids in Coin table)
SUPPORTED_COINS = ("USDT", "USDC", "TON", "BTC", "ETH", "TRX")

DEFAULT_PRICE_BAND_PCT = Decimal("15")  # ±15%


async def upsert(db: AsyncSession, coin: str, fiat: str, price: Decimal, source: str = "coingecko") -> None:
    """Создать или обновить индекс для пары."""
    coin = coin.upper()
    fiat = fiat.upper()
    if price <= 0:
        return
    row = (
        await db.execute(
            select(PriceIndex).where(PriceIndex.coin == coin, PriceIndex.fiat == fiat)
        )
    ).scalar_one_or_none()
    if row:
        row.price = price
        row.source = source
    else:
        db.add(PriceIndex(coin=coin, fiat=fiat, price=price, source=source))


async def get_index(db: AsyncSession, coin: str, fiat: str) -> Optional[Decimal]:
    """Получить текущий индекс. None если пара не отслеживается."""
    coin = coin.upper()
    fiat = fiat.upper()
    row = (
        await db.execute(
            select(PriceIndex).where(PriceIndex.coin == coin, PriceIndex.fiat == fiat)
        )
    ).scalar_one_or_none()
    return row.price if row else None


async def get_band_pct(db: AsyncSession) -> Decimal:
    """Допустимое отклонение цены оффера от индекса (в процентах)."""
    raw = await settings_kv.get_setting(db, "p2p_price_band_pct", None)
    try:
        return Decimal(str(raw)) if raw is not None else DEFAULT_PRICE_BAND_PCT
    except Exception:
        return DEFAULT_PRICE_BAND_PCT


async def within_band(db: AsyncSession, price: Decimal, coin: str, fiat: str) -> tuple[bool, Optional[Decimal], Decimal]:
    """Проверка price-band.

    Returns (ok, index_price, band_pct).
    Если индекс неизвестен — возвращаем (True, None, band) (не блокируем).
    """
    band = await get_band_pct(db)
    idx = await get_index(db, coin, fiat)
    if idx is None or idx <= 0:
        return True, None, band
    diff_pct = abs(price - idx) / idx * Decimal("100")
    return (diff_pct <= band), idx, band


async def compute_float_price(db: AsyncSession, coin: str, fiat: str, margin_pct: Decimal) -> Optional[Decimal]:
    """Считает фактическую цену float-оффера: index * margin / 100.

    None если индекс отсутствует.
    """
    idx = await get_index(db, coin, fiat)
    if idx is None or idx <= 0:
        return None
    return (idx * margin_pct / Decimal("100")).quantize(Decimal("0.01"))

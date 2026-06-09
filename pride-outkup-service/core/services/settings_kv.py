"""KV-settings storage — курс, комиссии, feature flags.

Хранит JSON значения по ключам в таблице kv_settings.
Может быть обновлено из JARVIS через webhook.
"""
import json
import logging
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def get_setting(db: AsyncSession, key: str, default: Any = None) -> Any:
    res = await db.execute(text("SELECT value FROM kv_settings WHERE key = :k"), {"k": key})
    row = res.first()
    if not row:
        return default
    val = row[0]
    if isinstance(val, (str, bytes)):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val
    return val


async def set_setting(db: AsyncSession, key: str, value: Any) -> None:
    """Upsert."""
    payload = json.dumps(value) if not isinstance(value, str) else value
    await db.execute(
        text("""
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES (:k, :v::jsonb, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value = :v::jsonb, updated_at = NOW()
        """),
        {"k": key, "v": payload},
    )


async def get_rate_buy(db: AsyncSession) -> Decimal:
    v = await get_setting(db, "rate_buy_usdt", 84.0)
    return Decimal(str(v))


async def get_rate_sell(db: AsyncSession) -> Decimal:
    v = await get_setting(db, "rate_sell_usdt", 82.0)
    return Decimal(str(v))


async def get_fee_v1_pct(db: AsyncSession) -> Decimal:
    v = await get_setting(db, "pct_fee_v1", 3.5)
    return Decimal(str(v))


async def get_fee_v2_pct(db: AsyncSession) -> Decimal:
    v = await get_setting(db, "pct_fee_v2", 0.3)
    return Decimal(str(v))


async def is_v2_p2p_public(db: AsyncSession) -> bool:
    v = await get_setting(db, "feature_v2_p2p_public", False)
    return bool(v)

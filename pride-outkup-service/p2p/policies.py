"""Policy Engine (ТЗ Том 14).

Все лимиты, таймеры, бизнес-правила хранятся в БД (p2p_policies).
В коде только дефолты — на случай если запись в БД отсутствует.

Используется кеш в памяти со временем жизни 60 сек.
После изменения политики admin вызывает reload() — кеш сбрасывается.
"""
from __future__ import annotations
import logging
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from p2p.models import P2PPolicy

logger = logging.getLogger("p2p.policies")

# Дефолтные значения (если нет в БД)
_DEFAULTS: dict[str, Any] = {
    # Timers
    "TRADE_PAYMENT_TIMEOUT_MIN": 15,        # на оплату
    "TRADE_CONFIRM_TIMEOUT_MIN": 20,        # на подтверждение продавцом
    "DISPUTE_SLA_HOURS": 24,                # SLA арбитра
    # Trade limits
    "MIN_TRADE_AMOUNT_USDT": "10",
    "MAX_TRADE_AMOUNT_USDT": "50000",
    "MAX_ACTIVE_TRADES_PER_USER": 20,
    # Advertisement limits
    "MAX_ACTIVE_ADVERTISEMENTS": 100,
    "AUTO_PAUSE_EMPTY_BALANCE": True,
    "AUTO_RESUME_ON_BALANCE": True,
    # Pricing
    "MAX_PRICE_DEVIATION_PCT": 15,          # ±15% от рыночного индекса
    "ALLOW_FLOATING_PRICE": True,
    # Fees
    "PLATFORM_FEE_PCT": "0.5",              # 0.5% с buyer
    # Roles & access
    "FEATURE_P2P_PUBLIC": True,
    "REQUIRE_KYC_FOR_SELL": False,
    "REQUIRE_KYC_FOR_BUY": False,
    # Risk
    "MAX_FAILED_LOGINS": 10,
    "MAX_CONCURRENT_SESSIONS": 10,
    # Chat
    "CHAT_MAX_MESSAGE_LENGTH": 2000,
    "CHAT_MAX_ATTACHMENT_MB": 25,
    # Rate limits (per minute)
    "RL_CREATE_ADVERTISEMENT": 5,
    "RL_CREATE_TRADE": 10,
    "RL_PAYMENT_MARKED": 3,
    "RL_PAYMENT_CONFIRM": 3,
    "RL_OPEN_DISPUTE": 3,
    "RL_CHAT_MESSAGE": 30,
    # Idempotency TTL
    "IDEMPOTENCY_TTL_HOURS": 24,
    # Allowed currencies
    "ALLOWED_CRYPTO": ["USDT"],
    "ALLOWED_FIAT": ["RUB", "USD", "EUR", "PLN", "UAH", "TRY", "KZT"],
}


class _Cache:
    data: dict[str, Any] = {}
    loaded_at: float = 0.0
    ttl: float = 60.0  # seconds


_cache = _Cache()


def _coerce(default: Any, raw: Any) -> Any:
    """Привести значение из БД к типу дефолта."""
    if isinstance(default, bool):
        return bool(raw)
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        if default.replace(".", "", 1).isdigit() or default.lstrip("-").replace(".", "", 1).isdigit():
            # Это число-строка (Decimal)
            return str(raw)
        return str(raw)
    if isinstance(default, list):
        return list(raw) if isinstance(raw, list) else default
    return raw


async def _reload_cache(db: AsyncSession) -> None:
    """Загрузить все активные политики из БД в кеш."""
    r = await db.execute(
        select(P2PPolicy).where(P2PPolicy.is_active == True)  # noqa: E712
    )
    fresh: dict[str, Any] = dict(_DEFAULTS)
    for p in r.scalars().all():
        if p.policy_key in _DEFAULTS:
            try:
                val = (p.value or {}).get("value", p.value)
                fresh[p.policy_key] = _coerce(_DEFAULTS[p.policy_key], val)
            except Exception:
                pass
        else:
            fresh[p.policy_key] = (p.value or {}).get("value")
    _cache.data = fresh
    _cache.loaded_at = time.time()
    logger.info("[policy] cache reloaded: %d keys", len(fresh))


def reload_cache() -> None:
    """Сбросить кеш — следующий get() прочитает из БД."""
    _cache.loaded_at = 0


async def get_policy(db: AsyncSession, key: str, default: Any = None) -> Any:
    """Получить значение политики. Если нет — вернуть default или дефолт из _DEFAULTS."""
    if time.time() - _cache.loaded_at > _cache.ttl:
        try:
            await _reload_cache(db)
        except Exception as e:
            logger.warning("[policy] reload failed: %s — using defaults", e)
            _cache.data = dict(_DEFAULTS)
            _cache.loaded_at = time.time()
    if key in _cache.data:
        return _cache.data[key]
    if default is not None:
        return default
    return _DEFAULTS.get(key)


async def get_decimal(db: AsyncSession, key: str) -> Decimal:
    """Утилита для денежных политик."""
    return Decimal(str(await get_policy(db, key)))


async def get_int(db: AsyncSession, key: str) -> int:
    return int(await get_policy(db, key))


async def get_bool(db: AsyncSession, key: str) -> bool:
    return bool(await get_policy(db, key))

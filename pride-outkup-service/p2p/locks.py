"""Lock Manager — pessimistic locks через PostgreSQL.

Используется два механизма:
1. Advisory locks (pg_advisory_xact_lock) — для логических объектов
2. SELECT ... FOR UPDATE — для конкретных строк таблиц

Все локи автоматически освобождаются при COMMIT/ROLLBACK транзакции.
"""
from __future__ import annotations
import hashlib
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("p2p.locks")


def _key_to_bigint(key: str) -> int:
    """Конвертит произвольную строку в bigint для pg_advisory_lock.

    SHA1 → first 8 bytes → signed bigint.
    """
    h = hashlib.sha1(key.encode()).digest()
    val = int.from_bytes(h[:8], byteorder="big", signed=True)
    return val


async def advisory_lock(db: AsyncSession, key: str) -> None:
    """Берём advisory lock на ключ. Освободится при COMMIT/ROLLBACK.

    Несколько вызовов в одной транзакции — без блокировки (reentrant).
    """
    big = _key_to_bigint(key)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": big})


async def try_advisory_lock(db: AsyncSession, key: str) -> bool:
    """Non-blocking advisory lock. True если взяли, False если занят."""
    big = _key_to_bigint(key)
    r = await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": big})
    return bool(r.scalar())


async def lock_user_wallet(db: AsyncSession, user_id: int, currency: str = "USDT") -> None:
    """Лок на конкретный wallet юзера."""
    await advisory_lock(db, f"wallet:{user_id}:{currency}")


async def lock_advertisement(db: AsyncSession, advertisement_id: str) -> None:
    """Лок на объявление (для concurrent trade creation)."""
    await advisory_lock(db, f"ad:{advertisement_id}")


async def lock_trade(db: AsyncSession, trade_id: str) -> None:
    """Лок на сделку (для transitions / payment confirmation)."""
    await advisory_lock(db, f"trade:{trade_id}")


async def lock_dispute(db: AsyncSession, dispute_id: str) -> None:
    """Лок на спор (для arbitrator decision)."""
    await advisory_lock(db, f"dispute:{dispute_id}")


async def lock_workflow(db: AsyncSession, workflow_id: str) -> None:
    """Лок на workflow (для recovery после сбоя)."""
    await advisory_lock(db, f"wf:{workflow_id}")

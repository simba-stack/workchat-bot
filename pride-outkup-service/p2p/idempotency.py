"""Idempotency Key helper (ТЗ Том 19 §10).

Используется на всех POST/PATCH endpoint'ах чтобы повторный запрос с тем же ключом
вернул уже сохранённый результат без повторной обработки.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from p2p import locks
from p2p.models import P2PIdempotencyKey

logger = logging.getLogger("p2p.idempotency")


class IdempotencyHit(NamedTuple):
    status: int
    body: dict


async def check(
    db: AsyncSession,
    *,
    user_id: int | None,
    endpoint: str,
    key: str,
) -> IdempotencyHit | None:
    """Проверить был ли уже обработан этот key. Возвращает результат или None."""
    if not key:
        return None
    r = await db.execute(
        select(P2PIdempotencyKey).where(
            P2PIdempotencyKey.user_id == user_id,
            P2PIdempotencyKey.endpoint == endpoint,
            P2PIdempotencyKey.key == key,
        )
    )
    row = r.scalar_one_or_none()
    if row is None:
        return None
    # Проверяем срок жизни
    if row.expires_at <= datetime.now(timezone.utc):
        return None
    logger.debug("[idempotency] HIT endpoint=%s key=%s status=%s",
                 endpoint, key, row.response_status)
    return IdempotencyHit(status=row.response_status, body=row.response_body or {})


async def save(
    db: AsyncSession,
    *,
    user_id: int | None,
    endpoint: str,
    key: str,
    status: int,
    body: dict,
    workflow_id: str | None = None,
    ttl_hours: int = 24,
) -> None:
    """Сохранить результат запроса в idempotency table."""
    if not key:
        return
    # advisory lock на (endpoint, key) — защита от race при первом сохранении
    await locks.advisory_lock(db, f"idemp:{endpoint}:{key}")
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    # Если уже есть — обновлять не нужно (повтор был ранее)
    existing = await db.execute(
        select(P2PIdempotencyKey).where(
            P2PIdempotencyKey.user_id == user_id,
            P2PIdempotencyKey.endpoint == endpoint,
            P2PIdempotencyKey.key == key,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    rec = P2PIdempotencyKey(
        user_id=user_id,
        endpoint=endpoint,
        key=key,
        response_status=status,
        response_body=body,
        workflow_id=workflow_id,
        expires_at=expires,
    )
    db.add(rec)
    await db.flush()


async def cleanup_expired(db: AsyncSession, batch: int = 1000) -> int:
    """Удалить просроченные ключи (фон-задача)."""
    now = datetime.now(timezone.utc)
    r = await db.execute(
        delete(P2PIdempotencyKey).where(P2PIdempotencyKey.expires_at < now)
    )
    return r.rowcount or 0

"""Outbox Pattern (ТЗ Том 19).

Принцип: вместе с business data в одной транзакции пишется запись в p2p_outbox.
После COMMIT отдельный Publisher worker читает PENDING события и публикует:
  - WebSocket broadcast
  - Telegram Bot notify
  - Notification create
  - Analytics

Если Publisher не справился — retry с exponential backoff.
После N попыток — DEAD_LETTER (требует ручного разбора).
"""
from __future__ import annotations
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from p2p.enums import OutboxStatus
from p2p.models import P2POutbox

logger = logging.getLogger("p2p.outbox")


async def emit(
    db: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    *,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    correlation_id: str | None = None,
    workflow_id: str | None = None,
) -> str:
    """Создать запись в outbox в текущей транзакции.

    После commit транзакции Publisher worker подхватит её.
    Возвращает event_id.
    """
    event_id = str(uuid.uuid4())
    row = P2POutbox(
        event_id=event_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        status=OutboxStatus.PENDING.value,
        correlation_id=correlation_id,
        workflow_id=workflow_id,
    )
    db.add(row)
    await db.flush()
    logger.debug("[outbox] emit %s id=%s aggregate=%s/%s",
                 event_type, event_id, aggregate_type, aggregate_id)
    return event_id


async def claim_pending(
    db: AsyncSession,
    limit: int = 50,
) -> list[P2POutbox]:
    """Atomic claim — пометить PENDING как PROCESSING и вернуть для обработки.

    Использует SELECT ... FOR UPDATE SKIP LOCKED — несколько worker'ов работают
    параллельно без блокировок.
    """
    from sqlalchemy import update
    from datetime import datetime, timezone
    # Берём PENDING с next_retry_at <= now (или NULL)
    now = datetime.now(timezone.utc)
    res = await db.execute(
        select(P2POutbox)
        .where(
            P2POutbox.status == OutboxStatus.PENDING.value,
            (P2POutbox.next_retry_at.is_(None) | (P2POutbox.next_retry_at <= now)),
        )
        .order_by(P2POutbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(res.scalars().all())
    if not rows:
        return []
    # Помечаем PROCESSING
    ids = [r.id for r in rows]
    await db.execute(
        update(P2POutbox).where(P2POutbox.id.in_(ids)).values(status=OutboxStatus.PROCESSING.value)
    )
    await db.flush()
    return rows


async def mark_published(db: AsyncSession, event: P2POutbox) -> None:
    from datetime import datetime, timezone
    event.status = OutboxStatus.PUBLISHED.value
    event.published_at = datetime.now(timezone.utc)
    event.last_error = None
    await db.flush()


async def mark_failed(
    db: AsyncSession,
    event: P2POutbox,
    error: str,
    *,
    max_retries: int = 10,
) -> None:
    """Зафиксировать неудачную попытку + назначить next_retry."""
    from datetime import datetime, timedelta, timezone
    event.retry_count = (event.retry_count or 0) + 1
    event.last_error = (error or "")[:1000]
    if event.retry_count >= max_retries:
        event.status = OutboxStatus.DEAD_LETTER.value
        logger.error("[outbox] DEAD_LETTER event=%s after %d retries: %s",
                     event.event_id, event.retry_count, error)
    else:
        # Exponential backoff: 5s, 10s, 30s, 60s, 5m, 15m, 30m, 1h, 1h, 1h
        backoff_seconds = [5, 10, 30, 60, 300, 900, 1800, 3600, 3600, 3600]
        idx = min(event.retry_count - 1, len(backoff_seconds) - 1)
        event.status = OutboxStatus.PENDING.value
        event.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds[idx])
    await db.flush()

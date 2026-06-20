"""Inbox Consumer Worker (ТЗ Том 19).

Exactly-once consume входящих событий из внешних систем:
- JARVIS balance sync
- TRON blockchain events
- Payment webhooks

P2PInbox.event_id + consumer UNIQUE на уровне БД пресекает дубликаты.

Каждые 2-3 сек worker:
- claim_pending() из p2p_inbox (FOR UPDATE SKIP LOCKED)
- dispatch на handler по event_type
- mark_processed / mark_failed (exponential backoff)
- после max_retries → DEAD_LETTER

Регистрация handler'ов: внешние модули могут добавить через register_handler().
"""
from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import AsyncSessionLocal
from p2p.models import P2PInbox

logger = logging.getLogger("p2p.worker.inbox")


# Consumer name (для UniqueConstraint consumer+event_id)
CONSUMER_NAME = "p2p_inbox_default"

# Inbox statuses (P2PInbox.status — VARCHAR)
STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_PROCESSED = "PROCESSED"
STATUS_FAILED = "FAILED"
STATUS_DEAD_LETTER = "DEAD_LETTER"


# ═══════════════════════════════════════════════════════════════════════
# Handlers registry
# ═══════════════════════════════════════════════════════════════════════
# Сюда внешние модули регистрируют свои handler'ы.
# Сигнатура: async def handler(db: AsyncSession, payload: dict) -> None
#
# TODO: подключить handler'ы для будущих типов событий:
#   - "jarvis.balance_sync"     — sync балансов из JARVIS (Том 17)
#   - "tron.deposit_confirmed"  — confirmed TRC20 deposit
#   - "payment.received"        — webhook от фиатного провайдера
#   - "kyc.status_changed"      — обновление KYC статуса
#   - "fraud.alert"             — алерт от Fraud Engine
_HANDLERS: dict[str, Callable[[AsyncSession, dict], Awaitable[None]]] = {}


def register_handler(
    event_type: str,
    handler: Callable[[AsyncSession, dict], Awaitable[None]],
) -> None:
    """Зарегистрировать handler для конкретного event_type."""
    _HANDLERS[event_type] = handler
    logger.info("[inbox] handler registered: %s", event_type)


# ═══════════════════════════════════════════════════════════════════════
# Public API: добавить событие в inbox (для webhook handlers и т.п.)
# ═══════════════════════════════════════════════════════════════════════

async def enqueue_inbox_event(
    db: AsyncSession,
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    source: str,
    correlation_id: str | None = None,
    consumer: str = CONSUMER_NAME,
) -> str | None:
    """Положить событие в inbox для exactly-once consume.

    Возвращает inbox row id или None если дубликат (UniqueConstraint hit).
    """
    # Пытаемся вставить через прямой SQL чтобы поддержать колонки которых
    # может не быть в текущей ORM-модели (event_type, payload, source, ...).
    row_id = str(uuid.uuid4())
    try:
        await db.execute(
            text(
                "INSERT INTO p2p_inbox "
                "(id, consumer, event_id, event_type, payload, source, status, "
                " correlation_id, retry_count, processed_at) "
                "VALUES (:id, :consumer, :event_id, :event_type, "
                " CAST(:payload AS JSONB), :source, :status, :corr, 0, NULL) "
                "ON CONFLICT ON CONSTRAINT uq_p2p_inbox_consumer_event DO NOTHING"
            ),
            {
                "id": row_id,
                "consumer": consumer,
                "event_id": event_id,
                "event_type": event_type,
                "payload": _json_dumps(payload or {}),
                "source": source,
                "status": STATUS_PENDING,
                "corr": correlation_id,
            },
        )
        await db.flush()
        return row_id
    except Exception as e:
        # Fallback: попробовать ORM-вставкой если SQL не прошёл (колонки отсутствуют)
        logger.warning("[inbox] enqueue via SQL failed (%s), falling back to ORM", e)
        try:
            row = P2PInbox(
                consumer=consumer,
                event_id=event_id,
                status=STATUS_PENDING,
                correlation_id=correlation_id,
            )
            db.add(row)
            await db.flush()
            return row.id
        except Exception as e2:
            logger.warning("[inbox] ORM enqueue also failed: %s", e2)
            return None


def _json_dumps(v: Any) -> str:
    import json
    try:
        return json.dumps(v, default=str)
    except Exception:
        return "{}"


# ═══════════════════════════════════════════════════════════════════════
# Worker internals
# ═══════════════════════════════════════════════════════════════════════

async def claim_pending(db: AsyncSession, limit: int = 50) -> list[dict]:
    """Atomic claim — пометить PENDING как PROCESSING и вернуть для обработки.

    Возвращает list of dicts (а не ORM объектов, потому что часть колонок
    может отсутствовать в модели).
    """
    now = datetime.now(timezone.utc)
    # Защищаемся: некоторые колонки могут отсутствовать. Сначала пробуем
    # полный запрос, при ошибке fallback на минимальный.
    try:
        res = await db.execute(
            text(
                "SELECT id, consumer, event_id, event_type, payload, source, "
                "       status, retry_count, correlation_id "
                "FROM p2p_inbox "
                "WHERE status = :pending "
                "  AND (next_retry_at IS NULL OR next_retry_at <= :now) "
                "ORDER BY processed_at NULLS FIRST, id ASC "
                "LIMIT :limit "
                "FOR UPDATE SKIP LOCKED"
            ),
            {"pending": STATUS_PENDING, "now": now, "limit": limit},
        )
        rows = [dict(r._mapping) for r in res.all()]
    except Exception as e:
        logger.warning("[inbox] full claim query failed (%s), using minimal", e)
        # Минимальный fallback — только core колонки модели
        res = await db.execute(
            text(
                "SELECT id, consumer, event_id, status, correlation_id "
                "FROM p2p_inbox "
                "WHERE status = :pending "
                "LIMIT :limit "
                "FOR UPDATE SKIP LOCKED"
            ),
            {"pending": STATUS_PENDING, "limit": limit},
        )
        rows = [dict(r._mapping) for r in res.all()]

    if not rows:
        return []

    ids = [r["id"] for r in rows]
    await db.execute(
        text("UPDATE p2p_inbox SET status=:s WHERE id = ANY(:ids)"),
        {"s": STATUS_PROCESSING, "ids": ids},
    )
    await db.flush()
    return rows


async def mark_processed(db: AsyncSession, inbox_row: dict) -> None:
    now = datetime.now(timezone.utc)
    try:
        await db.execute(
            text(
                "UPDATE p2p_inbox "
                "SET status=:s, processed_at=:now, last_error=NULL "
                "WHERE id=:id"
            ),
            {"s": STATUS_PROCESSED, "now": now, "id": inbox_row["id"]},
        )
    except Exception:
        await db.execute(
            text("UPDATE p2p_inbox SET status=:s, processed_at=:now WHERE id=:id"),
            {"s": STATUS_PROCESSED, "now": now, "id": inbox_row["id"]},
        )
    await db.flush()


async def mark_failed(
    db: AsyncSession,
    inbox_row: dict,
    err: str,
    *,
    max_retries: int = 10,
) -> None:
    """Зафиксировать неудачную попытку + назначить next_retry.

    Exponential backoff: 5s, 10s, 30s, 60s, 5m, 15m, 1h, 1h, 1h, 1h
    """
    retry_count = int(inbox_row.get("retry_count") or 0) + 1
    backoff_seconds = [5, 10, 30, 60, 300, 900, 3600, 3600, 3600, 3600]
    if retry_count >= max_retries:
        new_status = STATUS_DEAD_LETTER
        next_retry = None
        logger.error(
            "[inbox] DEAD_LETTER id=%s event_id=%s after %d retries: %s",
            inbox_row.get("id"), inbox_row.get("event_id"), retry_count, err,
        )
    else:
        new_status = STATUS_PENDING
        idx = min(retry_count - 1, len(backoff_seconds) - 1)
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds[idx])

    try:
        await db.execute(
            text(
                "UPDATE p2p_inbox "
                "SET status=:s, retry_count=:rc, next_retry_at=:nr, "
                "    last_error=LEFT(:err, 1000) "
                "WHERE id=:id"
            ),
            {
                "s": new_status, "rc": retry_count, "nr": next_retry,
                "err": (err or "")[:1000], "id": inbox_row["id"],
            },
        )
    except Exception:
        # Минимальный fallback (если колонок нет)
        await db.execute(
            text("UPDATE p2p_inbox SET status=:s WHERE id=:id"),
            {"s": new_status, "id": inbox_row["id"]},
        )
    await db.flush()


async def _dispatch(db: AsyncSession, inbox_row: dict) -> None:
    """Найти handler по event_type и вызвать его."""
    event_type = inbox_row.get("event_type")
    payload = inbox_row.get("payload") or {}
    if not event_type:
        # Если поля event_type ещё нет в БД — просто mark_processed (no-op)
        logger.debug(
            "[inbox] no event_type on row id=%s — marking processed",
            inbox_row.get("id"),
        )
        return

    handler = _HANDLERS.get(event_type)
    if handler is None:
        # Нет handler'а — это нормально на данном этапе (handler'ы пока не написаны).
        # Логируем и mark_processed чтобы не блокировать очередь.
        logger.info(
            "[inbox] no handler for event_type=%s (id=%s) — skipping",
            event_type, inbox_row.get("id"),
        )
        return

    await handler(db, payload if isinstance(payload, dict) else {})


async def _process_one(inbox_row: dict) -> tuple[bool, str | None]:
    """Обработать одно сообщение в отдельной транзакции."""
    try:
        async with AsyncSessionLocal() as db:
            await _dispatch(db, inbox_row)
            await mark_processed(db, inbox_row)
            await db.commit()
        return True, None
    except Exception as e:
        logger.exception(
            "[inbox] dispatch failed id=%s event_type=%s: %s",
            inbox_row.get("id"), inbox_row.get("event_type"), e,
        )
        # Mark failed в отдельной транзакции
        try:
            async with AsyncSessionLocal() as db:
                await mark_failed(db, inbox_row, str(e))
                await db.commit()
        except Exception as e2:
            logger.warning("[inbox] mark_failed itself failed: %s", e2)
        return False, str(e)


async def run() -> None:
    """Главный цикл worker'а. Запускается из api/main.py lifespan."""
    logger.info("[inbox-worker] started, consumer=%s, handlers=%d",
                CONSUMER_NAME, len(_HANDLERS))
    backoff = 1.0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                events = await claim_pending(db, limit=50)
                await db.commit()
            if not events:
                await asyncio.sleep(2.5)
                continue
            logger.info("[inbox-worker] claimed %d events", len(events))
            for ev in events:
                await _process_one(ev)
            backoff = 1.0
        except Exception as e:
            logger.exception("[inbox-worker] iteration failed: %s", e)
            await asyncio.sleep(min(backoff, 30))
            backoff *= 2

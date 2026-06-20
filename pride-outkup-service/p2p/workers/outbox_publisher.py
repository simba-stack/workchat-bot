"""Outbox Publisher Worker (ТЗ Том 19).

Каждые 1-2 сек:
- claim_pending() из p2p_outbox (FOR UPDATE SKIP LOCKED)
- для каждого события: WS broadcast + bot.notify_user
- mark_published / mark_failed (exponential backoff)
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

from core.db import AsyncSessionLocal
from p2p import outbox
from p2p.enums import EventType
from p2p.models import P2POutbox

logger = logging.getLogger("p2p.worker.outbox")


# WebSocket subscribers (filled in api/websocket.py later)
_ws_publishers: list = []


def register_ws_publisher(fn) -> None:
    """Регистрация WS publisher из api/main.py.
    fn: async def(event_type: str, payload: dict, aggregate_type: str|None, aggregate_id: str|None) -> None
    """
    _ws_publishers.append(fn)


# Bot notify helper
async def _bot_notify(event_type: str, payload: dict) -> None:
    try:
        from bot.main import notify_user  # type: ignore
    except Exception:
        return  # bot не загружен — пропустим
    # Простой маппинг: используем buyer_id/seller_id из payload
    targets: set[int] = set()
    for k in ("buyer_id", "seller_id", "owner_id", "opener_id"):
        if k in payload and payload[k]:
            try:
                targets.add(int(payload[k]))
            except Exception:
                pass
    # текст уведомления
    text = _format_notification(event_type, payload)
    if not text:
        return
    for uid in targets:
        try:
            await notify_user(uid, text)
        except Exception as e:
            logger.debug("[outbox] bot notify failed user=%s: %s", uid, e)


def _format_notification(event_type: str, p: dict) -> str | None:
    if event_type == EventType.TRADE_CREATED.value:
        return f"🆕 Новая сделка #{p.get('trade_id','?')[:8]} — {p.get('amount_crypto')} {p.get('crypto')} / {p.get('amount_fiat')} {p.get('fiat')}"
    if event_type == EventType.TRADE_PAYMENT_MARKED.value:
        return f"💸 Покупатель отметил оплату по сделке #{str(p.get('trade_id',''))[:8]}"
    if event_type == EventType.TRADE_COMPLETED.value:
        return f"✅ Сделка #{str(p.get('trade_id',''))[:8]} завершена — {p.get('amount')} {p.get('crypto')}"
    if event_type == EventType.TRADE_CANCELLED.value:
        return f"❌ Сделка #{str(p.get('trade_id',''))[:8]} отменена — {p.get('reason','')}"
    if event_type == EventType.DISPUTE_OPENED.value:
        return f"⚠️ Открыт диспут по сделке #{str(p.get('trade_id',''))[:8]}"
    if event_type == EventType.DISPUTE_RESOLVED.value:
        return f"⚖️ Диспут решён ({p.get('resolution')})"
    if event_type == EventType.ADVERTISEMENT_CREATED.value:
        return None  # не уведомлять никого по создании ad
    return None


async def _process_one(event: P2POutbox) -> tuple[bool, str | None]:
    """Опубликовать одно событие. Возвращает (success, error_text)."""
    try:
        # 1) WS broadcast
        for fn in _ws_publishers:
            try:
                await fn(event.event_type, event.payload or {},
                        event.aggregate_type, event.aggregate_id)
            except Exception as e:
                logger.warning("[outbox] WS publisher failed: %s", e)
        # 2) Bot notify
        try:
            await _bot_notify(event.event_type, event.payload or {})
        except Exception as e:
            logger.warning("[outbox] bot notify failed: %s", e)
        # 3) Persistent notifications в p2p_notifications
        try:
            await _persist_notifications(event)
        except Exception as e:
            logger.warning("[outbox] persist notifications failed: %s", e)
        return True, None
    except Exception as e:
        return False, str(e)


async def _persist_notifications(event) -> None:
    """Создать P2PNotification записи для участников события."""
    from p2p.api.notifications import create_notification
    from core.db import AsyncSessionLocal
    text = _format_notification(event.event_type, event.payload or {})
    if not text:
        return
    targets: set[int] = set()
    p = event.payload or {}
    for k in ("buyer_id", "seller_id", "owner_id", "opened_by_id"):
        if k in p and p[k]:
            try:
                targets.add(int(p[k]))
            except Exception:
                pass
    if not targets:
        return
    async with AsyncSessionLocal() as db:
        for uid in targets:
            try:
                await create_notification(
                    db, user_id=uid, type_=event.event_type,
                    title=text, body=None, payload=p,
                    correlation_id=event.correlation_id,
                )
            except Exception:
                pass
        await db.commit()


async def run() -> None:
    logger.info("[outbox-worker] started")
    backoff = 1.0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                events = await outbox.claim_pending(db, limit=50)
                if not events:
                    await db.commit()
                    await asyncio.sleep(2.0)
                    continue
                logger.info("[outbox-worker] claimed %d events", len(events))
                for event in events:
                    ok, err = await _process_one(event)
                    if ok:
                        await outbox.mark_published(db, event)
                    else:
                        await outbox.mark_failed(db, event, err or "unknown")
                await db.commit()
                backoff = 1.0
        except Exception as e:
            logger.exception("[outbox-worker] iteration failed: %s", e)
            await asyncio.sleep(min(backoff, 30))
            backoff *= 2

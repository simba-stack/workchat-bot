"""Event bus для real-time push событий с бота на дашборд.

Pub/sub в памяти. Userbot, accounting, и т.д. публикуют события через
emit_event(). FastAPI SSE-эндпоинт подписывается через subscribe() и
стримит их клиенту.

Безопасно: если очередь не создана / нет подписчиков — emit_event()
просто кладёт в ring buffer и возвращается без ошибок. Не блокирует
основную логику бота даже если очередь полна.

Никаких записей в storage — события живут только в RAM. При рестарте
бота история теряется (это OK для UI-дашборда).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Ring buffer последних N событий — новые подписчики получают эту историю
# при подключении (чтобы дашборд показал что-то сразу, без ожидания).
_HISTORY_SIZE = 500
_history: list = []

# Активные подписчики — у каждого своя asyncio.Queue в которую копируется
# каждое событие. Если subscriber не успевает читать — события дропаются
# через QueueFull.
_subscribers: set = set()


def emit_event(
    event_type: str,
    payload: Optional[dict] = None,
    character: str = "",
    severity: str = "info",
) -> None:
    """Опубликовать событие.

    Args:
        event_type: короткий идентификатор события в kebab-case
            (например 'ai-reply', 'lk-created', 'application-processed').
        payload: данные для UI (будут JSON-сериализованы).
        character: какого персонажа анимировать на дашборде:
            'chat' — AI отвечает клиенту
            'accounting' — обработка заявки / отчёта
            'lk' — карточка ЛК / БРАК / БЛОК
            '' — без персонажа (фоновое событие)
        severity: 'info' / 'success' / 'warning' / 'alert' — цвет в логах
            и тип анимации (idle / working / alert).

    Не падает если очередь не доступна или нет подписчиков.
    """
    try:
        event = {
            "type": event_type,
            "character": character,
            "severity": severity,
            "payload": payload or {},
            "ts": time.time(),
        }
        _history.append(event)
        if len(_history) > _HISTORY_SIZE:
            _history.pop(0)
        # Веерная рассылка по подписчикам — не блокирует.
        for q in list(_subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Подписчик не успевает читать. Дропаем тихо.
                pass
            except Exception:
                pass
    except Exception as e:
        # Логируем но НЕ пробрасываем — основная логика бота не должна
        # падать из-за проблем с дашбордом.
        logger.warning("event_bus.emit_event failed (%s): %s", event_type, e)


def get_history(n: int = 100) -> list:
    """Последние N событий — для replay при подключении нового клиента."""
    if n <= 0:
        return []
    return list(_history[-n:])


def subscriber_count() -> int:
    return len(_subscribers)


async def subscribe(replay_last: int = 50) -> AsyncIterator[dict]:
    """Подписаться на поток событий. Возвращает async generator.

    При подключении сначала отдаёт replay_last прошлых событий из истории,
    дальше — новые события в реальном времени.

    Usage:
        async for event in event_bus.subscribe():
            ...

    При отмене таска подписчик автоматически удаляется.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    try:
        # Replay истории — клиент сразу видит что было.
        for e in get_history(replay_last):
            yield e
        # Стрим новых событий.
        while True:
            event = await q.get()
            yield event
    finally:
        _subscribers.discard(q)

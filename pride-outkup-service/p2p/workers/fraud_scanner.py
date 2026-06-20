"""Fraud Scanner Worker (ТЗ Том 11).

Каждые 5 минут запускает все детекторы из p2p.fraud за окно последнего часа.
Дедупликация по hash(pattern + sorted user_ids + sorted trade_ids) с TTL 1 час.

Алерты пишутся в audit_log + emit через outbox (SYSTEM_ALERT).
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from core.db import AsyncSessionLocal
from p2p import fraud

logger = logging.getLogger("p2p.worker.fraud")

SCAN_INTERVAL_SEC = 300        # 5 минут
SCAN_WINDOW_HOURS = 1          # сканируем последний час
DEDUP_TTL_SEC = 3600           # 1 час

# In-memory кеш дедупликации: dedup_key → first_seen_ts
_seen: dict[str, float] = {}


def _gc_seen() -> None:
    """Сборщик мусора в _seen — выкидываем записи старше DEDUP_TTL_SEC."""
    now = time.time()
    expired = [k for k, ts in _seen.items() if now - ts > DEDUP_TTL_SEC]
    for k in expired:
        _seen.pop(k, None)


async def run_once() -> dict:
    """Один проход скана. Возвращает {alerts_total, alerts_new, alerts_deduped}."""
    _gc_seen()
    since = datetime.now(timezone.utc) - timedelta(hours=SCAN_WINDOW_HOURS)
    new_count = 0
    dedup_count = 0
    total = 0
    async with AsyncSessionLocal() as db:
        try:
            alerts = await fraud.run_all_detectors(db, since=since)
        except Exception as e:
            logger.exception("[fraud-worker] run_all_detectors failed: %s", e)
            return {"alerts_total": 0, "alerts_new": 0, "alerts_deduped": 0, "error": str(e)[:200]}
        total = len(alerts)
        for a in alerts:
            key = a.dedup_key()
            if key in _seen:
                dedup_count += 1
                continue
            _seen[key] = time.time()
            try:
                await fraud.record_alert(db, a)
                new_count += 1
            except Exception as e:
                logger.warning("[fraud-worker] record_alert failed pattern=%s: %s",
                               a.pattern, e)
        if new_count > 0:
            try:
                await db.commit()
            except Exception as e:
                logger.exception("[fraud-worker] commit failed: %s", e)
                await db.rollback()
    return {"alerts_total": total, "alerts_new": new_count, "alerts_deduped": dedup_count}


async def run() -> None:
    logger.info("[fraud-worker] started (interval=%ds, window=%dh)",
                SCAN_INTERVAL_SEC, SCAN_WINDOW_HOURS)
    while True:
        try:
            r = await run_once()
            if r.get("alerts_new", 0) > 0:
                logger.warning("[fraud-worker] scan done: %s", r)
            else:
                logger.info("[fraud-worker] scan done: %s", r)
        except Exception as e:
            logger.exception("[fraud-worker] iteration failed: %s", e)
        await asyncio.sleep(SCAN_INTERVAL_SEC)

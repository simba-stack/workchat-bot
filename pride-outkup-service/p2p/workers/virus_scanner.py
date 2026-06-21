"""Virus Scanner Worker (TODO #2, stub MVP).

Каждые 30 сек:
  1. SELECT ... FOR UPDATE SKIP LOCKED → берёт P2PAttachment с virus_scan_status='PENDING'
  2. Помечает 'SCANNING'
  3. Имитирует скан (sleep 0.5s) — 99% CLEAN, 1% INFECTED
  4. Если INFECTED — emit SYSTEM_ALERT через outbox + статус остаётся 'INFECTED' (frontend
     не отдаёт ссылку на такой файл).

Это stub до реальной интеграции (ClamAV / VirusTotal API).
"""
from __future__ import annotations
import asyncio
import logging
import random
from datetime import datetime, timezone

from sqlalchemy import select, update

from core.db import AsyncSessionLocal
from p2p import outbox
from p2p.enums import EventType
from p2p.models import P2PAttachment

logger = logging.getLogger("p2p.worker.virus_scanner")

SCAN_INTERVAL_SECONDS = 30
BATCH_LIMIT = 20
INFECTED_PROBABILITY = 0.01  # 1% chance to flag INFECTED (stub)


async def _scan_one(db, att: P2PAttachment) -> str:
    """Имитация скана. Возвращает финальный статус: CLEAN | INFECTED."""
    await asyncio.sleep(0.5)
    if random.random() < INFECTED_PROBABILITY:
        return "INFECTED"
    return "CLEAN"


async def _process_batch() -> int:
    """Найти PENDING attachments, проскорить, обновить статус."""
    processed = 0
    async with AsyncSessionLocal() as db:
        try:
            # claim batch (SKIP LOCKED — параллельные worker'ы безопасны)
            r = await db.execute(
                select(P2PAttachment)
                .where(P2PAttachment.virus_scan_status == "PENDING")
                .order_by(P2PAttachment.created_at)
                .limit(BATCH_LIMIT)
                .with_for_update(skip_locked=True)
            )
            atts = list(r.scalars().all())
            if not atts:
                return 0
            ids = [a.id for a in atts]
            await db.execute(
                update(P2PAttachment)
                .where(P2PAttachment.id.in_(ids))
                .values(virus_scan_status="SCANNING")
            )
            await db.commit()
        except Exception as e:
            logger.warning("[virus_scanner] claim batch failed: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            return 0

    for att_id in ids:
        async with AsyncSessionLocal() as db:
            try:
                r = await db.execute(
                    select(P2PAttachment).where(P2PAttachment.id == att_id)
                )
                att = r.scalar_one_or_none()
                if not att:
                    continue
                result = await _scan_one(db, att)
                att.virus_scan_status = result
                if result == "INFECTED":
                    logger.warning(
                        "[virus_scanner] INFECTED file id=%s sha=%s uploader=%s",
                        att.id, att.sha256[:16], att.uploaded_by_id,
                    )
                    try:
                        await outbox.emit(
                            db,
                            event_type=EventType.SYSTEM_ALERT.value,
                            payload={
                                "alert": "virus_detected",
                                "attachment_id": att.id,
                                "sha256": att.sha256,
                                "mime_type": att.mime_type,
                                "file_size": att.file_size,
                                "uploaded_by_id": att.uploaded_by_id,
                                "detected_at": datetime.now(timezone.utc).isoformat(),
                            },
                            aggregate_type="attachment",
                            aggregate_id=att.id,
                        )
                    except Exception as e:
                        logger.warning("[virus_scanner] emit alert failed: %s", e)
                await db.commit()
                processed += 1
            except Exception as e:
                logger.exception("[virus_scanner] scan failed id=%s: %s", att_id, e)
                try:
                    await db.rollback()
                except Exception:
                    pass
                # Откатим SCANNING → PENDING чтобы попробовать снова
                try:
                    async with AsyncSessionLocal() as db2:
                        await db2.execute(
                            update(P2PAttachment)
                            .where(P2PAttachment.id == att_id)
                            .values(virus_scan_status="PENDING")
                        )
                        await db2.commit()
                except Exception:
                    pass
    return processed


async def run() -> None:
    """Worker entry point. Запускается в lifespan через asyncio.create_task."""
    logger.info("[virus_scanner] started (interval=%ds, batch=%d, infect_p=%.4f)",
                SCAN_INTERVAL_SECONDS, BATCH_LIMIT, INFECTED_PROBABILITY)
    while True:
        try:
            n = await _process_batch()
            if n:
                logger.info("[virus_scanner] processed %d attachments", n)
        except Exception as e:
            logger.exception("[virus_scanner] iteration failed: %s", e)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

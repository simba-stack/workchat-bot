"""Virus Scanner Worker (TODO #2 — реальный ClamAV).

Каждые 30 сек:
  1. SELECT ... FOR UPDATE SKIP LOCKED → берёт P2PAttachment с virus_scan_status='PENDING'
  2. Помечает 'SCANNING'
  3. Читает файл с диска (storage_key) и отдаёт его демону ClamAV (INSTREAM)
  4. CLEAN/INFECTED → пишет статус. Если INFECTED — emit SYSTEM_ALERT через outbox,
     а download-эндпоинт отдаёт 451 (карантин).

Fail-closed: если ClamAV выключен/недоступен — файл НЕ помечается CLEAN, остаётся
PENDING до следующей попытки (даунлоад по-прежнему доступен пока файл не доказан
INFECTED — таков существующий gating в attachments.py).

Скан реализован в core/services/clamav_service.py. Демон поднимается отдельно
(контейнер clamav/clamav), адрес — в ENV CLAMAV_HOST/PORT или CLAMAV_SOCKET.
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, update

from core.config import settings
from core.db import AsyncSessionLocal
from core.services import clamav_service
from core.services.clamav_service import ClamAVUnavailable
from p2p import outbox
from p2p.enums import EventType
from p2p.models import P2PAttachment

logger = logging.getLogger("p2p.worker.virus_scanner")

SCAN_INTERVAL_SECONDS = 30
BATCH_LIMIT = 20
# Максимальный размер файла для INSTREAM-скана (защита от OOM). Больше — пропускаем
# скан и оставляем PENDING (крупное видео можно сканировать отдельным пайплайном).
MAX_SCAN_BYTES = 100 * 1024 * 1024  # 100 MB

_STORAGE_BASE = os.environ.get("STORAGE_PATH") or os.path.join(
    os.getcwd(), "storage", "p2p",
)


def _resolve_path(storage_key: str) -> str | None:
    """Безопасно (anti path-traversal) получить абсолютный путь файла."""
    if not storage_key:
        return None
    key = storage_key.replace("\\", "/")
    if ".." in key.split("/") or key.startswith("/"):
        return None
    return os.path.abspath(os.path.join(_STORAGE_BASE, key))


async def _scan_one(db, att: P2PAttachment) -> str | None:
    """Реальный ClamAV-скан. Возвращает 'CLEAN'|'INFECTED', либо None — оставить PENDING.

    None означает «не удалось проверить» (AV выключен/недоступен/файла нет/слишком
    большой) — статус не меняем, попробуем в следующей итерации.
    """
    abs_path = _resolve_path(att.storage_key)
    if not abs_path or not os.path.isfile(abs_path):
        logger.warning("[virus_scanner] file missing on disk id=%s key=%s",
                       att.id, att.storage_key)
        return None

    size = os.path.getsize(abs_path)
    if size > MAX_SCAN_BYTES:
        logger.warning("[virus_scanner] file too large to scan id=%s size=%d — skip",
                       att.id, size)
        return None

    try:
        data = await asyncio.to_thread(_read_file, abs_path)
        result = await clamav_service.scan_bytes(data)
    except ClamAVUnavailable as e:
        # Демон недоступен — fail-closed: оставляем PENDING (retry)
        logger.warning("[virus_scanner] ClamAV unavailable for id=%s: %s", att.id, e)
        return None

    if result.infected:
        logger.warning("[virus_scanner] INFECTED id=%s signature=%s", att.id, result.signature)
    return result.status


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


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
                if result is None:
                    # Не удалось проверить (AV off/недоступен/файла нет) —
                    # вернуть SCANNING -> PENDING для повторной попытки (rollback тут НЕ
                    # помогает: SCANNING закоммичен в сессии клейма батча).
                    att.virus_scan_status = "PENDING"
                    await db.commit()
                    continue
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
    logger.info("[virus_scanner] started (interval=%ds, batch=%d, clamav_enabled=%s)",
                SCAN_INTERVAL_SECONDS, BATCH_LIMIT, settings.clamav_enabled)
    if not settings.clamav_enabled:
        logger.warning("[virus_scanner] CLAMAV_ENABLED=false — скан вложений отключён, "
                       "файлы остаются PENDING (fail-closed). Воркер простаивает.")
    while True:
        try:
            if settings.clamav_enabled:
                n = await _process_batch()
                if n:
                    logger.info("[virus_scanner] processed %d attachments", n)
        except Exception as e:
            logger.exception("[virus_scanner] iteration failed: %s", e)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

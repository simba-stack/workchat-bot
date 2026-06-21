"""P2P Attachments API (Том 13 + 27.4).

Endpoints:
  POST /api/v2/p2p/trades/{trade_id}/attachments  — upload file + auto-message
  GET  /api/v2/p2p/attachments/{attachment_id}    — download with RBAC

Хранилище — локальная файловая система под STORAGE_PATH (env), default
./storage/p2p. Файл кладётся в <STORAGE_BASE>/<sha256[:2]>/<sha256>.<ext>
(content-addressable: дубли по содержимому склеиваются).

Жёсткие лимиты:
  IMAGE       — 20 MB
  VIDEO       — 100 MB
  VOICE       — 25 MB
  DOCUMENT    — 50 MB
  PAYMENT_PROOF — 20 MB (как IMAGE)

После записи P2PAttachment + P2PMessage в outbox emit-ится ChatMessageSent
(чтобы WS-клиенты получили апдейт).
"""
from __future__ import annotations
import hashlib
import logging
import mimetypes
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.config import settings
from core.db import get_db
from core.models import User
from p2p import audit, locks, outbox, rbac
from p2p.enums import EventType, MessageType, TradeStatus
from p2p.models import P2PAttachment, P2PMessage, P2PTrade

logger = logging.getLogger("p2p.api.attachments")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-attachments"])


# ═════════════════════════════════════════════════════════════════
# Config
# ═════════════════════════════════════════════════════════════════

STORAGE_BASE = os.environ.get("STORAGE_PATH") or os.path.join(
    os.getcwd(), "storage", "p2p",
)

# Лимиты в байтах
_MB = 1024 * 1024
SIZE_LIMITS: dict[str, int] = {
    MessageType.IMAGE.value: 20 * _MB,
    MessageType.PAYMENT_PROOF.value: 20 * _MB,
    MessageType.VIDEO.value: 100 * _MB,
    MessageType.VOICE.value: 25 * _MB,
    MessageType.DOCUMENT.value: 50 * _MB,
}

_ALLOWED_TYPES = set(SIZE_LIMITS.keys())

# Состояния где апдейтить чат уже нельзя
_CLOSED_STATES = {
    TradeStatus.COMPLETED.value,
    TradeStatus.CANCELLED.value,
    TradeStatus.RESOLVED.value,
}
_DISPUTE_STATES = {TradeStatus.DISPUTE_OPENED.value, TradeStatus.ARBITRATION.value}

# Чанк для стримингового SHA256+write (8 KB запас на медленные тоннели)
_CHUNK_BYTES = 64 * 1024


# ═════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════

def _ensure_storage_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _ext_for_mime(mime: str, fallback_name: str | None) -> str:
    """Подобрать расширение по MIME (или по имени файла как fallback)."""
    if fallback_name:
        suf = Path(fallback_name).suffix
        if suf and len(suf) <= 8:
            return suf.lower()
    if not mime:
        return ".bin"
    guess = mimetypes.guess_extension(mime.split(";")[0].strip()) or ""
    if not guess:
        # Частые недостающие у Python
        m = (mime or "").lower()
        if "ogg" in m:
            return ".ogg"
        if "webm" in m:
            return ".webm"
        if "jpeg" in m:
            return ".jpg"
        return ".bin"
    return guess.lower()


def _storage_paths(sha256: str, ext: str) -> tuple[str, str]:
    """Вернуть (abs_path, storage_key relative)."""
    sub = sha256[:2]
    fname = f"{sha256}{ext}"
    rel = f"{sub}/{fname}"
    abs_path = os.path.join(STORAGE_BASE, sub, fname)
    return abs_path, rel


def _resolve_storage_path(storage_key: str) -> str:
    """Безопасно получить абсолютный путь из storage_key.

    Защита от path traversal: storage_key не должен содержать '..' и
    не должен начинаться с '/'.
    """
    if not storage_key:
        raise HTTPException(404, "attachment file missing")
    key = storage_key.replace("\\", "/")
    if ".." in key.split("/") or key.startswith("/"):
        raise HTTPException(400, "invalid storage_key")
    return os.path.abspath(os.path.join(STORAGE_BASE, key))


def _is_admin_like(user: User) -> bool:
    try:
        return (
            rbac.is_admin(user)
            or rbac.is_arbitrator(user)
            or rbac.is_support(user)
        )
    except Exception:
        try:
            return int(getattr(user, "tg_id", 0) or 0) in set(settings.admin_ids or [])
        except Exception:
            return False


# ═════════════════════════════════════════════════════════════════
# POST /trades/{trade_id}/attachments
# ═════════════════════════════════════════════════════════════════

@router.post("/trades/{trade_id}/attachments")
async def upload_trade_attachment(
    trade_id: str,
    file: UploadFile = File(...),
    caption: str = Form(""),
    message_type: str = Form(MessageType.IMAGE.value),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Принять файл, сохранить на диск, создать P2PAttachment + P2PMessage."""
    mtype = (message_type or "").upper().strip()
    if mtype not in _ALLOWED_TYPES:
        raise HTTPException(
            422,
            f"message_type must be one of {sorted(_ALLOWED_TYPES)}",
        )

    # === 1. Trade lookup + RBAC ===
    tr = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = tr.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    is_buyer = trade.buyer_id == user.id
    is_seller = trade.seller_id == user.id
    admin_like = _is_admin_like(user)

    if not (is_buyer or is_seller):
        # Admin/arbitrator/support — только в DISPUTE/ARBITRATION
        if not (admin_like and trade.status in _DISPUTE_STATES):
            raise HTTPException(403, "not a trade participant")

    if trade.status in _CLOSED_STATES:
        raise HTTPException(
            409,
            f"trade is {trade.status}, attachments not allowed",
        )

    # === 2. Mime + ext ===
    raw_mime = (file.content_type or "application/octet-stream").lower().strip()
    file_name = (file.filename or "").strip()[:256] or None
    ext = _ext_for_mime(raw_mime, file_name)

    # === 3. Stream → sha256 + tmp file → final path ===
    _ensure_storage_dir(STORAGE_BASE)
    tmp_name = f".tmp_{uuid.uuid4().hex}{ext}"
    tmp_abs = os.path.join(STORAGE_BASE, tmp_name)

    hasher = hashlib.sha256()
    size = 0
    limit = SIZE_LIMITS[mtype]

    try:
        with open(tmp_abs, "wb") as fh:
            while True:
                chunk = await file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        413,
                        f"file too large for {mtype} (max {limit // _MB} MB)",
                    )
                hasher.update(chunk)
                fh.write(chunk)
    except HTTPException:
        try:
            os.remove(tmp_abs)
        except OSError:
            pass
        raise
    except Exception as e:
        try:
            os.remove(tmp_abs)
        except OSError:
            pass
        logger.exception("[attach] write tmp failed: %s", e)
        raise HTTPException(500, "failed to store file")

    if size == 0:
        try:
            os.remove(tmp_abs)
        except OSError:
            pass
        raise HTTPException(422, "empty file")

    sha256 = hasher.hexdigest()
    abs_path, storage_key = _storage_paths(sha256, ext)

    # Создаём подпапку <sha[:2]> и переименовываем tmp → final
    try:
        _ensure_storage_dir(os.path.dirname(abs_path))
        if os.path.exists(abs_path):
            # Дубликат содержимого — удаляем tmp, используем существующий
            try:
                os.remove(tmp_abs)
            except OSError:
                pass
        else:
            os.replace(tmp_abs, abs_path)
    except Exception as e:
        try:
            os.remove(tmp_abs)
        except OSError:
            pass
        logger.exception("[attach] move failed: %s", e)
        raise HTTPException(500, "failed to finalize file")

    # === 4. Insert P2PAttachment ===
    att = P2PAttachment(
        sha256=sha256,
        storage_key=storage_key,
        mime_type=raw_mime[:64],
        file_size=size,
        file_name=file_name,
        uploaded_by_id=user.id,
        virus_scan_status="PENDING",
    )
    db.add(att)
    await db.flush()  # нужен att.id для message

    # === 5. sequence_number + P2PMessage ===
    await locks.advisory_lock(db, f"chat:{trade_id}")
    rseq = await db.execute(
        select(func.coalesce(func.max(P2PMessage.sequence_number), 0))
        .where(P2PMessage.trade_id == trade_id)
    )
    next_seq = int(rseq.scalar() or 0) + 1

    cap = (caption or "").strip()[:500] or None
    msg = P2PMessage(
        trade_id=trade_id,
        sender_id=user.id,
        sequence_number=next_seq,
        message_type=mtype,
        text=cap,
        attachment_id=att.id,
        is_system=False,
        status="SENT",
    )
    db.add(msg)
    await db.flush()

    # === 6. Audit + Outbox ===
    actor_role = None
    try:
        actor_role = rbac.resolve_role(user)
    except Exception:
        pass

    await audit.log(
        db,
        action="chat.attachment_uploaded",
        entity_type="trade_attachment",
        entity_id=att.id,
        actor_id=user.id,
        actor_role=actor_role,
        new_state={
            "trade_id": trade_id,
            "message_id": msg.id,
            "sequence_number": next_seq,
            "sha256": sha256,
            "size": size,
            "mime": raw_mime,
            "message_type": mtype,
        },
    )

    await outbox.emit(
        db,
        event_type=EventType.CHAT_MESSAGE_SENT.value,
        payload={
            "message_id": msg.id,
            "trade_id": trade_id,
            "sender_id": user.id,
            "sequence_number": next_seq,
            "message_type": mtype,
            "buyer_id": trade.buyer_id,
            "seller_id": trade.seller_id,
            "attachment_id": att.id,
            "attachment_mime": raw_mime,
            "attachment_size": size,
        },
        aggregate_type="trade",
        aggregate_id=trade_id,
    )

    # Также emit ChatFileUploaded — отдельное событие для аналитики
    try:
        await outbox.emit(
            db,
            event_type=EventType.CHAT_FILE_UPLOADED.value,
            payload={
                "attachment_id": att.id,
                "trade_id": trade_id,
                "uploader_id": user.id,
                "sha256": sha256,
                "size": size,
                "mime": raw_mime,
            },
            aggregate_type="trade",
            aggregate_id=trade_id,
        )
    except Exception as e:
        logger.warning("[attach] outbox ChatFileUploaded failed: %s", e)

    return {
        "ok": True,
        "attachment_id": att.id,
        "message_id": msg.id,
        "sequence_number": next_seq,
        "sha256": sha256,
        "url": f"/api/v2/p2p/attachments/{att.id}",
        "mime": raw_mime,
        "size": size,
        "file_name": file_name,
        "message_type": mtype,
    }


# ═════════════════════════════════════════════════════════════════
# GET /attachments/{attachment_id}  (download)
# ═════════════════════════════════════════════════════════════════

@router.get("/attachments/{attachment_id}")
async def download_attachment(
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Скачать вложение. RBAC: только участник какого-либо trade, в котором
    это вложение было прикреплено (через p2p_messages), или admin/arbitrator/support.
    """
    # Найти attachment
    ar = await db.execute(
        select(P2PAttachment).where(P2PAttachment.id == attachment_id)
    )
    att = ar.scalar_one_or_none()
    if not att:
        raise HTTPException(404, "attachment not found")

    # Карантин
    if (att.virus_scan_status or "").upper() == "INFECTED":
        raise HTTPException(451, "file quarantined (virus detected)")

    admin_like = _is_admin_like(user)
    if not admin_like:
        # Проверяем участие хотя бы в одном trade
        q = (
            select(func.count(P2PTrade.id))
            .select_from(P2PMessage)
            .join(P2PTrade, P2PTrade.id == P2PMessage.trade_id)
            .where(P2PMessage.attachment_id == attachment_id)
            .where(
                (P2PTrade.buyer_id == user.id)
                | (P2PTrade.seller_id == user.id)
            )
        )
        cnt = int((await db.execute(q)).scalar() or 0)
        if cnt == 0:
            raise HTTPException(403, "access denied")

    # Файл на диске
    abs_path = _resolve_storage_path(att.storage_key)
    if not os.path.isfile(abs_path):
        logger.error("[attach] missing file on disk: %s (att=%s)", abs_path, attachment_id)
        raise HTTPException(404, "attachment file not found on disk")

    headers = {
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }
    if att.file_name:
        # Безопасный inline filename
        safe_name = att.file_name.replace('"', "").replace("\n", "")[:200]
        headers["Content-Disposition"] = f'inline; filename="{safe_name}"'

    return FileResponse(
        abs_path,
        media_type=att.mime_type or "application/octet-stream",
        headers=headers,
    )

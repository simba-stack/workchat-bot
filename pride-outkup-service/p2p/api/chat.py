"""P2P Trade Chat API (ТЗ Том 13).

POST   /api/v2/p2p/trades/{trade_id}/messages    — отправить сообщение
GET    /api/v2/p2p/trades/{trade_id}/messages    — list (paginate via after_seq)
"""
from __future__ import annotations
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.config import settings
from core.db import get_db
from core.models import User
from p2p.api._deps import get_idempotency_key, get_actor_role
from p2p.enums import TradeStatus
from p2p.models import P2PMessage, P2PTrade
from p2p.orchestrator import run_workflow
from p2p.workflows import send_message

logger = logging.getLogger("p2p.api.chat")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-chat"])


_DISPUTE_STATES = {TradeStatus.DISPUTE_OPENED.value, TradeStatus.ARBITRATION.value}


def _source(req: Request | None) -> str | None:
    if not req:
        return None
    ua = req.headers.get("user-agent", "")[:50]
    return f"miniapp:{ua}"


@router.post("/trades/{trade_id}/messages")
async def cmd_send_message(
    trade_id: str,
    payload: dict[str, Any] | None = None,
    req: Request = None,  # type: ignore
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "trade_id": trade_id}
    return await run_workflow(
        db,
        workflow_type="send_message",
        user_id=user.id,
        input_payload=input_p,
        handler=send_message.handle,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req),
        endpoint=f"POST /p2p/trades/{trade_id}/messages",
    )


@router.get("/trades/{trade_id}/messages")
async def list_messages(
    trade_id: str,
    after_seq: int = Query(0, ge=0, description="Вернуть сообщения с sequence_number > after_seq"),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Проверка прав: участник трейда или арбитр (admin) при споре
    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = r.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    is_buyer = trade.buyer_id == user.id
    is_seller = trade.seller_id == user.id
    is_admin = user.tg_id in settings.admin_ids
    if not (is_buyer or is_seller):
        if not (is_admin and trade.status in _DISPUTE_STATES):
            raise HTTPException(403, "not a trade participant")

    # Сообщения + sender username + attachment metadata (LEFT JOIN на случай отсутствия)
    from p2p.models import P2PAttachment
    q = (
        select(
            P2PMessage.id,
            P2PMessage.sender_id,
            User.username,
            P2PMessage.message_type,
            P2PMessage.text,
            P2PMessage.attachment_id,
            P2PMessage.is_system,
            P2PMessage.status,
            P2PMessage.created_at,
            P2PMessage.sequence_number,
            P2PAttachment.mime_type,
            P2PAttachment.file_size,
            P2PAttachment.file_name,
            P2PAttachment.storage_key,
            P2PAttachment.preview_key,
            P2PAttachment.sha256,
            P2PAttachment.virus_scan_status,
        )
        .select_from(P2PMessage)
        .join(User, User.id == P2PMessage.sender_id, isouter=True)
        .join(P2PAttachment, P2PAttachment.id == P2PMessage.attachment_id, isouter=True)
        .where(P2PMessage.trade_id == trade_id)
        .where(P2PMessage.sequence_number > after_seq)
        .order_by(P2PMessage.sequence_number.asc())
        .limit(limit)
    )
    rows = (await db.execute(q)).all()

    def _att_url(storage_key):
        if not storage_key:
            return None
        # Если storage_key уже URL (http/https) — возвращаем как есть.
        # Иначе предполагаем что это S3-ключ, нужен presigned URL (TODO когда будет S3).
        if storage_key.startswith("http"):
            return storage_key
        return f"/api/v2/p2p/attachments/{storage_key}"  # placeholder route

    items = [
        {
            "id": row[0],
            "sender_id": row[1],
            "sender_username": row[2],
            "message_type": row[3],
            "body_text": row[4],
            "attachment_id": row[5],
            "is_system": bool(row[6]),
            "status": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
            "sequence_number": row[9],
            # attachment metadata (null если нет вложения)
            "attachment_mime": row[10],
            "attachment_size": row[11],
            "attachment_name": row[12],
            "attachment_url": _att_url(row[13]),
            "attachment_preview_url": _att_url(row[14]),
            "attachment_sha256": row[15],
            "attachment_virus_status": row[16],
        }
        for row in rows
    ]
    return {
        "items": items,
        "trade_id": trade_id,
        "after_seq": after_seq,
        "limit": limit,
        "count": len(items),
        "next_after_seq": items[-1]["sequence_number"] if items else after_seq,
    }

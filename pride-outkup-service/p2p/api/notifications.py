"""P2P Notifications API."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc, update as sql_update, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p.models import P2PNotification

logger = logging.getLogger("p2p.api.notif")
router = APIRouter(prefix="/api/v2/p2p/notifications", tags=["p2p-notifications"])


def _to_dict(n: P2PNotification) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "payload": n.payload or {},
        "is_read": n.is_read,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "read_at": n.read_at.isoformat() if n.read_at else None,
    }


@router.get("")
async def list_notifications(
    only_unread: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(P2PNotification).where(P2PNotification.user_id == user.id)
    if only_unread:
        q = q.where(P2PNotification.is_read == False)  # noqa: E712
    q = q.order_by(desc(P2PNotification.created_at)).limit(limit).offset(offset)
    r = await db.execute(q)
    items = [_to_dict(n) for n in r.scalars().all()]
    return {"items": items, "limit": limit, "offset": offset, "count": len(items)}


@router.get("/unread-count")
async def unread_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(func.count(P2PNotification.id)).where(
            P2PNotification.user_id == user.id,
            P2PNotification.is_read == False,  # noqa: E712
        )
    )
    return {"unread_count": int(r.scalar() or 0)}


@router.post("/{notif_id}/read")
async def mark_read(
    notif_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(P2PNotification).where(P2PNotification.id == notif_id))
    n = r.scalar_one_or_none()
    if not n:
        raise HTTPException(404, "notification not found")
    if n.user_id != user.id:
        raise HTTPException(403, "not yours")
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.now(timezone.utc)
        await db.commit()
    return _to_dict(n)


@router.post("/read-all")
async def mark_all_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    r = await db.execute(
        sql_update(P2PNotification)
        .where(P2PNotification.user_id == user.id, P2PNotification.is_read == False)  # noqa: E712
        .values(is_read=True, read_at=now)
    )
    await db.commit()
    return {"ok": True, "updated": r.rowcount or 0}


# Helper для использования из outbox publisher
async def create_notification(
    db: AsyncSession,
    *,
    user_id: int,
    type_: str,
    title: str,
    body: str | None = None,
    payload: dict | None = None,
    correlation_id: str | None = None,
) -> str:
    n = P2PNotification(
        user_id=user_id, type=type_, title=title[:256],
        body=body, payload=payload or {},
        correlation_id=correlation_id,
    )
    db.add(n)
    await db.flush()
    return n.id

"""P2P Disputes API (для участников сделки).

GET    /api/v2/p2p/disputes/{id}              — детали диспута (участник)
GET    /api/v2/p2p/disputes/{id}/evidence     — список attachments
POST   /api/v2/p2p/disputes/{id}/evidence     — загрузить файл-доказательство
POST   /api/v2/p2p/disputes/{id}/reopen       — переоткрыть RESOLVED диспут
"""
from __future__ import annotations
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import rbac
from p2p.api._deps import get_actor_role, get_idempotency_key
from p2p.models import P2PAttachment, P2PDispute, P2PMessage, P2PTrade
from p2p.orchestrator import run_workflow
from p2p.workflows import dispute_extras

logger = logging.getLogger("p2p.api.disputes")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-disputes"])


def _source(req: Request | None) -> str | None:
    if not req:
        return None
    ua = req.headers.get("user-agent", "")[:50]
    return f"miniapp:{ua}"


async def _load_dispute_and_trade(
    db: AsyncSession, dispute_id: str,
) -> tuple[P2PDispute, P2PTrade]:
    r = await db.execute(select(P2PDispute).where(P2PDispute.id == dispute_id))
    dispute = r.scalar_one_or_none()
    if not dispute:
        raise HTTPException(404, "dispute not found")
    tr = await db.execute(select(P2PTrade).where(P2PTrade.id == dispute.trade_id))
    trade = tr.scalar_one_or_none()
    if not trade:
        raise HTTPException(500, "trade missing for dispute")
    return dispute, trade


def _can_view_dispute(user: User, trade: P2PTrade, dispute: P2PDispute) -> bool:
    if user.id in (trade.buyer_id, trade.seller_id):
        return True
    if dispute.arbitrator_id and dispute.arbitrator_id == user.id:
        return True
    if rbac.is_arbitrator(user) or rbac.is_support(user):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# GET /disputes/{id}
# ═══════════════════════════════════════════════════════════════════════

@router.get("/disputes/{dispute_id}")
async def get_dispute(
    dispute_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dispute, trade = await _load_dispute_and_trade(db, dispute_id)
    if not _can_view_dispute(user, trade, dispute):
        raise HTTPException(403, "not a dispute participant")
    return {
        "id": dispute.id,
        "trade_id": dispute.trade_id,
        "opened_by_id": dispute.opened_by_id,
        "arbitrator_id": dispute.arbitrator_id,
        "status": dispute.status,
        "reason": dispute.reason,
        "description": dispute.description,
        "resolution": dispute.resolution,
        "resolution_note": dispute.resolution_note,
        "sla_deadline_at": dispute.sla_deadline_at.isoformat() if dispute.sla_deadline_at else None,
        "resolved_at": dispute.resolved_at.isoformat() if dispute.resolved_at else None,
        "closed_at": dispute.closed_at.isoformat() if dispute.closed_at else None,
        "created_at": dispute.created_at.isoformat() if dispute.created_at else None,
        "version": dispute.version,
        "trade": {
            "id": trade.id,
            "status": trade.status,
            "buyer_id": trade.buyer_id,
            "seller_id": trade.seller_id,
            "crypto_amount": str(trade.crypto_amount),
            "fiat_amount": str(trade.fiat_amount),
            "crypto": trade.crypto_currency,
            "fiat": trade.fiat_currency,
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# GET /disputes/{id}/evidence — список attachments из чата
# ═══════════════════════════════════════════════════════════════════════

@router.get("/disputes/{dispute_id}/evidence")
async def list_evidence(
    dispute_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dispute, trade = await _load_dispute_and_trade(db, dispute_id)
    if not _can_view_dispute(user, trade, dispute):
        raise HTTPException(403, "not a dispute participant")

    # Все сообщения с attachment_id в трейде
    q = (
        select(
            P2PMessage.id,
            P2PMessage.sender_id,
            P2PMessage.sequence_number,
            P2PMessage.message_type,
            P2PMessage.text,
            P2PMessage.attachment_id,
            P2PMessage.created_at,
            P2PAttachment.sha256,
            P2PAttachment.storage_key,
            P2PAttachment.preview_key,
            P2PAttachment.mime_type,
            P2PAttachment.file_size,
            P2PAttachment.file_name,
            P2PAttachment.virus_scan_status,
        )
        .select_from(P2PMessage)
        .join(P2PAttachment, P2PAttachment.id == P2PMessage.attachment_id)
        .where(P2PMessage.trade_id == trade.id)
        .where(P2PMessage.attachment_id.is_not(None))
        .order_by(P2PMessage.sequence_number.asc())
    )
    rows = (await db.execute(q)).all()
    items = []
    for row in rows:
        items.append({
            "message_id": row[0],
            "sender_id": row[1],
            "sequence_number": row[2],
            "message_type": row[3],
            "caption": row[4],
            "attachment_id": row[5],
            "created_at": row[6].isoformat() if row[6] else None,
            "sha256": row[7],
            "storage_key": row[8],
            "preview_key": row[9],
            "mime_type": row[10],
            "file_size": int(row[11] or 0),
            "file_name": row[12],
            "virus_scan_status": row[13],
        })
    return {
        "dispute_id": dispute_id,
        "trade_id": trade.id,
        "count": len(items),
        "items": items,
    }


# ═══════════════════════════════════════════════════════════════════════
# POST /disputes/{id}/evidence — загрузить файл (через workflow)
# ═══════════════════════════════════════════════════════════════════════

@router.post("/disputes/{dispute_id}/evidence")
async def upload_evidence_cmd(
    dispute_id: str,
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "dispute_id": dispute_id}
    return await run_workflow(
        db,
        workflow_type="upload_evidence",
        user_id=user.id,
        input_payload=input_p,
        handler=dispute_extras.upload_evidence,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req),
        endpoint=f"POST /p2p/disputes/{dispute_id}/evidence",
    )


# ═══════════════════════════════════════════════════════════════════════
# POST /disputes/{id}/reopen
# ═══════════════════════════════════════════════════════════════════════

@router.post("/disputes/{dispute_id}/reopen")
async def reopen_dispute_cmd(
    dispute_id: str,
    payload: dict[str, Any] | None = None,
    req: Request = None,  # type: ignore
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "dispute_id": dispute_id}
    return await run_workflow(
        db,
        workflow_type="reopen_dispute",
        user_id=user.id,
        input_payload=input_p,
        handler=dispute_extras.reopen_dispute,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req) if req else None,
        endpoint=f"POST /p2p/disputes/{dispute_id}/reopen",
    )

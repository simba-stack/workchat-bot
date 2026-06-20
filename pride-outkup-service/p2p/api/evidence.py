"""P2P Evidence Center aggregate endpoint (Том 27.5).

GET /api/v2/p2p/trades/{trade_id}/evidence-center
"""
from __future__ import annotations
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import rbac
from p2p.models import (
    P2PAttachment, P2PAuditLog, P2PDispute, P2PMessage,
    P2POutbox, P2PTrade,
)

logger = logging.getLogger("p2p.api.evidence")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-evidence"])


def _can_view(user: User, trade: P2PTrade, dispute: Optional[P2PDispute]) -> bool:
    if user.id in (trade.buyer_id, trade.seller_id):
        return True
    if dispute is not None and dispute.arbitrator_id == user.id:
        return True
    if rbac.is_arbitrator(user) or rbac.is_support(user) or rbac.is_admin(user):
        return True
    return False


@router.get("/trades/{trade_id}/evidence-center")
async def evidence_center(
    trade_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Агрегатор для Evidence Center UI: summary + timeline + messages +
    attachments + payment_snapshot + trade_snapshot + audit events.
    """
    tr = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = tr.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    dr = await db.execute(select(P2PDispute).where(P2PDispute.trade_id == trade_id))
    dispute = dr.scalar_one_or_none()

    if not _can_view(user, trade, dispute):
        raise HTTPException(403, "not allowed")

    is_priv = (
        rbac.is_arbitrator(user)
        or rbac.is_support(user)
        or rbac.is_admin(user)
        or (dispute and dispute.arbitrator_id == user.id)
    )

    # === SUMMARY ===
    summary = {
        "trade_id": trade.id,
        "trade_number": trade.trade_number,
        "buyer_id": trade.buyer_id,
        "seller_id": trade.seller_id,
        "status": trade.status,
        "opened_at": trade.created_at.isoformat() if trade.created_at else None,
        "closed_at": (
            trade.completed_at.isoformat() if trade.completed_at
            else trade.cancelled_at.isoformat() if trade.cancelled_at
            else None
        ),
        "dispute_status": dispute.status if dispute else None,
        "arbitrator_id": dispute.arbitrator_id if dispute else None,
    }

    # === TRADE SNAPSHOT (immutable fields) ===
    trade_snapshot = {
        "price": str(trade.price),
        "amount_crypto": str(trade.crypto_amount),
        "amount_fiat": str(trade.fiat_amount),
        "fiat": trade.fiat_currency,
        "crypto": trade.crypto_currency,
        "fee_pct": str(trade.fee_pct) if trade.fee_pct is not None else None,
        "fee_crypto": str(trade.fee_crypto) if trade.fee_crypto is not None else None,
        "payment_method_id": trade.payment_method_id,
        "advertisement_id": trade.advertisement_id,
        "version": trade.version,
    }

    # === TIMELINE (audit + outbox merged, chronological asc) ===
    aq = await db.execute(
        select(P2PAuditLog)
        .where(P2PAuditLog.entity_id == trade_id)
        .order_by(asc(P2PAuditLog.created_at))
        .limit(500)
    )
    audit_rows = list(aq.scalars().all())

    ox = await db.execute(
        select(P2POutbox)
        .where(P2POutbox.aggregate_type == "trade", P2POutbox.aggregate_id == trade_id)
        .order_by(asc(P2POutbox.created_at))
        .limit(500)
    )
    outbox_rows = list(ox.scalars().all())

    timeline: list[dict[str, Any]] = []
    for a in audit_rows:
        timeline.append({
            "ts": a.created_at.isoformat() if a.created_at else None,
            "source": "audit",
            "event": a.action,
            "actor": a.actor_id,
            "actor_role": a.actor_role,
            "description": a.action,
        })
    for o in outbox_rows:
        timeline.append({
            "ts": o.created_at.isoformat() if o.created_at else None,
            "source": "outbox",
            "event": o.event_type,
            "actor": None,
            "actor_role": None,
            "description": o.event_type,
            "payload": o.payload or {},
        })
    timeline.sort(key=lambda x: x.get("ts") or "")

    # === MESSAGES ===
    mq = await db.execute(
        select(P2PMessage)
        .where(P2PMessage.trade_id == trade_id)
        .order_by(asc(P2PMessage.sequence_number))
        .limit(1000)
    )
    msgs = list(mq.scalars().all())
    messages = [
        {
            "id": m.id,
            "sender_id": m.sender_id,
            "sequence_number": m.sequence_number,
            "message_type": m.message_type,
            "text": m.text,
            "attachment_id": m.attachment_id,
            "is_system": bool(m.is_system),
            "status": m.status,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in msgs
    ]

    # === ATTACHMENTS ===
    att_ids = [m.attachment_id for m in msgs if m.attachment_id]
    attachments = []
    if att_ids:
        rq = await db.execute(
            select(P2PAttachment).where(P2PAttachment.id.in_(att_ids))
        )
        for a in rq.scalars().all():
            attachments.append({
                "id": a.id,
                "sha256": a.sha256,
                "storage_key": a.storage_key,
                "preview_key": a.preview_key,
                "mime_type": a.mime_type,
                "file_size": int(a.file_size or 0),
                "file_name": a.file_name,
                "uploaded_by_id": a.uploaded_by_id,
                "virus_scan_status": a.virus_scan_status,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })

    # === SYSTEM EVENTS (subset of outbox для UI) ===
    system_events = [
        {
            "ts": o.created_at.isoformat() if o.created_at else None,
            "event_type": o.event_type,
            "payload": o.payload or {},
        }
        for o in outbox_rows
    ]

    # === AUDIT EVENTS (только для admin/arbitrator/support) ===
    audit_events = []
    if is_priv:
        audit_events = [
            {
                "id": a.id,
                "ts": a.created_at.isoformat() if a.created_at else None,
                "actor_id": a.actor_id,
                "actor_role": a.actor_role,
                "action": a.action,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "previous_state": a.previous_state,
                "new_state": a.new_state,
                "ip_address": a.ip_address,
                "source": a.source,
                "correlation_id": a.correlation_id,
                "workflow_id": a.workflow_id,
            }
            for a in audit_rows
        ]

    return {
        "trade_id": trade_id,
        "summary": summary,
        "trade_snapshot": trade_snapshot,
        "payment_snapshot": trade.payment_snapshot or {},
        "timeline": timeline,
        "messages": messages,
        "attachments": attachments,
        "system_events": system_events,
        "audit_events": audit_events,
        "viewer_role": rbac.resolve_role(user),
    }

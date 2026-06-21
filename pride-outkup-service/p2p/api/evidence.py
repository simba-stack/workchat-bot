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


# ═══════════════════════════════════════════════════════════════════════
# EXPORT: PDF / ZIP (Том 27.5 §4)
# ═══════════════════════════════════════════════════════════════════════
import asyncio as _asyncio
import hashlib as _hashlib
import io as _io
import json as _json
import os as _os
import zipfile as _zipfile
from datetime import datetime as _dt, timezone as _tz

from fastapi.responses import Response as _Response, StreamingResponse as _StreamingResponse


def _short_tid(trade_id: str) -> str:
    s = (trade_id or "").replace("-", "")
    return s[:8] or "trade"


async def _gather_evidence_data(
    db: AsyncSession,
    trade_id: str,
    user: User,
) -> dict:
    """Собрать всё что нужно для PDF/ZIP. Вызывает _can_view -> 403."""
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

    # Audit + Outbox
    aq = await db.execute(
        select(P2PAuditLog)
        .where(P2PAuditLog.entity_id == trade_id)
        .order_by(asc(P2PAuditLog.created_at))
        .limit(2000)
    )
    audit_rows = list(aq.scalars().all())

    ox = await db.execute(
        select(P2POutbox)
        .where(P2POutbox.aggregate_type == "trade", P2POutbox.aggregate_id == trade_id)
        .order_by(asc(P2POutbox.created_at))
        .limit(2000)
    )
    outbox_rows = list(ox.scalars().all())

    # Messages
    mq = await db.execute(
        select(P2PMessage)
        .where(P2PMessage.trade_id == trade_id)
        .order_by(asc(P2PMessage.sequence_number))
        .limit(5000)
    )
    msgs = list(mq.scalars().all())

    # Attachments
    att_ids = [m.attachment_id for m in msgs if m.attachment_id]
    attachments: list[P2PAttachment] = []
    if att_ids:
        rq = await db.execute(
            select(P2PAttachment).where(P2PAttachment.id.in_(att_ids))
        )
        attachments = list(rq.scalars().all())

    return {
        "trade": trade,
        "dispute": dispute,
        "is_priv": bool(is_priv),
        "audit_rows": audit_rows,
        "outbox_rows": outbox_rows,
        "messages": msgs,
        "attachments": attachments,
        "viewer_role": rbac.resolve_role(user),
        "generated_at": _dt.now(_tz.utc),
    }


# ─── PDF builder (sync, выносится в ThreadPoolExecutor) ───────────────

def _build_pdf(data: dict) -> bytes:
    """Сгенерировать PDF через reportlab. Возвращает bytes."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
        )
        from reportlab.pdfgen import canvas as _canvas
    except Exception as e:
        # Fallback — минимальный текстовый PDF-имитатор не делаем,
        # лучше явная ошибка, чем мусор.
        raise RuntimeError(f"reportlab not installed: {e}")

    trade: P2PTrade = data["trade"]
    dispute: Optional[P2PDispute] = data.get("dispute")
    msgs: list[P2PMessage] = data["messages"]
    attachments: list[P2PAttachment] = data["attachments"]
    audit_rows: list[P2PAuditLog] = data["audit_rows"]
    outbox_rows: list[P2POutbox] = data["outbox_rows"]
    is_priv: bool = data["is_priv"]
    gen_at: _dt = data["generated_at"]

    att_by_id = {a.id: a for a in attachments}
    short_tid = _short_tid(trade.id)

    buf = _io.BytesIO()

    def _footer(cv, doc):
        cv.saveState()
        cv.setFont("Helvetica", 8)
        cv.setFillColor(colors.grey)
        footer = (
            f"P2P Evidence Report  -  TradeID {trade.id}  -  "
            f"Generated UTC {gen_at.strftime('%Y-%m-%d %H:%M:%S')}  -  "
            f"Page {doc.page}"
        )
        cv.drawString(15 * mm, 10 * mm, footer)
        cv.restoreState()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=20 * mm,
        title=f"P2P Evidence {short_tid}",
        author="PRIDE P2P",
    )

    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10)
    mono = ParagraphStyle(
        "mono", parent=body, fontName="Courier", fontSize=8, leading=10,
    )

    def _safe(s: Any) -> str:
        if s is None:
            return ""
        try:
            return (
                str(s)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
        except Exception:
            return ""

    story: list = []

    # ─── COVER ──────────────────────────────────────────────────
    story.append(Paragraph("P2P Evidence Report", h1))
    story.append(Spacer(1, 6 * mm))

    cover_rows = [
        ["Trade ID", trade.id],
        ["Trade Number", trade.trade_number or ""],
        ["Status", trade.status],
        ["Buyer ID", str(trade.buyer_id)],
        ["Seller ID", str(trade.seller_id)],
        ["Crypto Amount", f"{trade.crypto_amount} {trade.crypto_currency}"],
        ["Fiat Amount", f"{trade.fiat_amount} {trade.fiat_currency}"],
        ["Price", f"{trade.price} {trade.fiat_currency}"],
        ["Created At (UTC)",
         trade.created_at.strftime("%Y-%m-%d %H:%M:%S") if trade.created_at else ""],
        ["Generated At (UTC)", gen_at.strftime("%Y-%m-%d %H:%M:%S")],
        ["Viewer Role", data.get("viewer_role") or ""],
    ]
    if dispute:
        cover_rows.append(["Dispute Status", dispute.status])
        cover_rows.append(["Arbitrator ID", str(dispute.arbitrator_id or "")])

    t = Table(cover_rows, colWidths=[55 * mm, 110 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    # ─── SECTION 1: TRADE SNAPSHOT ──────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("1. Trade Snapshot", h2))
    snap_rows = [
        ["Field", "Value"],
        ["Advertisement ID", trade.advertisement_id],
        ["Pricing", f"{trade.price} {trade.fiat_currency}"],
        ["Amount Crypto", f"{trade.crypto_amount} {trade.crypto_currency}"],
        ["Amount Fiat", f"{trade.fiat_amount} {trade.fiat_currency}"],
        ["Fee %", str(trade.fee_pct)],
        ["Fee Crypto", str(trade.fee_crypto)],
        ["Payment Method ID", str(trade.payment_method_id or "")],
        ["Pay Deadline (UTC)",
         trade.pay_deadline_at.strftime("%Y-%m-%d %H:%M:%S") if trade.pay_deadline_at else ""],
        ["Confirm Deadline (UTC)",
         trade.confirm_deadline_at.strftime("%Y-%m-%d %H:%M:%S") if trade.confirm_deadline_at else ""],
        ["Workflow ID", trade.workflow_id],
        ["Correlation ID", trade.correlation_id],
        ["Version", str(trade.version)],
    ]
    t2 = Table(snap_rows, colWidths=[55 * mm, 110 * mm])
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(t2)

    # ─── SECTION 2: PAYMENT SNAPSHOT ────────────────────────────
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("2. Payment Snapshot", h2))
    ps = trade.payment_snapshot or {}
    if ps:
        rows = [["Field", "Value"]]
        for k, v in ps.items():
            rows.append([_safe(k), _safe(v)[:200]])
        t3 = Table(rows, colWidths=[55 * mm, 110 * mm])
        t3.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t3)
    else:
        story.append(Paragraph("(no payment snapshot)", small))

    # ─── SECTION 3: TIMELINE ─────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("3. Timeline", h2))
    timeline = []
    for a in audit_rows:
        timeline.append((a.created_at, "audit", a.action, a.actor_id, a.actor_role))
    for o in outbox_rows:
        timeline.append((o.created_at, "outbox", o.event_type, None, None))
    timeline.sort(key=lambda x: x[0] or _dt.min.replace(tzinfo=_tz.utc))

    if timeline:
        rows = [["Timestamp (UTC)", "Source", "Event", "Actor", "Role"]]
        for ts, src, ev, actor, role in timeline:
            rows.append([
                ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "",
                src,
                _safe(ev)[:60],
                str(actor) if actor is not None else "",
                _safe(role)[:20] if role else "",
            ])
        tt = Table(rows, colWidths=[38 * mm, 18 * mm, 65 * mm, 20 * mm, 24 * mm])
        tt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(tt)
    else:
        story.append(Paragraph("(no timeline events)", small))

    # ─── SECTION 4: MESSAGES ────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("4. Chat Messages", h2))
    if msgs:
        for m in msgs:
            head = (
                f"<b>#{m.sequence_number}</b> "
                f"[{_safe(m.message_type)}] "
                f"sender={m.sender_id} "
                f"at {m.created_at.strftime('%Y-%m-%d %H:%M:%S') if m.created_at else ''}"
            )
            story.append(Paragraph(head, small))
            if m.text:
                story.append(Paragraph(_safe(m.text)[:2000], body))
            if m.attachment_id:
                att = att_by_id.get(m.attachment_id)
                if att:
                    info = (
                        f"attachment_id={att.id} sha256={att.sha256} "
                        f"size={att.file_size} mime={att.mime_type} "
                        f"name={_safe(att.file_name or '')}"
                    )
                else:
                    info = f"attachment_id={m.attachment_id} (metadata missing)"
                story.append(Paragraph(info, mono))
            story.append(Spacer(1, 2 * mm))
    else:
        story.append(Paragraph("(no messages)", small))

    # ─── SECTION 5: SYSTEM EVENTS + AUDIT ────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("5. System Events", h2))
    if outbox_rows:
        rows = [["Timestamp (UTC)", "Event Type", "Status"]]
        for o in outbox_rows:
            rows.append([
                o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
                _safe(o.event_type)[:60],
                _safe(o.status),
            ])
        ts2 = Table(rows, colWidths=[40 * mm, 90 * mm, 30 * mm])
        ts2.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
        ]))
        story.append(ts2)
    else:
        story.append(Paragraph("(no system events)", small))

    if is_priv and audit_rows:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Audit Log (privileged view)", h2))
        rows = [["Timestamp (UTC)", "Actor", "Role", "Action", "Source"]]
        for a in audit_rows:
            rows.append([
                a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
                str(a.actor_id) if a.actor_id is not None else "",
                _safe(a.actor_role)[:16],
                _safe(a.action)[:60],
                _safe(a.source)[:20],
            ])
        ta = Table(rows, colWidths=[38 * mm, 18 * mm, 22 * mm, 65 * mm, 22 * mm])
        ta.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
        ]))
        story.append(ta)
    elif not is_priv:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(
            "(Audit log hidden — privileged role required)", small,
        ))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


# ─── ZIP builder (sync) ────────────────────────────────────────────────

def _build_zip(data: dict, storage_base: str) -> bytes:
    """Собрать ZIP с PDF + JSON + attachments. Возвращает bytes."""
    trade: P2PTrade = data["trade"]
    msgs: list[P2PMessage] = data["messages"]
    attachments: list[P2PAttachment] = data["attachments"]
    audit_rows: list[P2PAuditLog] = data["audit_rows"]
    outbox_rows: list[P2POutbox] = data["outbox_rows"]
    is_priv: bool = data["is_priv"]
    gen_at: _dt = data["generated_at"]

    def _ts(x):
        return x.isoformat() if x else None

    timeline_json = []
    for a in audit_rows:
        timeline_json.append({
            "ts": _ts(a.created_at),
            "source": "audit",
            "event": a.action,
            "actor_id": a.actor_id,
            "actor_role": a.actor_role,
        })
    for o in outbox_rows:
        timeline_json.append({
            "ts": _ts(o.created_at),
            "source": "outbox",
            "event": o.event_type,
            "status": o.status,
        })
    timeline_json.sort(key=lambda x: x.get("ts") or "")

    messages_json = [
        {
            "id": m.id,
            "sequence_number": m.sequence_number,
            "sender_id": m.sender_id,
            "message_type": m.message_type,
            "text": m.text,
            "attachment_id": m.attachment_id,
            "is_system": bool(m.is_system),
            "status": m.status,
            "created_at": _ts(m.created_at),
        }
        for m in msgs
    ]

    snapshots_json = {
        "trade": {
            "id": trade.id,
            "trade_number": trade.trade_number,
            "status": trade.status,
            "buyer_id": trade.buyer_id,
            "seller_id": trade.seller_id,
            "advertisement_id": trade.advertisement_id,
            "crypto_currency": trade.crypto_currency,
            "fiat_currency": trade.fiat_currency,
            "price": str(trade.price),
            "crypto_amount": str(trade.crypto_amount),
            "fiat_amount": str(trade.fiat_amount),
            "fee_pct": str(trade.fee_pct),
            "fee_crypto": str(trade.fee_crypto),
            "payment_method_id": trade.payment_method_id,
            "workflow_id": trade.workflow_id,
            "correlation_id": trade.correlation_id,
            "version": trade.version,
            "created_at": _ts(trade.created_at),
            "pay_deadline_at": _ts(trade.pay_deadline_at),
            "confirm_deadline_at": _ts(trade.confirm_deadline_at),
            "completed_at": _ts(trade.completed_at),
            "cancelled_at": _ts(trade.cancelled_at),
            "cancelled_reason": trade.cancelled_reason,
        },
        "payment": trade.payment_snapshot or {},
    }

    audit_json = []
    if is_priv:
        for a in audit_rows:
            audit_json.append({
                "id": a.id,
                "ts": _ts(a.created_at),
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
            })

    # 1. Сначала собираем PDF, чтобы он попал в архив с хешем
    pdf_bytes = _build_pdf(data)

    buf = _io.BytesIO()
    manifest: list[dict] = []

    def _add(zf: _zipfile.ZipFile, arcname: str, raw: bytes):
        zf.writestr(arcname, raw)
        manifest.append({
            "path": arcname,
            "size": len(raw),
            "sha256": _hashlib.sha256(raw).hexdigest(),
        })

    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        _add(zf, "summary.pdf", pdf_bytes)
        _add(
            zf, "timeline.json",
            _json.dumps(timeline_json, ensure_ascii=False, indent=2).encode(),
        )
        _add(
            zf, "messages.json",
            _json.dumps(messages_json, ensure_ascii=False, indent=2).encode(),
        )
        _add(
            zf, "snapshots.json",
            _json.dumps(snapshots_json, ensure_ascii=False, indent=2).encode(),
        )
        if is_priv:
            _add(
                zf, "audit.json",
                _json.dumps(audit_json, ensure_ascii=False, indent=2).encode(),
            )

        # attachments/<sha256>.<ext>
        for att in attachments:
            try:
                key = (att.storage_key or "").replace("\\", "/")
                if not key or ".." in key.split("/") or key.startswith("/"):
                    continue
                src = _os.path.abspath(_os.path.join(storage_base, key))
                if not _os.path.isfile(src):
                    continue
                ext = _os.path.splitext(key)[1] or ".bin"
                arc = f"attachments/{att.sha256}{ext}"
                with open(src, "rb") as fh:
                    raw = fh.read()
                _add(zf, arc, raw)
            except Exception as e:
                # пропускаем сломанный файл, но фиксируем в manifest
                manifest.append({
                    "path": f"attachments/{att.sha256}",
                    "error": str(e)[:200],
                })

        # manifest LAST (содержит хеши всего что собрали)
        meta = {
            "trade_id": trade.id,
            "trade_number": trade.trade_number,
            "generated_at": gen_at.isoformat(),
            "is_privileged_view": is_priv,
            "files": manifest,
        }
        zf.writestr(
            "manifest.json",
            _json.dumps(meta, ensure_ascii=False, indent=2).encode(),
        )

    return buf.getvalue()


# ─── PDF endpoint ─────────────────────────────────────────────────────

@router.get("/trades/{trade_id}/evidence/export/pdf")
async def export_evidence_pdf(
    trade_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сгенерировать и отдать PDF-evidence по сделке."""
    data = await _gather_evidence_data(db, trade_id, user)

    loop = _asyncio.get_event_loop()
    try:
        pdf_bytes = await loop.run_in_executor(None, _build_pdf, data)
    except RuntimeError as e:
        logger.error("[evidence.pdf] %s", e)
        raise HTTPException(503, "PDF generation unavailable (reportlab missing)")
    except Exception as e:
        logger.exception("[evidence.pdf] failed: %s", e)
        raise HTTPException(500, "PDF generation failed")

    fname = f"evidence_{_short_tid(trade_id)}.pdf"
    return _Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "private, no-store",
        },
    )


# ─── ZIP endpoint ─────────────────────────────────────────────────────

@router.get("/trades/{trade_id}/evidence/export/zip")
async def export_evidence_zip(
    trade_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сгенерировать и отдать ZIP-evidence (PDF + JSON + файлы)."""
    data = await _gather_evidence_data(db, trade_id, user)

    # storage_base разрешаем через env (как в attachments.py)
    storage_base = _os.environ.get("STORAGE_PATH") or _os.path.join(
        _os.getcwd(), "storage", "p2p",
    )

    loop = _asyncio.get_event_loop()
    try:
        zip_bytes = await loop.run_in_executor(
            None, _build_zip, data, storage_base,
        )
    except RuntimeError as e:
        logger.error("[evidence.zip] %s", e)
        raise HTTPException(503, "ZIP generation unavailable (reportlab missing)")
    except Exception as e:
        logger.exception("[evidence.zip] failed: %s", e)
        raise HTTPException(500, "ZIP generation failed")

    fname = f"evidence_{_short_tid(trade_id)}.zip"
    return _Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "private, no-store",
        },
    )

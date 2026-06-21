"""Dispute extras workflows (ТЗ Том 10).

- upload_evidence: загрузить файл-доказательство в диспут (через P2PAttachment + P2PMessage)
- reopen_dispute: переоткрыть RESOLVED диспут в течение 24h
"""
from __future__ import annotations
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select

from p2p import audit, locks, outbox, state
from p2p.enums import (
    DisputeStatus, EventType, MessageType, TradeStatus,
)
from p2p.models import P2PAttachment, P2PDispute, P2PMessage, P2PTrade
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.dispute_extras")


# Лимит файлов на участника диспута
MAX_EVIDENCE_PER_USER = 10
# Окно переоткрытия диспута
REOPEN_WINDOW_HOURS = 24


# ═══════════════════════════════════════════════════════════════════════
# upload_evidence
# ═══════════════════════════════════════════════════════════════════════

async def upload_evidence(ctx: WorkflowContext) -> dict:
    """Загрузить файл-доказательство к диспуту.

    Input:
        dispute_id, file_url, file_type (mime), caption (optional)

    Доступ: участники сделки + арбитр (для DISPUTE_OPENED / ARBITRATION).
    Лимит: MAX_EVIDENCE_PER_USER файлов на участника на диспут.

    Реализация: создаём P2PAttachment (sha256 из file_url+caption — суррогат),
    затем системное P2PMessage с attachment_id и message_type=DOCUMENT/IMAGE,
    привязанным к trade_id диспута. emit CHAT_FILE_UPLOADED.
    """
    p = ctx.input_payload
    db = ctx.db

    dispute_id = p.get("dispute_id")
    file_url = (p.get("file_url") or "").strip()
    file_type = (p.get("file_type") or "application/octet-stream").strip()[:64]
    caption = (p.get("caption") or "")[:500]
    file_size = int(p.get("file_size") or 0)
    file_name = (p.get("file_name") or "")[:256] or None

    if not dispute_id or not file_url:
        raise HTTPException(422, "dispute_id and file_url required")
    if len(file_url) > 512:
        raise HTTPException(422, "file_url too long (max 512)")

    # Найти dispute + lock
    await locks.lock_dispute(db, dispute_id)
    r = await db.execute(select(P2PDispute).where(P2PDispute.id == dispute_id))
    dispute = r.scalar_one_or_none()
    if not dispute:
        raise HTTPException(404, "dispute not found")

    if dispute.status not in (DisputeStatus.OPENED.value, DisputeStatus.ARBITRATION.value):
        raise HTTPException(409, f"cannot upload evidence in dispute status {dispute.status}")

    # Найти trade
    tr = await db.execute(select(P2PTrade).where(P2PTrade.id == dispute.trade_id))
    trade = tr.scalar_one_or_none()
    if not trade:
        raise HTTPException(500, "trade missing for dispute")

    # Проверка участия (участники сделки или арбитр)
    is_participant = ctx.user_id in (trade.buyer_id, trade.seller_id)
    is_arbiter = dispute.arbitrator_id and dispute.arbitrator_id == ctx.user_id
    from p2p import rbac
    from core.models import User
    ur = await db.execute(select(User).where(User.id == ctx.user_id))
    u = ur.scalar_one_or_none()
    has_arbiter_role = bool(u and rbac.is_arbitrator(u))

    if not (is_participant or is_arbiter or has_arbiter_role):
        raise HTTPException(403, "not a dispute participant")

    # Лимит файлов: считаем сколько уже attachments залил этот юзер в трейд после открытия диспута
    cnt_r = await db.execute(
        select(func.count(P2PMessage.id)).where(
            P2PMessage.trade_id == trade.id,
            P2PMessage.sender_id == ctx.user_id,
            P2PMessage.attachment_id.is_not(None),
        )
    )
    existing_cnt = int(cnt_r.scalar() or 0)
    if existing_cnt >= MAX_EVIDENCE_PER_USER:
        raise HTTPException(
            429,
            f"evidence limit reached ({existing_cnt}/{MAX_EVIDENCE_PER_USER})",
        )

    # Создаём P2PAttachment. sha256 = hash от file_url+caption (суррогат пока
    # реальной интеграции хранилища нет — будет переписано когда появится S3).
    sha_input = f"{file_url}|{caption}|{ctx.user_id}|{datetime.now(timezone.utc).isoformat()}"
    sha256_val = hashlib.sha256(sha_input.encode("utf-8")).hexdigest()

    # Определяем message_type по mime
    if file_type.startswith("image/"):
        msg_type = MessageType.IMAGE.value
    elif file_type.startswith("video/"):
        msg_type = MessageType.VIDEO.value
    else:
        msg_type = MessageType.DOCUMENT.value

    attachment = P2PAttachment(
        sha256=sha256_val,
        storage_key=file_url[:512],
        mime_type=file_type,
        file_size=max(file_size, 0),
        file_name=file_name,
        uploaded_by_id=ctx.user_id,
        virus_scan_status="PENDING",
    )
    db.add(attachment)
    await db.flush()

    # Создаём системное сообщение со ссылкой на attachment
    await locks.advisory_lock(db, f"chat:{trade.id}")
    rseq = await db.execute(
        select(func.coalesce(func.max(P2PMessage.sequence_number), 0))
        .where(P2PMessage.trade_id == trade.id)
    )
    next_seq = int(rseq.scalar() or 0) + 1

    msg = P2PMessage(
        trade_id=trade.id,
        sender_id=ctx.user_id,
        sequence_number=next_seq,
        message_type=msg_type,
        text=caption or None,
        attachment_id=attachment.id,
        is_system=False,
        status="SENT",
    )
    db.add(msg)
    await db.flush()

    await audit.log(
        db,
        action="dispute.evidence_uploaded",
        entity_type="dispute",
        entity_id=dispute.id,
        actor_id=ctx.user_id,
        actor_role=ctx.actor_role,
        new_state={
            "attachment_id": attachment.id,
            "message_id": msg.id,
            "trade_id": trade.id,
            "file_type": file_type,
            "file_size": file_size,
            "caption": caption[:200],
        },
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.CHAT_FILE_UPLOADED.value,
        payload={
            "attachment_id": attachment.id,
            "message_id": msg.id,
            "dispute_id": dispute.id,
            "trade_id": trade.id,
            "buyer_id": trade.buyer_id,
            "seller_id": trade.seller_id,
            "uploaded_by_id": ctx.user_id,
            "file_type": file_type,
        },
        aggregate_type="dispute",
        aggregate_id=dispute.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "attachment_id": attachment.id,
        "message_id": msg.id,
        "dispute_id": dispute.id,
        "trade_id": trade.id,
        "sequence_number": next_seq,
    }


# ═══════════════════════════════════════════════════════════════════════
# reopen_dispute
# ═══════════════════════════════════════════════════════════════════════

async def reopen_dispute(ctx: WorkflowContext) -> dict:
    """Переоткрыть RESOLVED диспут в течение REOPEN_WINDOW_HOURS.

    Input: dispute_id, reason

    Только участники сделки, только status=RESOLVED, только если
    resolved_at + 24h > now. Reset → OPENED, очищаем arbitrator/resolution/resolved_at.
    """
    p = ctx.input_payload
    db = ctx.db

    dispute_id = p.get("dispute_id")
    reason = (p.get("reason") or "").strip()[:500]
    if not dispute_id or not reason:
        raise HTTPException(422, "dispute_id and reason required")

    await locks.lock_dispute(db, dispute_id)
    r = await db.execute(select(P2PDispute).where(P2PDispute.id == dispute_id))
    dispute = r.scalar_one_or_none()
    if not dispute:
        raise HTTPException(404, "dispute not found")

    if dispute.status != DisputeStatus.RESOLVED.value:
        raise HTTPException(409, f"can reopen only RESOLVED disputes (current: {dispute.status})")

    if not dispute.resolved_at:
        raise HTTPException(409, "dispute has no resolved_at timestamp")

    now = datetime.now(timezone.utc)
    resolved_at = dispute.resolved_at
    if resolved_at.tzinfo is None:
        resolved_at = resolved_at.replace(tzinfo=timezone.utc)
    if (now - resolved_at) > timedelta(hours=REOPEN_WINDOW_HOURS):
        raise HTTPException(
            410,
            f"reopen window expired ({REOPEN_WINDOW_HOURS}h after resolution)",
        )

    # Найти trade, проверить участие
    await locks.lock_trade(db, dispute.trade_id)
    tr = await db.execute(select(P2PTrade).where(P2PTrade.id == dispute.trade_id))
    trade = tr.scalar_one_or_none()
    if not trade:
        raise HTTPException(500, "trade missing for dispute")
    if ctx.user_id not in (trade.buyer_id, trade.seller_id):
        raise HTTPException(403, "only trade participants can reopen dispute")

    prev_resolution = dispute.resolution
    prev_arbitrator = dispute.arbitrator_id
    prev_trade_status = trade.status

    # Reset dispute через state matrix (TODO #9: RESOLVED → OPENED разрешён в _DISPUTE_ALLOWED)
    state.assert_dispute_transition(dispute.status, DisputeStatus.OPENED.value)
    dispute.status = DisputeStatus.OPENED.value
    dispute.arbitrator_id = None
    dispute.resolution = None
    dispute.resolved_at = None
    dispute.resolution_note = (
        (dispute.resolution_note or "") + f"\n[reopened by user {ctx.user_id}]: {reason}"
    )[:4000]
    dispute.version = (dispute.version or 0) + 1

    # Если trade был COMPLETED/CANCELLED — оставляем как есть (ledger трогать опасно).
    # Если trade ещё в RESOLVED — переводим в DISPUTE_OPENED через state matrix.
    if trade.status == TradeStatus.RESOLVED.value:
        # TODO #9: матрица расширена — RESOLVED → DISPUTE_OPENED это путь reopen.
        state.assert_trade_transition(trade.status, TradeStatus.DISPUTE_OPENED.value)
        trade.status = TradeStatus.DISPUTE_OPENED.value
        trade.version = (trade.version or 0) + 1

    await db.flush()

    await audit.log(
        db,
        action="dispute.reopened",
        entity_type="dispute",
        entity_id=dispute.id,
        actor_id=ctx.user_id,
        actor_role=ctx.actor_role,
        previous_state={
            "status": DisputeStatus.RESOLVED.value,
            "resolution": prev_resolution,
            "arbitrator_id": prev_arbitrator,
            "trade_status": prev_trade_status,
        },
        new_state={
            "status": dispute.status,
            "reason": reason,
            "trade_status": trade.status,
        },
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )
    await outbox.emit(
        db,
        event_type=EventType.DISPUTE_OPENED.value,
        payload={
            "dispute_id": dispute.id,
            "trade_id": trade.id,
            "reopened": True,
            "reason": reason,
            "buyer_id": trade.buyer_id,
            "seller_id": trade.seller_id,
            "opened_by_id": ctx.user_id,
        },
        aggregate_type="dispute",
        aggregate_id=dispute.id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "dispute_id": dispute.id,
        "trade_id": trade.id,
        "status": dispute.status,
        "trade_status": trade.status,
        "reopened": True,
    }

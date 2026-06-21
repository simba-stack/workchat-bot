"""P2P Admin/Arbitrator API.

Эндпоинты для арбитра/админа: resolve dispute, list disputes, request-evidence.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import audit, locks, outbox, rbac
from p2p.api._deps import get_idempotency_key
from p2p.api.notifications import create_notification
from p2p.enums import EventType, MessageType, P2PUserRole, DisputeStatus
from p2p.models import P2PDispute, P2PMessage, P2PTrade
from p2p.orchestrator import run_workflow
from p2p.workflows import resolve_dispute

logger = logging.getLogger("p2p.api.admin")
router = APIRouter(prefix="/api/v2/p2p/admin", tags=["p2p-admin"])


@router.get("/disputes")
async def list_disputes(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(rbac.require_role(
        P2PUserRole.ADMIN.value,
        P2PUserRole.ARBITRATOR.value,
        P2PUserRole.SUPPORT.value,
    )),
    db: AsyncSession = Depends(get_db),
):
    q = select(P2PDispute)
    if status:
        q = q.where(P2PDispute.status == status.upper())
    q = q.order_by(desc(P2PDispute.created_at)).limit(limit)
    r = await db.execute(q)
    items = []
    for d in r.scalars().all():
        items.append({
            "id": d.id,
            "trade_id": d.trade_id,
            "opened_by_id": d.opened_by_id,
            "reason": d.reason,
            "status": d.status,
            "resolution": d.resolution,
            "arbitrator_id": d.arbitrator_id,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
        })
    return {"items": items}


@router.post("/disputes/{dispute_id}/resolve")
async def resolve_dispute_cmd(
    dispute_id: str,
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(rbac.require_role(
        P2PUserRole.ARBITRATOR.value,
        P2PUserRole.ADMIN.value,
    )),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "dispute_id": dispute_id}
    return await run_workflow(
        db,
        workflow_type="resolve_dispute",
        user_id=user.id,
        input_payload=input_p,
        handler=resolve_dispute.handle,
        idempotency_key=idempotency_key,
        actor_role=rbac.resolve_role(user),
        source=f"admin:{req.headers.get('user-agent','')[:50]}",
        endpoint=f"POST /p2p/admin/disputes/{dispute_id}/resolve",
    )


@router.post("/disputes/{dispute_id}/request-evidence")
async def request_evidence(
    dispute_id: str,
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(rbac.require_role(
        P2PUserRole.ARBITRATOR.value,
        P2PUserRole.ADMIN.value,
        P2PUserRole.SUPPORT.value,
    )),
    db: AsyncSession = Depends(get_db),
):
    """Арбитр запрашивает доп. доказательства у участников диспута.

    Действие:
      1. Создаёт SYSTEM сообщение в чате трейда (с reason и deadline).
      2. Создаёт notifications для buyer и seller.
      3. Пишет audit + emit event.
    """
    reason = (payload.get("reason") or "").strip()
    try:
        deadline_hours = int(payload.get("deadline_hours") or 24)
    except (TypeError, ValueError):
        raise HTTPException(422, "deadline_hours must be int")
    if not reason:
        raise HTTPException(422, "reason required")
    if deadline_hours < 1 or deadline_hours > 168:
        raise HTTPException(422, "deadline_hours must be 1..168")

    dr = await db.execute(select(P2PDispute).where(P2PDispute.id == dispute_id))
    dispute = dr.scalar_one_or_none()
    if not dispute:
        raise HTTPException(404, "dispute not found")

    tr = await db.execute(select(P2PTrade).where(P2PTrade.id == dispute.trade_id))
    trade = tr.scalar_one_or_none()
    if not trade:
        raise HTTPException(500, "trade missing for dispute")

    now = datetime.now(timezone.utc)
    deadline_at = now + timedelta(hours=deadline_hours)

    # SYSTEM message в чате
    await locks.advisory_lock(db, f"chat:{trade.id}")
    rseq = await db.execute(
        select(func.coalesce(func.max(P2PMessage.sequence_number), 0))
        .where(P2PMessage.trade_id == trade.id)
    )
    next_seq = int(rseq.scalar() or 0) + 1

    sys_text = (
        f"[Arbitrator] Запрос доказательств: {reason}\n"
        f"Срок ответа: до {deadline_at.isoformat()} (через {deadline_hours} ч)."
    )
    msg = P2PMessage(
        trade_id=trade.id,
        sender_id=user.id,
        sequence_number=next_seq,
        message_type=MessageType.SYSTEM.value,
        text=sys_text,
        is_system=True,
        status="SENT",
    )
    db.add(msg)
    await db.flush()

    # Notifications обеим сторонам
    for uid in (trade.buyer_id, trade.seller_id):
        try:
            await create_notification(
                db,
                user_id=uid,
                type_="dispute.evidence_requested",
                title="Запрос доказательств по спору",
                body=reason,
                payload={
                    "trade_id": trade.id,
                    "dispute_id": dispute.id,
                    "deadline_at": deadline_at.isoformat(),
                    "deadline_hours": deadline_hours,
                },
            )
        except Exception as e:
            logger.warning("[request_evidence] notify failed for %s: %s", uid, e)

    await audit.log(
        db,
        action="dispute.evidence_requested",
        entity_type="dispute",
        entity_id=dispute.id,
        actor_id=user.id,
        actor_role=rbac.resolve_role(user),
        new_state={
            "reason": reason,
            "deadline_hours": deadline_hours,
            "deadline_at": deadline_at.isoformat(),
            "trade_id": trade.id,
        },
        source=f"admin:{req.headers.get('user-agent','')[:50]}",
    )

    try:
        await outbox.emit(
            db,
            event_type="DisputeEvidenceRequested",
            payload={
                "dispute_id": dispute.id,
                "trade_id": trade.id,
                "buyer_id": trade.buyer_id,
                "seller_id": trade.seller_id,
                "reason": reason,
                "deadline_at": deadline_at.isoformat(),
            },
            aggregate_type="dispute",
            aggregate_id=dispute.id,
        )
    except Exception as e:
        logger.warning("[request_evidence] outbox emit failed: %s", e)

    await db.commit()

    return {
        "ok": True,
        "dispute_id": dispute.id,
        "trade_id": trade.id,
        "message_id": msg.id,
        "sequence_number": next_seq,
        "deadline_at": deadline_at.isoformat(),
    }


@router.post("/policies/reload")
async def reload_policies(
    user: User = Depends(rbac.require_role(
        P2PUserRole.ADMIN.value,
        P2PUserRole.SUPER_ADMIN.value,
    )),
    db: AsyncSession = Depends(get_db),
):
    """TODO #8: Форсированная перезагрузка PolicyEngine кеша.

    После изменения p2p_policies в БД admin зовёт этот endpoint,
    чтобы все live-инстансы тут же увидели новые значения.
    """
    from p2p import policies as _pol
    _pol.reload_cache()
    try:
        await _pol._reload_cache(db)
        keys_count = len(_pol._cache.data)
    except Exception as e:
        logger.warning("[admin] policies reload failed: %s", e)
        keys_count = 0
    await audit.log(
        db,
        action="admin.policies_reload",
        entity_type="policies",
        entity_id=None,
        actor_id=user.id,
        actor_role=rbac.resolve_role(user),
        new_state={"keys_loaded": keys_count},
    )
    await db.commit()
    return {
        "ok": True,
        "keys_loaded": keys_count,
        "reloaded_at": datetime.now(timezone.utc).isoformat(),
    }

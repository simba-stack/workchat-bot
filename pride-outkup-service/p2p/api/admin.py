"""P2P Admin/Arbitrator API.

Эндпоинты для арбитра/админа: resolve dispute, list disputes, ручная корректировка.
"""
from __future__ import annotations
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import rbac
from p2p.api._deps import get_idempotency_key
from p2p.enums import P2PUserRole, DisputeStatus
from p2p.models import P2PDispute, P2PTrade
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

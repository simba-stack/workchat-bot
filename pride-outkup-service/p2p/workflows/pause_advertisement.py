"""Workflow: pause_advertisement — ACTIVE → PAUSED. Escrow не трогаем (висит в ad_hold)."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, locks, outbox, state
from p2p.enums import AdvertisementStatus, EventType
from p2p.models import P2PAdvertisement
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.pause_ad")


async def handle(ctx: WorkflowContext) -> dict:
    p = ctx.input_payload
    db = ctx.db
    ad_id = p.get("advertisement_id")
    reason = (p.get("reason") or "manual")[:120]
    if not ad_id:
        raise HTTPException(422, "advertisement_id required")

    await locks.lock_advertisement(db, ad_id)
    r = await db.execute(select(P2PAdvertisement).where(P2PAdvertisement.id == ad_id))
    ad = r.scalar_one_or_none()
    if not ad:
        raise HTTPException(404, "advertisement not found")
    # SYSTEM-инициированная пауза разрешена (auto-pause empty balance)
    if ctx.actor_role != "SYSTEM" and ad.owner_id != ctx.user_id:
        raise HTTPException(403, "not the owner")

    if ad.status == AdvertisementStatus.PAUSED.value:
        return {"ok": True, "advertisement_id": ad_id, "already_paused": True}

    prev = ad.status
    state.assert_advertisement_transition(prev, AdvertisementStatus.PAUSED.value)
    ad.status = AdvertisementStatus.PAUSED.value
    ad.paused_reason = reason
    ad.paused_at = datetime.now(timezone.utc)
    ad.version += 1
    await db.flush()

    await audit.log(
        db, action="advertisement.paused", entity_type="advertisement", entity_id=ad_id,
        actor_id=ctx.user_id, actor_role=ctx.actor_role,
        previous_state={"status": prev}, new_state={"status": ad.status, "reason": reason},
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id, source=ctx.source,
    )
    await outbox.emit(
        db, event_type=EventType.ADVERTISEMENT_PAUSED.value,
        payload={"advertisement_id": ad_id, "owner_id": ad.owner_id, "reason": reason},
        aggregate_type="advertisement", aggregate_id=ad_id,
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id,
    )
    return {"ok": True, "advertisement_id": ad_id, "status": ad.status}

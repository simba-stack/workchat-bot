"""Workflow: delete_advertisement — soft-delete (ARCHIVED → DELETED). Без escrow операций (он уже отпущен)."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, locks, outbox, state
from p2p.enums import AdvertisementStatus, EventType
from p2p.models import P2PAdvertisement
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.delete_ad")


async def handle(ctx: WorkflowContext) -> dict:
    p = ctx.input_payload
    db = ctx.db
    ad_id = p.get("advertisement_id")
    if not ad_id:
        raise HTTPException(422, "advertisement_id required")

    await locks.lock_advertisement(db, ad_id)
    r = await db.execute(select(P2PAdvertisement).where(P2PAdvertisement.id == ad_id))
    ad = r.scalar_one_or_none()
    if not ad:
        raise HTTPException(404, "advertisement not found")
    if ad.owner_id != ctx.user_id and ctx.actor_role not in ("ADMIN", "SUPER_ADMIN"):
        raise HTTPException(403, "not the owner")

    prev = ad.status
    state.assert_advertisement_transition(prev, AdvertisementStatus.DELETED.value)
    ad.status = AdvertisementStatus.DELETED.value
    ad.deleted_at = datetime.now(timezone.utc)
    ad.version += 1
    await db.flush()

    await audit.log(
        db, action="advertisement.deleted", entity_type="advertisement", entity_id=ad_id,
        actor_id=ctx.user_id, actor_role=ctx.actor_role,
        previous_state={"status": prev}, new_state={"status": ad.status},
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id, source=ctx.source,
    )
    await outbox.emit(
        db, event_type=EventType.ADVERTISEMENT_DELETED.value,
        payload={"advertisement_id": ad_id, "owner_id": ad.owner_id},
        aggregate_type="advertisement", aggregate_id=ad_id,
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id,
    )
    return {"ok": True, "advertisement_id": ad_id, "status": ad.status}

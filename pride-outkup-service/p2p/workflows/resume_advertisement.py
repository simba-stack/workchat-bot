"""Workflow: resume_advertisement — PAUSED → ACTIVE."""
from __future__ import annotations
import logging

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, locks, outbox, state, wallet
from p2p.enums import AdvertisementStatus, EventType, AdvertisementType
from p2p.models import P2PAdvertisement
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.resume_ad")


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
    if ad.owner_id != ctx.user_id and ctx.actor_role != "SYSTEM":
        raise HTTPException(403, "not the owner")

    if ad.status == AdvertisementStatus.ACTIVE.value:
        return {"ok": True, "advertisement_id": ad_id, "already_active": True}

    # Если SELL — убедиться что escrow ещё есть (advertisement_hold >= amount_available)
    if ad.type == AdvertisementType.SELL.value:
        b = await wallet.get_breakdown(db, ad.owner_id, ad.crypto_currency)
        if b.advertisement_hold <= 0 and ad.available_amount > 0:
            raise HTTPException(409, "ad_hold balance is zero — cannot resume")

    prev = ad.status
    state.assert_advertisement_transition(prev, AdvertisementStatus.ACTIVE.value)
    ad.status = AdvertisementStatus.ACTIVE.value
    ad.paused_reason = None
    ad.paused_at = None
    ad.version += 1
    await db.flush()

    await audit.log(
        db, action="advertisement.resumed", entity_type="advertisement", entity_id=ad_id,
        actor_id=ctx.user_id, actor_role=ctx.actor_role,
        previous_state={"status": prev}, new_state={"status": ad.status},
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id, source=ctx.source,
    )
    await outbox.emit(
        db, event_type=EventType.ADVERTISEMENT_RESUMED.value,
        payload={"advertisement_id": ad_id, "owner_id": ad.owner_id},
        aggregate_type="advertisement", aggregate_id=ad_id,
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id,
    )
    return {"ok": True, "advertisement_id": ad_id, "status": ad.status}

"""Workflow: archive_advertisement — снять с публикации, вернуть остаток escrow в available.

ACTIVE/PAUSED → ARCHIVED. Если есть amount_available — release_ad_hold_to_available.
"""
from __future__ import annotations
import logging
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, ledger, locks, outbox, state, wallet
from p2p.enums import AdvertisementStatus, AdvertisementType, EventType
from p2p.models import P2PAdvertisement
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.archive_ad")


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
    if ad.reserved_amount > 0:
        raise HTTPException(409, f"cannot archive: {ad.reserved_amount} reserved in active trades")

    prev = ad.status
    if prev == AdvertisementStatus.ARCHIVED.value:
        return {"ok": True, "advertisement_id": ad_id, "already_archived": True}

    # Release остаток
    if ad.type == AdvertisementType.SELL.value and ad.available_amount > 0:
        await locks.lock_user_wallet(db, ad.owner_id, ad.crypto_currency)
        await ledger.release_ad_hold_to_available(
            db, user_id=ad.owner_id, currency=ad.crypto_currency,
            amount=ad.available_amount, advertisement_id=ad_id,
            workflow_id=ctx.workflow_id, correlation_id=ctx.correlation_id,
        )
        await wallet.update_wallet_from_ledger(db, ad.owner_id, ad.crypto_currency)
        released = ad.available_amount
        ad.available_amount = Decimal("0")
    else:
        released = Decimal("0")

    state.assert_advertisement_transition(prev, AdvertisementStatus.ARCHIVED.value)
    ad.status = AdvertisementStatus.ARCHIVED.value
    from datetime import datetime, timezone
    ad.archived_at = datetime.now(timezone.utc)
    ad.version += 1
    await db.flush()

    await audit.log(
        db, action="advertisement.archived", entity_type="advertisement", entity_id=ad_id,
        actor_id=ctx.user_id, actor_role=ctx.actor_role,
        previous_state={"status": prev}, new_state={"status": ad.status, "released": str(released)},
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id, source=ctx.source,
    )
    await outbox.emit(
        db, event_type=EventType.ADVERTISEMENT_DELETED.value,  # используем как универсальное событие
        payload={"advertisement_id": ad_id, "owner_id": ad.owner_id,
                 "status": "ARCHIVED", "released": str(released)},
        aggregate_type="advertisement", aggregate_id=ad_id,
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id,
    )
    return {"ok": True, "advertisement_id": ad_id, "status": ad.status, "released": str(released)}

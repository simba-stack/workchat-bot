"""Workflow: update_advertisement — изменение цены/лимитов/условий БЕЗ затрагивания escrow."""
from __future__ import annotations
import logging
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, locks, outbox
from p2p.enums import AdvertisementStatus, EventType, PricingMode
from p2p.models import P2PAdvertisement
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.update_ad")

# Поля разрешённые для on-the-fly изменения (escrow не трогаем).
# Ключ — имя в API/payload, значение — реальный атрибут модели.
_MUTABLE_FIELDS: dict[str, str] = {
    "price_fixed": "price",
    "price_margin_pct": "price_margin_pct",
    "pricing_mode": "pricing_mode",
    "min_order_fiat": "min_amount_fiat",
    "max_order_fiat": "max_amount_fiat",
    "payment_methods": "payment_method_ids",
    "time_limit_minutes": "pay_window_min",
    "require_kyc": "require_verified_taker",
    "min_completed_trades": "min_taker_completed",
    "terms_text": "description",
    "auto_reply_text": "merchant_note",
}

_DECIMAL_KEYS = {"price_fixed", "price_margin_pct", "min_order_fiat", "max_order_fiat"}


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
    if ad.owner_id != ctx.user_id:
        raise HTTPException(403, "not the owner")
    if ad.status not in (AdvertisementStatus.ACTIVE.value, AdvertisementStatus.PAUSED.value, AdvertisementStatus.DRAFT.value):
        raise HTTPException(409, f"cannot update in status {ad.status}")

    prev = {k: getattr(ad, attr) for k, attr in _MUTABLE_FIELDS.items() if hasattr(ad, attr)}
    prev = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in prev.items()}

    changed = {}
    for k, v in (p.get("changes") or {}).items():
        if k not in _MUTABLE_FIELDS:
            continue
        if k in _DECIMAL_KEYS:
            try:
                v = Decimal(str(v)) if v is not None else None
            except Exception:
                raise HTTPException(422, f"{k} must be decimal")
        if k == "pricing_mode" and v not in (PricingMode.FIXED.value, PricingMode.FLOATING.value):
            raise HTTPException(422, "pricing_mode invalid")
        attr = _MUTABLE_FIELDS[k]
        setattr(ad, attr, v)
        changed[k] = str(v) if isinstance(v, Decimal) else v

    if not changed:
        return {"ok": True, "advertisement_id": ad_id, "no_changes": True}

    # Валидация конистентности
    if (ad.min_amount_fiat or Decimal("0")) <= 0 or (ad.max_amount_fiat or Decimal("0")) < (ad.min_amount_fiat or Decimal("0")):
        raise HTTPException(422, "invalid order range")

    ad.version += 1
    await db.flush()

    await audit.log(
        db, action="advertisement.updated", entity_type="advertisement", entity_id=ad_id,
        actor_id=ctx.user_id, actor_role=ctx.actor_role,
        previous_state=prev, new_state=changed,
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id, source=ctx.source,
    )
    await outbox.emit(
        db, event_type=EventType.ADVERTISEMENT_UPDATED.value,
        payload={"advertisement_id": ad_id, "owner_id": ctx.user_id, "changes": changed},
        aggregate_type="advertisement", aggregate_id=ad_id,
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id,
    )
    return {"ok": True, "advertisement_id": ad_id, "changes": changed, "version": ad.version}

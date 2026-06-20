"""Workflow: create_review — отзыв после COMPLETED trade. Один автор → один отзыв на сделку."""
from __future__ import annotations
import logging

from fastapi import HTTPException
from sqlalchemy import select

from p2p import audit, outbox
from p2p.enums import TradeStatus, EventType
from p2p.models import P2PTrade, P2PReview
from p2p.orchestrator import WorkflowContext

logger = logging.getLogger("p2p.wf.review")


async def handle(ctx: WorkflowContext) -> dict:
    p = ctx.input_payload
    db = ctx.db

    trade_id = p.get("trade_id")
    try:
        rating = int(p.get("rating", 0))
    except Exception:
        raise HTTPException(422, "rating must be int 1..5")
    comment = (p.get("comment") or "").strip()[:1000]
    if not trade_id:
        raise HTTPException(422, "trade_id required")
    if rating < 1 or rating > 5:
        raise HTTPException(422, "rating 1..5 required")

    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = r.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")
    if ctx.user_id not in (trade.buyer_id, trade.seller_id):
        raise HTTPException(403, "not a participant")
    if trade.status != TradeStatus.COMPLETED.value:
        raise HTTPException(409, "can review only completed trades")

    target = trade.seller_id if ctx.user_id == trade.buyer_id else trade.buyer_id

    # Один отзыв от автора на сделку — UniqueConstraint
    existing = await db.execute(
        select(P2PReview).where(
            P2PReview.trade_id == trade_id,
            P2PReview.author_id == ctx.user_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "review already submitted")

    review = P2PReview(
        trade_id=trade_id,
        author_id=ctx.user_id,
        target_user_id=target,
        rating=rating,
        comment=comment or None,
    )
    db.add(review)
    await db.flush()

    await audit.log(
        db, action="review.created", entity_type="review", entity_id=review.id,
        actor_id=ctx.user_id, actor_role=ctx.actor_role,
        new_state={"trade_id": trade_id, "target": target, "rating": rating},
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id, source=ctx.source,
    )
    await outbox.emit(
        db, event_type=EventType.MERCHANT_RATING_CHANGED.value,
        payload={"target_user_id": target, "trade_id": trade_id, "rating": rating,
                 "review_id": review.id, "buyer_id": trade.buyer_id, "seller_id": trade.seller_id},
        aggregate_type="user", aggregate_id=str(target),
        correlation_id=ctx.correlation_id, workflow_id=ctx.workflow_id,
    )
    return {"ok": True, "review_id": review.id, "rating": rating, "target_user_id": target}

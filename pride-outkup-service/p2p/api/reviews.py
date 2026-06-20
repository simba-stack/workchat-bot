"""P2P Reviews API."""
from __future__ import annotations
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p.api._deps import get_idempotency_key, get_actor_role
from p2p.models import P2PReview
from p2p.orchestrator import run_workflow
from p2p.workflows import create_review

logger = logging.getLogger("p2p.api.reviews")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-reviews"])


def _review_to_dict(r: P2PReview) -> dict:
    return {
        "id": r.id,
        "trade_id": r.trade_id,
        "author_id": r.author_id,
        "target_user_id": r.target_user_id,
        "rating": r.rating,
        "comment": r.comment,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/trades/{trade_id}/review")
async def cmd_create_review(
    trade_id: str,
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    inp = {**(payload or {}), "trade_id": trade_id}
    return await run_workflow(
        db, workflow_type="create_review", user_id=user.id,
        input_payload=inp, handler=create_review.handle,
        idempotency_key=idempotency_key, actor_role=get_actor_role(user),
        source=f"miniapp:{req.headers.get('user-agent','')[:50]}",
        endpoint=f"POST /p2p/trades/{trade_id}/review",
    )


@router.get("/users/{target_id}/reviews")
async def list_reviews_for_user(
    target_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(P2PReview).where(P2PReview.target_user_id == target_id)
        .order_by(desc(P2PReview.created_at)).limit(limit).offset(offset)
    )
    return {"items": [_review_to_dict(x) for x in r.scalars().all()]}


@router.get("/users/{target_id}/rating-summary")
async def user_rating_summary(
    target_id: int,
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(
            func.count(P2PReview.id),
            func.coalesce(func.avg(P2PReview.rating), 0),
        ).where(P2PReview.target_user_id == target_id)
    )
    count, avg = r.first() or (0, 0)
    return {
        "user_id": target_id,
        "reviews_count": int(count or 0),
        "average_rating": round(float(avg or 0), 2),
    }

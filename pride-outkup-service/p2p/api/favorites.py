"""P2P Favorites API.

GET    /api/v2/p2p/favorites?type=ad|merchant — список с деталями
POST   /api/v2/p2p/favorites                   — добавить (advertisement_id или target_user_id)
DELETE /api/v2/p2p/favorites/{id}              — удалить
"""
from __future__ import annotations
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p import audit
from p2p.models import P2PAdvertisement, P2PFavorite

logger = logging.getLogger("p2p.api.favorites")
router = APIRouter(prefix="/api/v2/p2p/favorites", tags=["p2p-favorites"])


def _ad_summary(a: P2PAdvertisement) -> dict:
    return {
        "id": a.id,
        "owner_id": a.owner_id,
        "type": a.type,
        "crypto": a.crypto_currency,
        "fiat": a.fiat_currency,
        "price": str(a.price) if a.price is not None else None,
        "min_order_fiat": str(a.min_amount_fiat),
        "max_order_fiat": str(a.max_amount_fiat),
        "status": a.status,
    }


def _user_summary(u: User) -> dict:
    return {
        "id": u.id,
        "tg_id": getattr(u, "tg_id", None),
        "username": getattr(u, "username", None),
        "full_name": getattr(u, "full_name", None),
        "completed_deals": getattr(u, "completed_deals", 0),
    }


def _source(req: Optional[Request]) -> Optional[str]:
    if not req:
        return None
    ua = req.headers.get("user-agent", "")[:50]
    return f"miniapp:{ua}"


@router.get("")
async def list_favorites(
    type: Optional[str] = Query(None, regex="^(ad|merchant)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(P2PFavorite).where(P2PFavorite.user_id == user.id)
    if type == "ad":
        q = q.where(P2PFavorite.advertisement_id.is_not(None))
    elif type == "merchant":
        q = q.where(P2PFavorite.target_user_id.is_not(None))
    q = q.order_by(desc(P2PFavorite.created_at))
    r = await db.execute(q)
    favs = list(r.scalars().all())

    ad_ids = [f.advertisement_id for f in favs if f.advertisement_id]
    user_ids = [f.target_user_id for f in favs if f.target_user_id]

    ads_map: dict[str, P2PAdvertisement] = {}
    users_map: dict[int, User] = {}
    if ad_ids:
        ar = await db.execute(
            select(P2PAdvertisement).where(P2PAdvertisement.id.in_(ad_ids))
        )
        ads_map = {a.id: a for a in ar.scalars().all()}
    if user_ids:
        ur = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in ur.scalars().all()}

    items = []
    for f in favs:
        item: dict[str, Any] = {
            "id": f.id,
            "user_id": f.user_id,
            "advertisement_id": f.advertisement_id,
            "target_user_id": f.target_user_id,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        if f.advertisement_id and f.advertisement_id in ads_map:
            item["advertisement"] = _ad_summary(ads_map[f.advertisement_id])
        if f.target_user_id and f.target_user_id in users_map:
            item["merchant"] = _user_summary(users_map[f.target_user_id])
        items.append(item)

    return {"items": items, "count": len(items)}


@router.post("")
async def create_favorite(
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    advertisement_id = payload.get("advertisement_id")
    target_user_id = payload.get("target_user_id")

    if not advertisement_id and not target_user_id:
        raise HTTPException(422, "advertisement_id or target_user_id required")
    if advertisement_id and target_user_id:
        raise HTTPException(422, "only one of advertisement_id/target_user_id allowed")

    # Validate target
    if advertisement_id:
        ar = await db.execute(
            select(P2PAdvertisement).where(P2PAdvertisement.id == advertisement_id)
        )
        if ar.scalar_one_or_none() is None:
            raise HTTPException(404, "advertisement not found")
    if target_user_id:
        try:
            target_user_id = int(target_user_id)
        except (TypeError, ValueError):
            raise HTTPException(422, "target_user_id must be int")
        ur = await db.execute(select(User).where(User.id == target_user_id))
        if ur.scalar_one_or_none() is None:
            raise HTTPException(404, "target user not found")
        if target_user_id == user.id:
            raise HTTPException(422, "cannot favorite yourself")

    fav = P2PFavorite(
        user_id=user.id,
        advertisement_id=advertisement_id,
        target_user_id=target_user_id,
    )
    db.add(fav)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "already in favorites")

    await audit.log(
        db,
        action="favorite.added",
        entity_type="favorite",
        entity_id=fav.id,
        actor_id=user.id,
        new_state={
            "advertisement_id": advertisement_id,
            "target_user_id": target_user_id,
        },
        source=_source(req),
    )
    await db.commit()

    return {
        "id": fav.id,
        "user_id": fav.user_id,
        "advertisement_id": fav.advertisement_id,
        "target_user_id": fav.target_user_id,
        "created_at": fav.created_at.isoformat() if fav.created_at else None,
    }


@router.delete("/{fav_id}")
async def delete_favorite(
    fav_id: str,
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(P2PFavorite).where(P2PFavorite.id == fav_id))
    fav = r.scalar_one_or_none()
    if not fav:
        raise HTTPException(404, "favorite not found")
    if fav.user_id != user.id:
        raise HTTPException(403, "not yours")
    prev = {
        "advertisement_id": fav.advertisement_id,
        "target_user_id": fav.target_user_id,
    }
    await db.delete(fav)
    await audit.log(
        db,
        action="favorite.removed",
        entity_type="favorite",
        entity_id=fav_id,
        actor_id=user.id,
        previous_state=prev,
        source=_source(req),
    )
    await db.commit()
    return {"ok": True, "id": fav_id}

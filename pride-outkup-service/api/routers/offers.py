"""V2 P2P Offers — доска объявлений. Phase B1."""
from fastapi import APIRouter, Depends
from api.auth import get_current_user, require_verified
from core.models import User

router = APIRouter()


@router.get("")
async def list_offers(
    side: str = "buy",
    payment_method: str | None = None,
    min_amount: int = 0,
    online_only: int = 0,
    user: User = Depends(get_current_user),
):
    """V2: список offers с фильтрами. PRIDE Official всегда первым."""
    return {"items": [], "stub": True, "phase": "B1"}


@router.get("/{offer_id}")
async def get_offer(offer_id: int, user: User = Depends(get_current_user)):
    return {"offer": None, "stub": True}


@router.post("")
async def create_offer(payload: dict, user: User = Depends(require_verified)):
    return {"ok": True, "stub": True, "phase": "B1"}


@router.patch("/{offer_id}/pause")
async def pause_offer(offer_id: int, user: User = Depends(require_verified)):
    return {"ok": True, "stub": True}

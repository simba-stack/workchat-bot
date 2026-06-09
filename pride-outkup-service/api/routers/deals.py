"""V2 P2P Deals. Phase B2."""
from fastapi import APIRouter, Depends
from api.auth import get_current_user, require_verified
from core.models import User

router = APIRouter()


@router.post("")
async def create_deal(payload: dict, user: User = Depends(require_verified)):
    """Создать сделку из оффера. Lock escrow."""
    return {"ok": True, "stub": True, "phase": "B2"}


@router.get("/{deal_id}")
async def get_deal(deal_id: int, user: User = Depends(get_current_user)):
    return {"deal": None, "stub": True}


@router.post("/{deal_id}/i_paid")
async def i_paid(deal_id: int, user: User = Depends(get_current_user)):
    return {"ok": True, "stub": True}


@router.post("/{deal_id}/release")
async def release(deal_id: int, user: User = Depends(get_current_user)):
    """Seller подтверждает получение RUB → release USDT."""
    return {"ok": True, "stub": True}


@router.post("/{deal_id}/dispute")
async def open_dispute(deal_id: int, payload: dict, user: User = Depends(get_current_user)):
    return {"ok": True, "stub": True}

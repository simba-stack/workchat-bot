"""V1 Orders — заявки. Phase A3."""
from fastapi import APIRouter, Depends
from api.auth import get_current_user
from core.models import User

router = APIRouter()


@router.get("")
async def list_orders(status: str = "active", user: User = Depends(get_current_user)):
    return {"items": [], "stub": True}


@router.get("/{order_id}")
async def get_order(order_id: int, user: User = Depends(get_current_user)):
    return {"order": None, "stub": True}


@router.post("/business_outkup")
async def create_business_outkup(payload: dict, user: User = Depends(get_current_user)):
    return {"ok": True, "stub": True}


@router.post("/{order_id}/cancel")
async def cancel_order(order_id: int, user: User = Depends(get_current_user)):
    return {"ok": True, "stub": True}


@router.post("/{order_id}/upload_receipt")
async def upload_receipt(order_id: int, user: User = Depends(get_current_user)):
    return {"ok": True, "stub": True}

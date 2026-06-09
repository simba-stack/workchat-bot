"""Admin endpoints — модерация (вызываются из JARVIS или через бот)."""
from fastapi import APIRouter, Depends
from api.auth import require_admin
from core.models import User

router = APIRouter()


@router.get("/users")
async def list_users(user: User = Depends(require_admin)):
    return {"items": [], "stub": True}


@router.post("/users/{user_id}/kyc_decide")
async def kyc_decide(user_id: int, payload: dict, user: User = Depends(require_admin)):
    return {"ok": True, "stub": True}


@router.post("/users/{user_id}/ban")
async def ban_user(user_id: int, payload: dict, user: User = Depends(require_admin)):
    return {"ok": True, "stub": True}


@router.post("/disputes/{dispute_id}/resolve")
async def resolve_dispute(dispute_id: int, payload: dict, user: User = Depends(require_admin)):
    return {"ok": True, "stub": True}


@router.post("/exchange/set_rate")
async def set_rate(payload: dict, user: User = Depends(require_admin)):
    return {"ok": True, "stub": True}

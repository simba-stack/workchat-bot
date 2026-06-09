"""V1 Exchange — обмен с PRIDE (мы контрагент). Phase A3."""
from fastapi import APIRouter, Depends
from api.auth import get_current_user
from core.models import User

router = APIRouter()


@router.get("/rate")
async def get_rate(user: User = Depends(get_current_user)):
    # TODO Phase A3/A4: тянуть из JARVIS sync (outkup_settings.rate_rub_per_usdt)
    return {"buy": 84.0, "sell": 82.0, "fee_pct": 3.5, "updated_at": "2026-06-09T00:00:00Z"}


@router.post("/buy_usdt")
async def buy_usdt(payload: dict, user: User = Depends(get_current_user)):
    # TODO Phase A3: create Order(kind='buy_usdt') + notify JARVIS
    return {"ok": True, "stub": True, "todo": "Phase A3 — buy_usdt order creation"}


@router.post("/sell_usdt")
async def sell_usdt(payload: dict, user: User = Depends(get_current_user)):
    # TODO Phase A3: create Order(kind='sell_usdt') + lock balance
    return {"ok": True, "stub": True, "todo": "Phase A3 — sell_usdt order creation"}

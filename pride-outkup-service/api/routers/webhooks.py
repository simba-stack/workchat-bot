"""Incoming webhooks — TronGrid (auto-credit USDT deposits), JARVIS push."""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_db
from core.models import Dispute, Order, User
from core.services import settings_kv

router = APIRouter()
logger = logging.getLogger(__name__)


def _verify_hmac(body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    calc = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, signature)


# ─── TRON deposit webhook ─────────────────────────────────────────────
@router.post("/tron")
async def tron_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """TronGrid notification — incoming USDT TRC20 transfer.

    Payload (упрощённый):
      {"tx_id": "...", "to_address": "T...", "amount_usdt": "12.34", "from_address": "T..."}
    """
    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "bad json")

    logger.info("[webhook/tron] received: %s", payload)
    tx_id = payload.get("tx_id")
    to_addr = payload.get("to_address")
    amount = payload.get("amount_usdt")
    if not (tx_id and to_addr and amount):
        raise HTTPException(400, "missing fields")

    # TODO Phase A5: lookup user by deposit_address == to_addr, credit balance
    # сейчас просто логируем
    logger.info("[webhook/tron] would credit %s USDT to %s (tx %s)", amount, to_addr, tx_id)
    return {"ok": True, "stub": "phase_a5"}


# ─── JARVIS push webhook ──────────────────────────────────────────────
@router.post("/jarvis")
async def jarvis_push(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_jarvis_signature: str = Header(None),
):
    """Push от PRIDE JARVIS (админ-действия: одобрить KYC, решить спор, set rate)."""
    body = await request.body()
    if settings.jarvis_hmac_secret:
        if not _verify_hmac(body, x_jarvis_signature or "", settings.jarvis_hmac_secret):
            raise HTTPException(401, "invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "bad json")

    event = payload.get("event")
    data = payload.get("data") or {}
    logger.info("[webhook/jarvis] event=%s data=%s", event, data)

    if event == "set_rate":
        # JARVIS пушит свежий курс
        side = data.get("side")  # buy | sell
        rate = data.get("rate")
        if side not in ("buy", "sell"):
            raise HTTPException(400, "side must be buy|sell")
        key = "rate_buy_usdt" if side == "buy" else "rate_sell_usdt"
        await settings_kv.set_setting(db, key, float(rate))
        await db.commit()
        logger.info("[webhook/jarvis] rate updated: %s=%s", key, rate)
        return {"ok": True, "applied": "rate"}

    if event == "kyc_decided":
        # JARVIS подтвердил/отклонил KYC
        user_id = int(data.get("user_id") or 0)
        status_ = data.get("status")  # approved | rejected
        kyc_level = int(data.get("kyc_level") or 0)
        if user_id <= 0 or status_ not in ("approved", "rejected"):
            raise HTTPException(400, "bad user_id/status")
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(404, "user not found")
        user.kyc_status = status_
        if status_ == "approved" and kyc_level:
            user.kyc_level = kyc_level
        await db.commit()
        logger.info("[webhook/jarvis] KYC %s for user %s", status_, user_id)
        return {"ok": True, "applied": "kyc"}

    if event == "dispute_resolved":
        # JARVIS решил спор
        dispute_id = int(data.get("dispute_id") or 0)
        decision = data.get("decision")  # for_buyer | for_seller | split
        notes = data.get("notes") or ""
        if dispute_id <= 0 or decision not in ("for_buyer", "for_seller", "split"):
            raise HTTPException(400, "bad dispute params")
        dispute = await db.get(Dispute, dispute_id)
        if not dispute:
            raise HTTPException(404, "dispute not found")
        from datetime import datetime, timezone
        dispute.status = "resolved"
        dispute.resolution = decision
        dispute.resolution_note = notes
        dispute.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("[webhook/jarvis] dispute %s resolved: %s", dispute_id, decision)
        return {"ok": True, "applied": "dispute"}

    if event == "order_mark_paid":
        # JARVIS-оператор подтвердил получение фиатной оплаты от клиента (для business_outkup / buy_usdt)
        order_id = int(data.get("order_id") or 0)
        if order_id <= 0:
            raise HTTPException(400, "bad order_id")
        order = await db.get(Order, order_id)
        if not order:
            raise HTTPException(404, "order not found")
        order.status = "fiat_received"
        await db.commit()
        logger.info("[webhook/jarvis] order %s -> fiat_received", order_id)
        return {"ok": True, "applied": "order_fiat_received"}

    if event == "order_completed":
        # JARVIS закрыл заявку (после отправки USDT клиенту или прихода ИП)
        order_id = int(data.get("order_id") or 0)
        tx_id = data.get("tx_id") or None
        if order_id <= 0:
            raise HTTPException(400, "bad order_id")
        order = await db.get(Order, order_id)
        if not order:
            raise HTTPException(404, "order not found")
        from datetime import datetime, timezone
        order.status = "done"
        order.completed_at = datetime.now(timezone.utc)
        if tx_id:
            order.extra = {**(order.extra or {}), "tx_id": tx_id}
        await db.commit()
        logger.info("[webhook/jarvis] order %s -> done (tx %s)", order_id, tx_id)
        return {"ok": True, "applied": "order_completed"}

    if event == "feature_flag":
        # переключение feature flag (V2 P2P public on/off)
        key = data.get("key")
        value = data.get("value")
        if not key:
            raise HTTPException(400, "missing key")
        await settings_kv.set_setting(db, key, value)
        await db.commit()
        return {"ok": True, "applied": "flag", "key": key}

    logger.warning("[webhook/jarvis] unknown event: %s", event)
    return {"ok": True, "skipped": "unknown_event"}

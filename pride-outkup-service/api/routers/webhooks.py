"""Incoming webhooks — TronGrid (auto-credit USDT deposits), JARVIS push."""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _verify_hmac(body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    calc = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, signature)


@router.post("/tron")
async def tron_webhook(request: Request):
    """TronGrid notification — incoming USDT TRC20 transfer."""
    body = await request.body()
    payload = json.loads(body or b"{}")
    logger.info("[webhook/tron] received: %s", payload)
    # TODO Phase A5: парсить TX, найти юзера по to_address, начислить баланс
    return {"ok": True}


@router.post("/jarvis")
async def jarvis_push(
    request: Request,
    x_jarvis_signature: str = Header(None),
):
    """Push от PRIDE JARVIS (админ-действия: одобрить KYC, решить спор)."""
    body = await request.body()
    if not _verify_hmac(body, x_jarvis_signature or "", settings.jarvis_hmac_secret):
        raise HTTPException(401, "invalid signature")
    payload = json.loads(body or b"{}")
    event = payload.get("event")
    logger.info("[webhook/jarvis] event=%s", event)
    # TODO Phase A4: обработать события (kyc_decided, dispute_resolved, etc)
    return {"ok": True}

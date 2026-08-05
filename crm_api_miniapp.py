"""Mini-App API bridge — эндпоинты для stroy-crm-bot's Telegram Mini-App.

Архитектура: mini-app UI живёт в stroy-crm-bot (Node.js), но данные о
партнёрах/дропах/ролях — в workchat-bot storage. Поэтому mini-app backend
(miniapp_api.js) вызывает эти эндпоинты через HTTP + HMAC-подпись.

Регистрируется в api.py через `app.include_router(miniapp_router)`.

Auth: HMAC-SHA256 подпись в X-Miniapp-Signature заголовке. Секрет — env
MINIAPP_HMAC_SECRET. Формат body: `{method}|{path}|{ts}|{body_json}`.
TTL: 60 секунд.

⚠️  ROLES:
- owner:              CRM_OWNER_IDS env (или config.ADMIN_ID)
- worker (6 ролей):   storage.state.worker_roles[uname_lower] = {role, is_admin}
- partner:            есть запись в storage.state.crm_owners с tg_user_id
- client (drop):      есть запись в storage.state.crm_drops (по client_id)
- credit_manager:     есть запись в credit_chats как manager
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

import config
from storage import storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/miniapp", tags=["miniapp"])


# ─── HMAC auth ────────────────────────────────────────────────────
def _hmac_secret() -> str:
    return (os.getenv("MINIAPP_HMAC_SECRET", "") or "").strip()


def _sign(method: str, path: str, ts: str, body: str) -> str:
    """Строим подпись: HMAC-SHA256(secret, f'{METHOD}|{PATH}|{TS}|{BODY}')."""
    secret = _hmac_secret().encode()
    payload = f"{method.upper()}|{path}|{ts}|{body}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


async def _check_hmac(request: Request, sig: str, ts: str) -> None:
    """Проверяем подпись. Raises HTTPException при ошибке."""
    if not _hmac_secret():
        raise HTTPException(503, "MINIAPP_HMAC_SECRET not configured")
    if not sig or not ts:
        raise HTTPException(401, "missing signature or timestamp")
    # TTL
    try:
        ts_num = int(ts)
    except Exception:
        raise HTTPException(401, "bad timestamp")
    if abs(time.time() - ts_num) > 60:
        raise HTTPException(401, "signature expired")
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8") if body_bytes else ""
    expected = _sign(request.method, request.url.path, ts, body_str)
    if not hmac.compare_digest(sig, expected):
        logger.warning(
            "[miniapp-api] HMAC mismatch: path=%s ts=%s calc=%s got=%s body=%r",
            request.url.path, ts, expected[:16], sig[:16], body_str[:200],
        )
        raise HTTPException(401, "bad signature")


# ─── Роль-резолвер ────────────────────────────────────────────────
def _resolve_owner_ids() -> set:
    """CRM_OWNER_IDS (env, comma-separated) + config.ADMIN_ID + hardcoded."""
    ids = {8151738775}  # SIMBA hardcoded
    for src in (
        os.getenv("CRM_OWNER_IDS", ""),
        str(getattr(config, "ADMIN_ID", "") or ""),
    ):
        for x in (src or "").split(","):
            x = x.strip()
            if x.isdigit():
                ids.add(int(x))
    return ids


def _resolve_role_snapshot(tg_user_id: int, username: str) -> dict:
    """Возвращает полный role-snapshot по tg_id/username.

    Порядок приоритета: owner > worker > partner > drop > guest.
    Каждый флаг НЕзависимый — юзер может быть одновременно partner+worker.
    """
    uname = (username or "").lstrip("@").lower().strip()
    snap = {
        "tg_user_id": int(tg_user_id),
        "username": uname,
        "is_owner": False,
        "is_worker": False,
        "worker_role": None,
        "worker_is_admin": False,
        "is_partner": False,
        "partner_owner_id": None,
        "is_drop": False,
        "drop_ids": [],
        # Итоговая эффективная роль для UI-меню
        "effective_role": "guest",
        "display_name": "",
    }

    # 1) owner
    if int(tg_user_id) in _resolve_owner_ids():
        snap["is_owner"] = True
        snap["effective_role"] = "owner"

    # 2) worker (по username)
    if uname:
        wr = (storage.state.get("worker_roles") or {}).get(uname)
        if wr and isinstance(wr, dict):
            role = (wr.get("role") or "").strip().lower()
            if role:
                snap["is_worker"] = True
                snap["worker_role"] = role
                snap["worker_is_admin"] = bool(wr.get("is_admin"))
                if not snap["is_owner"]:
                    snap["effective_role"] = role

    # 3) partner (CRM-owner)
    partner = storage.find_crm_owner_by_tg(int(tg_user_id))
    if partner:
        snap["is_partner"] = True
        snap["partner_owner_id"] = partner.get("owner_id")
        snap["display_name"] = partner.get("name") or partner.get("username") or ""
        if snap["effective_role"] == "guest":
            snap["effective_role"] = "partner"

    # 4) drop (client)
    drops = storage.state.get("crm_drops") or {}
    my_drops = [
        did for did, d in drops.items()
        if int(d.get("client_id") or 0) == int(tg_user_id)
    ]
    if my_drops:
        snap["is_drop"] = True
        snap["drop_ids"] = my_drops
        if snap["effective_role"] == "guest":
            snap["effective_role"] = "client"

    return snap


# ─── Модели ──────────────────────────────────────────────────────
class WhoisReq(BaseModel):
    tg_user_id: int
    username: str = ""


# ─── Endpoints ───────────────────────────────────────────────────
@router.post("/whois")
async def whois(
    body: WhoisReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Возвращает role-snapshot по tg_user_id + username.

    Вызывается stroy-crm-bot'ом при каждом входе в mini-app для определения
    прав (owner/worker/partner/drop) и построения меню.
    """
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    return _resolve_role_snapshot(body.tg_user_id, body.username)


@router.get("/partner/{owner_id}")
async def get_partner(
    owner_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Полный профиль партнёра для экрана Профиль в mini-app."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    owner = storage.get_crm_owner(owner_id)
    if not owner:
        raise HTTPException(404, "partner not found")
    drops = storage.list_crm_drops(owner_id=owner_id) or {}
    stats = {
        "drops_total": len(drops),
        "drops_active": sum(1 for d in drops.values() if d.get("status") in ("pending", "accepted")),
        "drops_pending": sum(1 for d in drops.values() if d.get("status") in ("draft", "pending")),
        "drops_done": sum(1 for d in drops.values() if d.get("status") == "done"),
        "drops_brak": sum(1 for d in drops.values() if d.get("status") == "brak"),
    }
    done = [d for d in drops.values() if d.get("status") == "done"]
    stats["avg_price_usdt"] = (
        sum(int(d.get("price_usdt") or 0) for d in done) / len(done)
    ) if done else 0
    return {
        "owner": {
            "owner_id": owner.get("owner_id"),
            "tg_user_id": owner.get("tg_user_id"),
            "username": owner.get("username"),
            "name": owner.get("name"),
            "joined_at": owner.get("joined_at"),
            "rating": owner.get("rating") or 5.0,
            "warnings": owner.get("warnings") or 0,
            "banned_until": owner.get("banned_until") or 0,
        },
        "stats": stats,
    }


@router.get("/drops")
async def list_drops(
    request: Request,
    owner_id: Optional[str] = None,
    client_id: Optional[int] = None,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Список дропов по owner_id (для партнёра) или client_id (для клиента)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    drops_all = storage.list_crm_drops(owner_id=owner_id) if owner_id else (
        storage.state.get("crm_drops") or {}
    )
    items = []
    for did, d in drops_all.items():
        if client_id is not None and int(d.get("client_id") or 0) != int(client_id):
            continue
        items.append({
            "drop_id": did,
            "fio": d.get("fio") or "",
            "phone": d.get("phone") or "",
            "status": d.get("status") or "draft",
            "price_usdt": d.get("price_usdt") or 0,
            "owner_id": d.get("owner_id") or "",
            "work_chat_id": d.get("work_chat_id"),
            "created_at": d.get("created_at") or 0,
        })
    # Сорт: сначала active/pending, потом по дате desc
    order = {"accepted": 0, "pending": 1, "draft": 2, "done": 3, "brak": 4}
    items.sort(key=lambda x: (order.get(x["status"], 99), -float(x["created_at"] or 0)))
    return {"items": items, "count": len(items)}


@router.get("/healthz")
async def healthz():
    """Публичный health-check без auth — чтобы stroy-crm-bot мог пинговать."""
    return {"ok": True, "service": "miniapp-bridge", "hmac_configured": bool(_hmac_secret())}

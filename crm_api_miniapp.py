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
    # Full path with query string — JS-клиент подписывает всё что после base URL,
    # включая ?owner_id=X. Раньше здесь был только request.url.path без query →
    # всегда 401 на GET-запросах с параметрами.
    path_with_query = request.url.path
    if request.url.query:
        path_with_query += "?" + request.url.query
    expected = _sign(request.method, path_with_query, ts, body_str)
    if not hmac.compare_digest(sig, expected):
        logger.warning(
            "[miniapp-api] HMAC mismatch: path=%s ts=%s calc=%s got=%s body=%r",
            path_with_query, ts, expected[:16], sig[:16], body_str[:200],
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


# ═══════════════════════════════════════════════════════════════
# WORK-CHATS (рабочие беседы)
# ═══════════════════════════════════════════════════════════════

@router.get("/work-chats")
async def list_work_chats(
    request: Request,
    owner_id: Optional[str] = None,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Список managed_chats. Для owner_id — только чаты этого партнёра.
    Для owner (без фильтра) — все managed_chats."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    all_chats = storage.state.get("managed_chats") or {}
    # Партнёр видит только work_chat_id из своих дропов
    if owner_id:
        drops = storage.list_crm_drops(owner_id=owner_id) or {}
        allowed = {str(d.get("work_chat_id")) for d in drops.values() if d.get("work_chat_id")}
        # Плюс attached через crm_chats (регистрация партнёра в чате)
        crm_chats = storage.state.get("crm_chats") or {}
        for cid, info in crm_chats.items():
            if info.get("owner_id") == owner_id:
                allowed.add(str(cid))
    else:
        allowed = None
    items = []
    for cid, info in all_chats.items():
        if allowed is not None and str(cid) not in allowed:
            continue
        items.append({
            "chat_id": cid,
            "client_id": info.get("client_id"),
            "client_name": info.get("client_name") or "",
            "client_username": info.get("client_username") or "",
            "payment_method": info.get("payment_method") or "",
            "created_at": info.get("created_at") or 0,
            "welcome_sent": bool(info.get("welcome_sent")),
        })
    items.sort(key=lambda x: -float(x["created_at"] or 0))
    return {"items": items, "count": len(items)}


@router.get("/work-chats/{chat_id}")
async def work_chat_details(
    chat_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Детали work-чата + связанные дропы."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    info = storage.get_chat_info(chat_id)
    if not info:
        raise HTTPException(404, "chat not found")
    drops = storage.state.get("crm_drops") or {}
    related_drops = [
        {"drop_id": did, "fio": d.get("fio"), "status": d.get("status")}
        for did, d in drops.items() if str(d.get("work_chat_id") or "") == str(chat_id)
    ]
    return {
        "chat": {**info, "chat_id": chat_id},
        "drops": related_drops,
    }


# ═══════════════════════════════════════════════════════════════
# DROPS CRUD
# ═══════════════════════════════════════════════════════════════

class DropCreateReq(BaseModel):
    owner_id: str
    fio: str
    work_chat_id: Optional[int] = None


@router.post("/drops")
async def create_drop(
    body: DropCreateReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    if not storage.get_crm_owner(body.owner_id):
        raise HTTPException(404, "owner not found")
    drop_id = await storage.add_crm_drop(
        owner_id=body.owner_id,
        fio=body.fio.strip(),
        work_chat_id=body.work_chat_id,
    )
    return {"drop_id": drop_id}


class DropUpdateReq(BaseModel):
    fio: Optional[str] = None
    phone: Optional[str] = None
    about: Optional[str] = None
    status: Optional[str] = None
    price_usdt: Optional[float] = None
    work_chat_id: Optional[int] = None


@router.patch("/drops/{drop_id}")
async def update_drop(
    drop_id: str,
    body: DropUpdateReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not fields:
        return {"ok": True, "updated": []}
    ok = await storage.update_crm_drop(drop_id, **fields)
    if not ok:
        raise HTTPException(404, "drop not found")
    return {"ok": True, "updated": list(fields.keys())}


@router.get("/drops/{drop_id}")
async def get_drop(
    drop_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Детали дропа + чек-лист (соц/прописка/скан/ЛК) + связанные ЛК."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    d = (storage.state.get("crm_drops") or {}).get(drop_id)
    if not d:
        raise HTTPException(404, "drop not found")
    # Все ЛК этого дропа
    all_lks = storage.state.get("crm_drop_lks") or {}
    lks = [
        {
            "droplk_id": lkid,
            "bank": lk.get("bank"),
            "value": lk.get("value"),
            "status": lk.get("status"),
            "sms_stage": lk.get("sms_stage") or "",
            "created_at": lk.get("created_at") or 0,
        }
        for lkid, lk in all_lks.items()
        if lk.get("drop_id") == drop_id
    ]
    # Чек-лист прогресс
    checklist = {
        "fio_done": bool(d.get("fio")),
        "phone_done": bool(d.get("phone")),
        "about_done": bool(d.get("about")),
        "scan_done": bool(d.get("scan_file_ids")),
        "lks_count": len(lks),
        "ready_to_submit": bool(d.get("fio")) and bool(d.get("phone")) and len(lks) > 0,
    }
    return {"drop": d, "lks": lks, "checklist": checklist}


class DropLKAddReq(BaseModel):
    bank: str
    value: str = ""


@router.post("/drops/{drop_id}/lk")
async def add_drop_lk(
    drop_id: str,
    body: DropLKAddReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    d = (storage.state.get("crm_drops") or {}).get(drop_id)
    if not d:
        raise HTTPException(404, "drop not found")
    lkid = await storage.add_crm_drop_lk(
        drop_id=drop_id,
        owner_id=d.get("owner_id") or "",
        bank=body.bank,
        value=body.value,
    )
    return {"droplk_id": lkid}


@router.post("/drops/{drop_id}/submit")
async def submit_drop(
    drop_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Отправить дроп в работу (status draft → pending)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    ok = await storage.update_crm_drop(drop_id, status="pending", send_ts=time.time())
    if not ok:
        raise HTTPException(404, "drop not found")
    return {"ok": True, "new_status": "pending"}


# ═══════════════════════════════════════════════════════════════
# SYSTEM QUEUE (для СУС/manager)
# ═══════════════════════════════════════════════════════════════

@router.get("/system/queue")
async def system_queue(
    request: Request,
    stage: Optional[str] = None,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Очередь ЛК ожидающих действия СУС.
    stage: 'new' | 'pending' | 'login_asked' | 'perevyaz_asked' | 'perevyaz_received' | None (все)
    """
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    all_lks = storage.state.get("crm_drop_lks") or {}
    drops = storage.state.get("crm_drops") or {}
    items = []
    for lkid, lk in all_lks.items():
        st = (lk.get("sms_stage") or lk.get("status") or "").lower()
        if stage and st != stage.lower():
            continue
        # skip finalized
        if lk.get("status") == "done" and not stage:
            continue
        d = drops.get(lk.get("drop_id"), {})
        items.append({
            "droplk_id": lkid,
            "drop_id": lk.get("drop_id"),
            "bank": lk.get("bank"),
            "status": lk.get("status"),
            "sms_stage": lk.get("sms_stage") or "",
            "drop_fio": d.get("fio") or "",
            "drop_phone": d.get("phone") or "",
            "owner_id": lk.get("owner_id"),
            "created_at": lk.get("created_at") or 0,
        })
    items.sort(key=lambda x: -float(x["created_at"] or 0))
    return {"items": items, "count": len(items)}


class LKActionReq(BaseModel):
    action: str  # 'ask_login' | 'ask_perevyaz' | 'mark_done' | 'mark_brak' | 'retry'
    payload: Optional[dict] = None


@router.post("/system/lk/{droplk_id}/action")
async def lk_action(
    droplk_id: str,
    body: LKActionReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Универсальный action-endpoint для СУС. Меняет sms_stage/status."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    lk = (storage.state.get("crm_drop_lks") or {}).get(droplk_id)
    if not lk:
        raise HTTPException(404, "lk not found")

    updates = {}
    action = body.action
    if action == "ask_login":
        updates = {"sms_stage": "login_asked", "status": "pending"}
    elif action == "ask_perevyaz":
        updates = {"sms_stage": "perevyaz_asked", "status": "pending"}
    elif action == "mark_done":
        updates = {"sms_stage": "done", "status": "done"}
    elif action == "mark_brak":
        updates = {"status": "brak"}
    elif action == "retry":
        updates = {"sms_stage": "", "status": "new"}
    else:
        raise HTTPException(400, f"unknown action: {action}")

    # Merge payload if provided (e.g. code, comment)
    if body.payload:
        for k in ("new_login", "new_password", "new_mail", "new_number", "code_word"):
            if k in body.payload:
                updates[k] = str(body.payload[k])

    await storage.update_crm_drop_lk(droplk_id, **updates)
    return {"ok": True, "updates": updates}


# ═══════════════════════════════════════════════════════════════
# WALLET (партнёрский кошелёк TRC20)
# ═══════════════════════════════════════════════════════════════

@router.get("/wallet/{owner_id}")
async def get_wallet(
    owner_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    owner = storage.get_crm_owner(owner_id)
    if not owner:
        raise HTTPException(404, "owner not found")
    uname = (owner.get("username") or "").lstrip("@").lower()
    wallet_fn = getattr(storage, "get_partner_wallet", None)
    if wallet_fn is None:
        return {"balance_usdt": 0, "pending_payouts": [], "history": []}
    w = wallet_fn(uname) or {}
    return {
        "balance_usdt": float(w.get("balance_usdt") or 0),
        "pending_payouts": w.get("pending_payouts") or [],
        "history": (w.get("history") or [])[-50:],
        "deposit_address": os.getenv("TRC20_DEPOSIT_ADDRESS", ""),
    }


# ═══════════════════════════════════════════════════════════════
# OWNER-ONLY: список работников/партнёров
# ═══════════════════════════════════════════════════════════════

@router.get("/admin/workers")
async def list_workers(
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Все worker_roles + owners (для owner-панели)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    roles = storage.list_worker_roles() if hasattr(storage, "list_worker_roles") else (
        storage.state.get("worker_roles") or {}
    )
    items = []
    for uname, meta in roles.items():
        if isinstance(meta, str):
            items.append({"username": uname, "role": meta, "is_admin": False})
        else:
            items.append({
                "username": uname,
                "role": meta.get("role") or "",
                "is_admin": bool(meta.get("is_admin")),
                "usdt_address": meta.get("usdt_address") or "",
            })
    return {"items": items, "count": len(items)}


@router.get("/admin/partners")
async def list_partners(
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Все CRM-owners (партнёры) с базовой статистикой."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    owners = storage.state.get("crm_owners") or {}
    drops_all = storage.state.get("crm_drops") or {}
    # Считаем total_drops fresh
    items = []
    for oid, o in owners.items():
        my_drops = [d for d in drops_all.values() if d.get("owner_id") == oid]
        items.append({
            "owner_id": oid,
            "tg_user_id": o.get("tg_user_id"),
            "username": o.get("username"),
            "name": o.get("name"),
            "rating": o.get("rating") or 5.0,
            "warnings": o.get("warnings") or 0,
            "banned_until": o.get("banned_until") or 0,
            "drops_total": len(my_drops),
            "drops_done": sum(1 for d in my_drops if d.get("status") == "done"),
        })
    items.sort(key=lambda x: -x["drops_done"])
    return {"items": items, "count": len(items)}


@router.get("/healthz")
async def healthz():
    """Публичный health-check без auth — чтобы stroy-crm-bot мог пинговать."""
    return {"ok": True, "service": "miniapp-bridge", "hmac_configured": bool(_hmac_secret())}

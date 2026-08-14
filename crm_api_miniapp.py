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

    # 2) worker — сначала storage.worker_roles, потом hardcoded fallback
    if uname:
        wr = (storage.state.get("worker_roles") or {}).get(uname)
        role = None
        is_admin = False
        if wr and isinstance(wr, dict):
            role = (wr.get("role") or "").strip().lower() or None
            is_admin = bool(wr.get("is_admin"))
        elif isinstance(wr, str) and wr.strip():
            role = wr.strip().lower()
        # Hardcoded fallback для дефолт-команды
        if not role:
            hc = DEFAULT_TEAM_HARDCODED.get(uname)
            if hc:
                role = hc["role"]
        if role:
            snap["is_worker"] = True
            snap["worker_role"] = role
            snap["worker_is_admin"] = is_admin
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


# ═══════════════════════════════════════════════════════════════
# SELL-WIZARD state viewer (клиент видит прогресс сдачи РС)
# ═══════════════════════════════════════════════════════════════

@router.get("/sell-flow")
async def sell_flow_state(
    request: Request,
    chat_id: str,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Возвращает текущее состояние sell_wizard для клиента.
    Клиент видит: какой шаг, какой банк, какой метод оплаты,
    сколько uploads уже отправлено, статус верификации.
    """
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    flow = storage.get_sell_flow(chat_id) if hasattr(storage, "get_sell_flow") else {}
    if not flow:
        return {"state": None, "step": "not_started"}
    return {
        "state": {
            "step": flow.get("step") or "start",
            "material": flow.get("material") or "",
            "bank": flow.get("bank") or "",
            "price": flow.get("price") or 0,
            "method": flow.get("method") or "",
            "uploads_count": len(flow.get("uploads") or []),
            "uploads_types": list({(u.get("type") or "").lower() for u in (flow.get("uploads") or [])}),
            "deal_number": flow.get("deal_number") or "",
            "opened_ts": flow.get("opened_ts") or 0,
            "updated_ts": flow.get("updated_ts") or 0,
        },
        "step": flow.get("step") or "start",
    }


# ═══════════════════════════════════════════════════════════════
# DROP delete + additional actions
# ═══════════════════════════════════════════════════════════════

@router.delete("/drops/{drop_id}")
async def delete_drop(
    drop_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Удалить дроп (только draft)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    d = (storage.state.get("crm_drops") or {}).get(drop_id)
    if not d:
        raise HTTPException(404, "drop not found")
    if d.get("status") not in ("draft", "brak"):
        raise HTTPException(400, "can only delete draft or brak drops")
    ok = await storage.delete_crm_drop(drop_id)
    return {"ok": bool(ok)}


# ═══════════════════════════════════════════════════════════════
# WALLET operations — deposit request + withdraw request
# ═══════════════════════════════════════════════════════════════

class WalletDepositReq(BaseModel):
    txid: str
    amount_hint: Optional[float] = None


@router.post("/wallet/{owner_id}/deposit")
async def wallet_deposit(
    owner_id: str,
    body: WalletDepositReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Регистрирует уведомление о депозите от партнёра.
    НЕ начисляет баланс — это делает TRON-monitor автоматически или owner вручную.
    Просто добавляет запись в history для контекста."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    owner = storage.get_crm_owner(owner_id)
    if not owner:
        raise HTTPException(404, "owner not found")
    uname = (owner.get("username") or "").lstrip("@").lower()
    if not uname:
        raise HTTPException(400, "owner has no username")
    if not hasattr(storage, "_ensure_partner_wallet"):
        raise HTTPException(503, "wallet subsystem not available")
    from storage import _lock as _st_lock
    async with _st_lock:
        w = await storage._ensure_partner_wallet(uname)
        w.setdefault("history", []).append({
            "ts": time.time(),
            "type": "deposit_notification",
            "txid": body.txid.strip(),
            "amount_hint": float(body.amount_hint or 0),
            "note": "Партнёр сообщил о депозите через mini-app",
        })
        await storage._save_unlocked()
    return {"ok": True, "note": "Ждём подтверждения от owner или TRON-monitor"}


class WalletWithdrawReq(BaseModel):
    amount_usdt: float
    trc20_address: str


@router.post("/wallet/{owner_id}/withdraw")
async def wallet_withdraw(
    owner_id: str,
    body: WalletWithdrawReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Создаёт заявку на вывод. Не списывает баланс — только резервирует.
    Owner подтверждает через /payout в TG."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    owner = storage.get_crm_owner(owner_id)
    if not owner:
        raise HTTPException(404, "owner not found")
    uname = (owner.get("username") or "").lstrip("@").lower()
    payout_id = await storage.wallet_create_payout_request(
        username=uname,
        amount_usdt=body.amount_usdt,
        trc20_address=body.trc20_address,
    )
    if not payout_id:
        raise HTTPException(400, "insufficient balance or invalid input")
    return {"ok": True, "payout_id": payout_id}


# ═══════════════════════════════════════════════════════════════
# OWNER: set worker role
# ═══════════════════════════════════════════════════════════════

class SetRoleReq(BaseModel):
    role: str
    is_admin: Optional[bool] = None
    usdt_address: Optional[str] = None


@router.post("/admin/workers/{username}/role")
async def set_worker_role(
    username: str,
    body: SetRoleReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Устанавливает/меняет роль работника. Owner-only гвард на стороне stroy."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    uname = (username or "").lstrip("@").lower().strip()
    if not uname:
        raise HTTPException(400, "username required")
    valid_roles = {
        "owner", "manager", "system_dept", "accounting",
        "operationist", "outkup_specialist",
    }
    if body.role not in valid_roles:
        raise HTTPException(400, f"role must be one of {valid_roles}")
    # Прямая запись в storage.state["worker_roles"]
    from storage import _lock as _st_lock
    async with _st_lock:
        roles = storage.state.setdefault("worker_roles", {})
        cur = roles.get(uname)
        if isinstance(cur, str):
            cur = {"role": cur, "is_admin": False}
        elif not isinstance(cur, dict):
            cur = {}
        cur["role"] = body.role
        if body.is_admin is not None:
            cur["is_admin"] = bool(body.is_admin)
        if body.usdt_address is not None:
            cur["usdt_address"] = str(body.usdt_address).strip()
        roles[uname] = cur
        await storage._save_unlocked()
    return {"ok": True, "role": cur}


@router.delete("/admin/workers/{username}")
async def remove_worker(
    username: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    uname = (username or "").lstrip("@").lower().strip()
    from storage import _lock as _st_lock
    async with _st_lock:
        roles = storage.state.setdefault("worker_roles", {})
        cur = roles.get(uname)
        if isinstance(cur, dict) and cur.get("role") == "owner":
            raise HTTPException(400, "cannot remove owner role via API")
        removed = roles.pop(uname, None)
        if removed is not None:
            await storage._save_unlocked()
    return {"ok": True, "removed": bool(removed)}


# ═══════════════════════════════════════════════════════════════
# OWNER: partner actions (warn / ban / unban)
# ═══════════════════════════════════════════════════════════════

class PartnerActionReq(BaseModel):
    action: str  # 'warn' | 'ban' | 'unban' | 'reset_rating'
    duration_hours: Optional[int] = None
    reason: Optional[str] = None


@router.post("/admin/partners/{owner_id}/action")
async def partner_action(
    owner_id: str,
    body: PartnerActionReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    owner = storage.get_crm_owner(owner_id)
    if not owner:
        raise HTTPException(404, "owner not found")
    from storage import _lock as _st_lock
    async with _st_lock:
        o = storage.state.setdefault("crm_owners", {}).get(owner_id)
        if not o:
            raise HTTPException(404, "owner not found")
        if body.action == "warn":
            o["warnings"] = int(o.get("warnings") or 0) + 1
        elif body.action == "ban":
            hours = int(body.duration_hours or 24)
            o["banned_until"] = time.time() + hours * 3600
        elif body.action == "unban":
            o["banned_until"] = 0
            o["warnings"] = 0
        elif body.action == "reset_rating":
            o["rating"] = 5.0
        else:
            raise HTTPException(400, f"unknown action: {body.action}")
        await storage._save_unlocked()
    return {"ok": True, "owner": o}


# ═══════════════════════════════════════════════════════════════
# MINI-APP CHAT — свои messages внутри mini-app (не TG-чат)
# Хранятся в storage.state["miniapp_chats"] = { chat_id: { messages: [], meta: {} } }
# ═══════════════════════════════════════════════════════════════

def _ma_chats() -> dict:
    return storage.state.setdefault("miniapp_chats", {})


# ═════════════════════════════════════════════════════════
# Default team — команда PRIDE которая автоматически в каждом рабочем чате.
# Захардкожена по данным SIMBA (авг 2026) — реальные username, tg_id и роли.
# Через env DEFAULT_WORK_CHAT_MEMBERS можно переопределить список username'ов.
DEFAULT_TEAM_HARDCODED = {
    "simba_pride_adm":  {"tg_user_id": 8151738775, "role": "owner",        "display_name": "SIMBA"},
    "timonskup":        {"tg_user_id": 397572312,  "role": "manager",      "display_name": "Тимон"},
    "pride_manager1":   {"tg_user_id": 8232753590, "role": "manager",      "display_name": "М1"},
    "pride_sys01":      {"tg_user_id": 8548697416, "role": "system_dept",  "display_name": "СУС01"},
    "pride_sys02":      {"tg_user_id": 7552445074, "role": "system_dept",  "display_name": "СУС02"},
}


def _default_team_usernames() -> list[str]:
    src = os.getenv(
        "DEFAULT_WORK_CHAT_MEMBERS",
        "simba_pride_adm,timonskup,pride_manager1,pride_sys01,pride_sys02",
    )
    return [u.strip().lstrip("@").lower() for u in src.split(",") if u.strip()]


def _resolve_team_member(uname: str) -> dict | None:
    """Резолвит username → {tg_user_id, role, display_name}.
    Порядок: storage.worker_roles → storage.crm_owners → DEFAULT_TEAM_HARDCODED.
    """
    uname_low = (uname or "").lstrip("@").lower().strip()
    if not uname_low:
        return None
    # 1. worker_roles
    wr = (storage.state.get("worker_roles") or {}).get(uname_low)
    role = None
    if wr:
        if isinstance(wr, dict):
            role = wr.get("role")
        else:
            role = str(wr)
    # 2. crm_owners → tg_id, name
    tg_id = None
    display = uname_low
    for _oid, o in (storage.state.get("crm_owners") or {}).items():
        if (o.get("username") or "").lstrip("@").lower() == uname_low:
            tg_id = int(o.get("tg_user_id") or 0)
            display = o.get("name") or uname_low
            break
    # 3. Hardcoded fallback — если не нашли ни tg_id ни роль
    hc = DEFAULT_TEAM_HARDCODED.get(uname_low)
    if hc:
        if not tg_id:
            tg_id = int(hc["tg_user_id"])
        if not role:
            role = hc["role"]
        if display == uname_low:
            display = hc["display_name"]
    return {
        "username": uname_low,
        "tg_user_id": tg_id or 0,
        "role": role or "unknown",
        "display_name": display,
    }


def _hydrate_default_members(owner_id: str) -> list[dict]:
    """Собирает список members = дефолт-команда + партнёр (owner)."""
    members: list[dict] = []
    seen = set()
    for uname in _default_team_usernames():
        m = _resolve_team_member(uname)
        if not m: continue
        key = m["username"]
        if key in seen: continue
        seen.add(key)
        members.append(m)
    # Добавляем партнёра-owner
    o = storage.get_crm_owner(owner_id)
    if o:
        u = (o.get("username") or "").lstrip("@").lower()
        if u and u not in seen:
            members.append({
                "username": u,
                "tg_user_id": int(o.get("tg_user_id") or 0),
                "role": "partner",
                "display_name": o.get("name") or u,
            })
    return members


class MaChatCreateReq(BaseModel):
    owner_id: str
    client_username: str = ""     # если известен @username клиента
    client_tg_user_id: Optional[int] = None
    client_name: str = ""
    topic: str = "general"        # general | sell_rs | support


@router.post("/ma-chats")
async def ma_chat_create(
    body: MaChatCreateReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Партнёр создаёт новый рабочий чат в mini-app.
    Возвращает chat_id (в формате mac_XXXX)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        chats = _ma_chats()
        seq = int(storage.state.get("miniapp_chats_seq", 0)) + 1
        storage.state["miniapp_chats_seq"] = seq
        chat_id = f"mac_{seq:04d}"

        # Members: дефолт-команда + партнёр + клиент
        members = _hydrate_default_members(body.owner_id)
        client_uname = (body.client_username or "").lstrip("@").lower()
        if client_uname or body.client_tg_user_id:
            members.append({
                "username": client_uname,
                "tg_user_id": int(body.client_tg_user_id or 0),
                "role": "client",
                "display_name": body.client_name or (client_uname or f"client_{body.client_tg_user_id}"),
            })

        # Приветственное системное сообщение
        greeting = {
            "msg_id": 1, "author_tg_user_id": 0, "author_role": "system",
            "text": (
                f"🎯 Новый рабочий чат создан.\n"
                f"Участники: {', '.join('@' + m['username'] for m in members if m['username'])}.\n"
                f"Клиент, нажми «+» ниже чтобы начать сдачу РС."
            ),
            "attachments": [], "kind": "system", "ts": time.time(),
        }

        chats[chat_id] = {
            "chat_id": chat_id,
            "owner_id": body.owner_id,
            "client_username": client_uname,
            "client_tg_user_id": body.client_tg_user_id,
            "client_name": body.client_name or "",
            "topic": body.topic,
            "created_at": time.time(),
            "last_msg_ts": time.time(),
            "members": members,
            "messages": [greeting],
            "msg_seq": 1,
            "sell_state": {
                "step": "start", "material": "", "bank": "", "price": 0,
                "method": "", "uploads": [], "deal_number": "",
            },
        }
        await storage._save_unlocked()
    return {"chat_id": chat_id, "members": members}


@router.get("/ma-chats")
async def ma_chat_list(
    request: Request,
    owner_id: Optional[str] = None,
    for_tg_user_id: Optional[int] = None,
    for_username: Optional[str] = None,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Список mini-app чатов.
    - owner_id — все чаты партнёра
    - for_tg_user_id — все чаты где юзер является клиентом или партнёром
    - без фильтров — все (для owner)
    """
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    # Убедимся что team-чат существует и обновим его members
    from storage import _lock as _st_lock
    async with _st_lock:
        _ensure_team_chat()
        await storage._save_unlocked()
    chats = _ma_chats()
    items = []
    for cid, c in chats.items():
        if owner_id and c.get("owner_id") != owner_id:
            continue
        if for_tg_user_id is not None or for_username:
            # Юзер видит чат если он есть в members (по tg_id или username)
            members = c.get("members") or []
            uname_low = (for_username or "").lstrip("@").lower().strip()
            found = False
            for m in members:
                if for_tg_user_id is not None and int(m.get("tg_user_id") or 0) == int(for_tg_user_id):
                    found = True; break
                if uname_low and (m.get("username") or "").lower() == uname_low:
                    found = True; break
            # Legacy fallback: client_tg_user_id или owner
            if not found and for_tg_user_id is not None:
                if int(c.get("client_tg_user_id") or 0) == int(for_tg_user_id):
                    found = True
                else:
                    o = storage.get_crm_owner(c.get("owner_id") or "")
                    if o and int(o.get("tg_user_id") or 0) == int(for_tg_user_id):
                        found = True
            if not found:
                continue
        _all = c.get("messages") or []
        last_msg = _all[-1] if _all else None
        # Unread — сколько сообщений после моего last_read_msg_id (по tg_user_id)
        unread = 0
        if for_tg_user_id is not None and _all:
            my_last_read = int((c.get("reads") or {}).get(str(for_tg_user_id)) or 0)
            unread = sum(
                1 for m in _all
                if int(m.get("msg_id") or 0) > my_last_read
                and int(m.get("author_tg_user_id") or 0) != int(for_tg_user_id)
            )
        items.append({
            "chat_id": cid,
            "owner_id": c.get("owner_id"),
            "client_username": c.get("client_username") or "",
            "client_tg_user_id": c.get("client_tg_user_id"),
            "client_name": c.get("client_name") or "",
            "topic": c.get("topic") or "general",
            "created_at": float(c.get("created_at") or 0),
            "last_msg_ts": float(c.get("last_msg_ts") or 0),
            "msg_count": len(_all),
            "last_preview": ((last_msg or {}).get("text") or "")[:60] if last_msg else "",
            "unread": unread,
        })
    # Team-чат всегда первым если юзер в нём
    # L6 fix — свежие чаты без сообщений (last_msg_ts=0) шли в конец. Теперь
    # fallback на created_at, чтобы новый чат всегда наверху.
    def _sort_key(x):
        if x["chat_id"] == TEAM_CHAT_ID:
            return (0, 0)
        ts = float(x["last_msg_ts"] or 0) or float(x.get("created_at") or 0)
        return (1, -ts)
    items.sort(key=_sort_key)
    return {"items": items, "count": len(items)}


@router.get("/ma-chats/{chat_id}")
async def ma_chat_get(
    chat_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Полные данные чата: meta + sell_state (без messages — их через /messages).
    H1 fix — если запрошен team-чат но его ещё нет, автосоздаём."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    if chat_id == TEAM_CHAT_ID:
        from storage import _lock as _st_lock
        async with _st_lock:
            _ensure_team_chat()
            await storage._save_unlocked()
    c = _ma_chats().get(chat_id)
    if not c:
        raise HTTPException(404, "chat not found")
    return {
        "chat_id": chat_id,
        "owner_id": c.get("owner_id"),
        "client_username": c.get("client_username") or "",
        "client_tg_user_id": c.get("client_tg_user_id"),
        "client_name": c.get("client_name") or "",
        "topic": c.get("topic") or "general",
        "created_at": c.get("created_at") or 0,
        "members": c.get("members") or [],
        "sell_state": c.get("sell_state") or {},
        "pinned_msg_ids": c.get("pinned_msg_ids") or [],
        "reads": c.get("reads") or {},
    }


@router.delete("/ma-chats/{chat_id}")
async def ma_chat_delete(
    chat_id: str,
    request: Request,
    by_tg_user_id: int = 0,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Удалить mini-app чат. Team-чат удалять нельзя.
    Owner-check: только owner системы ИЛИ партнёр-владелец чата."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    if chat_id == TEAM_CHAT_ID:
        raise HTTPException(400, "cannot delete team chat")
    from storage import _lock as _st_lock
    async with _st_lock:
        chats = _ma_chats()
        c = chats.get(chat_id)
        if not c:
            return {"ok": True}
        # C2 fix — проверка владельца
        is_owner = int(by_tg_user_id) in _resolve_owner_ids()
        is_chat_owner = False
        if c.get("owner_id"):
            o = storage.get_crm_owner(c["owner_id"])
            if o and int(o.get("tg_user_id") or 0) == int(by_tg_user_id):
                is_chat_owner = True
        if not (is_owner or is_chat_owner):
            raise HTTPException(403, "only chat owner or system owner can delete")
        del chats[chat_id]
        await storage._save_unlocked()
    return {"ok": True}


@router.get("/ma-chats/{chat_id}/messages")
async def ma_chat_messages(
    chat_id: str,
    request: Request,
    since: float = 0,
    limit: int = 100,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Long-polling friendly. Возвращает messages с ts > since."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    c = _ma_chats().get(chat_id)
    if not c:
        raise HTTPException(404, "chat not found")
    msgs = c.get("messages") or []
    if since > 0:
        msgs = [m for m in msgs if float(m.get("ts") or 0) > float(since)]
    return {"items": msgs[-limit:], "count": len(msgs), "server_ts": time.time()}


class MaMessageReq(BaseModel):
    text: str
    author_tg_user_id: int
    author_role: str = "client"  # client | partner | manager | system_dept | owner
    attachments: Optional[list] = None
    kind: str = "text"           # text | system | action | sell_event


@router.post("/ma-chats/{chat_id}/messages")
async def ma_chat_post(
    chat_id: str,
    body: MaMessageReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Отправить сообщение в чат."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        c = _ma_chats().get(chat_id)
        if not c:
            raise HTTPException(404, "chat not found")
        msgs = c.setdefault("messages", [])
        seq = int(c.get("msg_seq", 0)) + 1
        c["msg_seq"] = seq
        msg = {
            "msg_id": seq,
            "author_tg_user_id": int(body.author_tg_user_id),
            "author_role": body.author_role or "client",
            "text": (body.text or "").strip(),
            "attachments": body.attachments or [],
            "kind": body.kind or "text",
            "ts": time.time(),
        }
        msgs.append(msg)
        c["last_msg_ts"] = msg["ts"]
        # Ограничиваем: 1000 сообщений в чате.
        # H5 fix — bump reads/pins чтобы старые ссылки не сломались.
        if len(msgs) > 1000:
            trimmed = msgs[-1000:]
            c["messages"] = trimmed
            min_id = min(int(m.get("msg_id") or 0) for m in trimmed) if trimmed else 0
            # Reads: если last_read юзера < min_id — bump до min_id-1 (не помечать удалённые как unread)
            reads = c.get("reads") or {}
            for uid, last_read in list(reads.items()):
                if int(last_read or 0) < min_id:
                    reads[uid] = min_id - 1
            c["reads"] = reads
            # Pins: удаляем ссылки на удалённые
            c["pinned_msg_ids"] = [x for x in (c.get("pinned_msg_ids") or []) if int(x) >= min_id]
        await storage._save_unlocked()
    return {"message": msg}


# ═══════════════════════════════════════════════════════════════
# SELL-WIZARD внутри mini-app чата (клиент нажимает "+" → выбирает)
# ═══════════════════════════════════════════════════════════════

class MaSellStepReq(BaseModel):
    step: str            # material | bank | payment | deal | done
    value: str = ""
    author_tg_user_id: int
    price: Optional[float] = None


@router.post("/ma-chats/{chat_id}/sell-step")
async def ma_sell_step(
    chat_id: str,
    body: MaSellStepReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Обновляет sell-state чата + добавляет системное сообщение о шаге.
    Логика упрощена: sell_state.step двигается вперёд, каждый шаг записывается
    как system-message в чат.
    """
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        c = _ma_chats().get(chat_id)
        if not c:
            raise HTTPException(404, "chat not found")
        ss = c.setdefault("sell_state", {})
        step_map = {
            "material": ("material", "🧾 Материал: {v}"),
            "bank": ("bank", "🏦 Банк: {v}"),
            "payment": ("method", "💳 Метод оплаты: {v}"),
            "deal": ("deal_number", "📃 Номер сделки: {v}"),
            "done": ("step", "✅ Отправлено на верификацию"),
        }
        if body.step not in step_map:
            raise HTTPException(400, f"unknown step: {body.step}")
        field, text_tpl = step_map[body.step]
        if body.step != "done":
            ss[field] = body.value
        if body.price is not None:
            ss["price"] = float(body.price)
        # Автоматический next-step
        step_order = {"material": "bank", "bank": "payment", "payment": "deal", "deal": "done"}
        if body.step in step_order:
            ss["step"] = step_order[body.step]
        else:
            ss["step"] = "done"

        # Системное сообщение
        msgs = c.setdefault("messages", [])
        seq = int(c.get("msg_seq", 0)) + 1
        c["msg_seq"] = seq
        msgs.append({
            "msg_id": seq,
            "author_tg_user_id": int(body.author_tg_user_id),
            "author_role": "system",
            "text": text_tpl.format(v=body.value or ""),
            "attachments": [],
            "kind": "sell_event",
            "ts": time.time(),
        })
        c["last_msg_ts"] = time.time()
        await storage._save_unlocked()
    return {"sell_state": ss}


# ═══════════════════════════════════════════════════════════════
# CHAT read/unread + pins
# ═══════════════════════════════════════════════════════════════

class ChatReadReq(BaseModel):
    tg_user_id: int
    last_read_msg_id: int


@router.post("/ma-chats/{chat_id}/read")
async def ma_chat_mark_read(
    chat_id: str,
    body: ChatReadReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Отметить сообщения как прочитанные до N."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        c = _ma_chats().get(chat_id)
        if not c:
            raise HTTPException(404, "chat not found")
        reads = c.setdefault("reads", {})
        cur = int(reads.get(str(body.tg_user_id)) or 0)
        if body.last_read_msg_id > cur:
            reads[str(body.tg_user_id)] = int(body.last_read_msg_id)
            await storage._save_unlocked()
    return {"ok": True}


class ChatPinReq(BaseModel):
    msg_id: int
    pinned: bool = True


@router.post("/ma-chats/{chat_id}/pin")
async def ma_chat_pin(
    chat_id: str,
    body: ChatPinReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Закрепить/открепить сообщение."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        c = _ma_chats().get(chat_id)
        if not c:
            raise HTTPException(404, "chat not found")
        pins = c.setdefault("pinned_msg_ids", [])
        if body.pinned and body.msg_id not in pins:
            pins.append(int(body.msg_id))
        elif not body.pinned and body.msg_id in pins:
            pins.remove(int(body.msg_id))
        # M7 fix: max 3 pinned (было 5, теперь строже)
        if len(pins) > 3:
            c["pinned_msg_ids"] = pins[-3:]
        await storage._save_unlocked()
    return {"ok": True, "pins": c.get("pinned_msg_ids") or []}


# ═══════════════════════════════════════════════════════════════
# PRIDE FEED — общий канал новостей для всех mini-app юзеров
# Хранится в state["pride_feed"] = [posts]
# ═══════════════════════════════════════════════════════════════

@router.get("/feed")
async def feed_list(
    request: Request,
    limit: int = 50,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Возвращает posts из PRIDE Feed (новости для всех)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    posts = (storage.state.get("pride_feed") or [])[-limit:]
    return {"items": posts, "count": len(posts)}


class FeedPostReq(BaseModel):
    title: str
    text: str
    kind: str = "news"   # news | update | announcement
    author_tg_user_id: int
    image_url: Optional[str] = None
    image_name: Optional[str] = None
    comments_enabled: bool = True
    author_display: str = "self"  # "self" | "community"
    author_name: Optional[str] = None  # если self — тут имя, для community игнор


@router.post("/feed")
async def feed_post(
    body: FeedPostReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Создать post в feed. Только owner (проверка на стороне stroy-crm-bot)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        feed = storage.state.setdefault("pride_feed", [])
        seq = int(storage.state.get("pride_feed_seq", 0)) + 1
        storage.state["pride_feed_seq"] = seq
        post = {
            "id": seq,
            "title": (body.title or "").strip()[:200],
            "text": (body.text or "").strip()[:5000],
            "kind": body.kind or "news",
            "author_tg_user_id": int(body.author_tg_user_id),
            "ts": time.time(),
            "likes": 0,
            "image_url": (body.image_url or "").strip() or None,
            "image_name": (body.image_name or "").strip() or None,
            "comments_enabled": bool(body.comments_enabled),
            "author_display": body.author_display if body.author_display in ("self", "community") else "self",
            "author_name": (body.author_name or "").strip()[:64] or None,
            "pinned": False,
            "comments": [],
            "comments_seq": 0,
        }
        feed.append(post)
        if len(feed) > 200:
            storage.state["pride_feed"] = feed[-200:]
        await storage._save_unlocked()
    return {"post": post}


@router.delete("/feed/{post_id}")
async def feed_delete(
    post_id: int,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        feed = storage.state.setdefault("pride_feed", [])
        storage.state["pride_feed"] = [p for p in feed if int(p.get("id") or 0) != int(post_id)]
        await storage._save_unlocked()
    return {"ok": True}


class FeedPatchReq(BaseModel):
    title: Optional[str] = None
    text: Optional[str] = None
    kind: Optional[str] = None
    image_url: Optional[str] = None  # "" чтобы убрать
    image_name: Optional[str] = None
    comments_enabled: Optional[bool] = None
    author_display: Optional[str] = None
    author_name: Optional[str] = None
    pinned: Optional[bool] = None


@router.patch("/feed/{post_id}")
async def feed_patch(
    post_id: int,
    body: FeedPatchReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Редактировать пост (owner-only гвард на стороне stroy)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        feed = storage.state.setdefault("pride_feed", [])
        target = None
        for p in feed:
            if int(p.get("id") or 0) == int(post_id):
                target = p
                break
        if not target:
            raise HTTPException(404, "post not found")
        if body.title is not None:      target["title"] = body.title.strip()[:200]
        if body.text is not None:       target["text"] = body.text.strip()[:5000]
        if body.kind is not None:       target["kind"] = body.kind
        if body.image_url is not None:
            target["image_url"] = body.image_url.strip() or None
            target["image_name"] = (body.image_name or "").strip() or None
        if body.comments_enabled is not None:
            target["comments_enabled"] = bool(body.comments_enabled)
        if body.author_display in ("self", "community"):
            target["author_display"] = body.author_display
        if body.author_name is not None:
            target["author_name"] = body.author_name.strip()[:64] or None
        if body.pinned is not None:
            new_pinned = bool(body.pinned)
            # M7 fix — max 3 закреплённых постов. Если делаем pinned=True
            # и уже 3 закреплено — открепляем самый старый (по ts).
            if new_pinned and not target.get("pinned"):
                pinned_now = [p for p in feed if p.get("pinned")]
                if len(pinned_now) >= 3:
                    oldest = min(pinned_now, key=lambda p: float(p.get("ts") or 0))
                    oldest["pinned"] = False
            target["pinned"] = new_pinned
        target["edited_ts"] = time.time()
        await storage._save_unlocked()
    return {"post": target}


# ═════════════════════════════════════════════════════════
# COMMENTS
# ═════════════════════════════════════════════════════════

class FeedCommentReq(BaseModel):
    text: str
    author_tg_user_id: int
    author_name: str = ""


@router.post("/feed/{post_id}/comments")
async def feed_comment_add(
    post_id: int,
    body: FeedCommentReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        feed = storage.state.setdefault("pride_feed", [])
        target = None
        for p in feed:
            if int(p.get("id") or 0) == int(post_id):
                target = p; break
        if not target:
            raise HTTPException(404, "post not found")
        if target.get("comments_enabled") is False:
            raise HTTPException(403, "comments disabled")
        text = (body.text or "").strip()[:1000]
        if not text:
            raise HTTPException(400, "empty text")
        seq = int(target.get("comments_seq", 0)) + 1
        target["comments_seq"] = seq
        comment = {
            "id": seq,
            "text": text,
            "author_tg_user_id": int(body.author_tg_user_id),
            "author_name": (body.author_name or "").strip()[:64] or "user",
            "ts": time.time(),
        }
        comments = target.setdefault("comments", [])
        comments.append(comment)
        if len(comments) > 500:
            target["comments"] = comments[-500:]
        await storage._save_unlocked()
    return {"comment": comment}


@router.delete("/feed/{post_id}/comments/{comment_id}")
async def feed_comment_delete(
    post_id: int,
    comment_id: int,
    request: Request,
    by_tg_user_id: int = 0,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Удалить комментарий. Только автор комментария ИЛИ owner системы.
    C4 fix — раньше проверка была на словах, теперь реально."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    is_system_owner = int(by_tg_user_id) in _resolve_owner_ids()
    from storage import _lock as _st_lock
    async with _st_lock:
        feed = storage.state.setdefault("pride_feed", [])
        for p in feed:
            if int(p.get("id") or 0) == int(post_id):
                comments = p.get("comments") or []
                target = next((c for c in comments if int(c.get("id") or 0) == int(comment_id)), None)
                if not target:
                    return {"ok": True}
                is_author = int(target.get("author_tg_user_id") or 0) == int(by_tg_user_id)
                if not (is_author or is_system_owner):
                    raise HTTPException(403, "only comment author or system owner can delete")
                p["comments"] = [c for c in comments if int(c.get("id") or 0) != int(comment_id)]
                break
        await storage._save_unlocked()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# PRIDE TEAM CHAT — общий чат команды (все worker_roles + owner)
# Special chat_id: "team". Автосоздаётся при первом обращении.
# ═══════════════════════════════════════════════════════════════

TEAM_CHAT_ID = "team"

def _ensure_team_chat() -> dict:
    """Создаёт или возвращает общий team-чат.
    Members = все worker_roles + owner + hardcoded default team.
    """
    chats = _ma_chats()
    c = chats.get(TEAM_CHAT_ID)
    # Собираем актуальный список members каждый раз (роли могут меняться)
    members: list[dict] = []
    seen = set()
    # 1. Owner (hardcoded по CRM_OWNER_IDS + SIMBA)
    for oid in _resolve_owner_ids():
        # Ищем этого owner в crm_owners/hardcoded
        found = None
        for _u, hc in DEFAULT_TEAM_HARDCODED.items():
            if hc["tg_user_id"] == oid:
                found = {"username": _u, "tg_user_id": oid, "role": hc["role"], "display_name": hc["display_name"]}
                break
        if not found:
            for _oid, o in (storage.state.get("crm_owners") or {}).items():
                if int(o.get("tg_user_id") or 0) == oid:
                    found = {"username": (o.get("username") or "").lower(), "tg_user_id": oid, "role": "owner", "display_name": o.get("name") or ""}
                    break
        if found and found["username"] not in seen:
            members.append(found)
            seen.add(found["username"])
    # 2. worker_roles
    for uname, wr in (storage.state.get("worker_roles") or {}).items():
        if uname in seen: continue
        m = _resolve_team_member(uname)
        if m:
            members.append(m)
            seen.add(uname)
    # 3. hardcoded default team
    for uname in DEFAULT_TEAM_HARDCODED:
        if uname in seen: continue
        m = _resolve_team_member(uname)
        if m:
            members.append(m)
            seen.add(uname)

    if not c:
        c = {
            "chat_id": TEAM_CHAT_ID,
            "owner_id": "",
            "client_username": "",
            "client_tg_user_id": None,
            "client_name": "PRIDE Team",
            "topic": "team",
            "created_at": time.time(),
            "last_msg_ts": time.time(),
            "members": members,
            "messages": [{
                "msg_id": 1, "author_tg_user_id": 0, "author_role": "system",
                "text": "🎯 Общий чат команды PRIDE. Здесь все свои.",
                "attachments": [], "kind": "system", "ts": time.time(),
            }],
            "msg_seq": 1,
            "sell_state": {},
            "pinned_msg_ids": [],
            "reads": {},
        }
        chats[TEAM_CHAT_ID] = c
    else:
        # Обновляем members актуально
        c["members"] = members
    return c


@router.get("/ma-chats/team")
async def ma_team_chat(
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Возвращает team-чат (автосоздаётся)."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        c = _ensure_team_chat()
        await storage._save_unlocked()
    return {
        "chat_id": c["chat_id"],
        "owner_id": "",
        "client_username": "",
        "client_tg_user_id": None,
        "client_name": c["client_name"],
        "topic": c["topic"],
        "created_at": c["created_at"],
        "members": c.get("members") or [],
        "sell_state": {},
        "pinned_msg_ids": c.get("pinned_msg_ids") or [],
        "reads": c.get("reads") or {},
    }


# ═══════════════════════════════════════════════════════════════
# СУС ГРУППЫ — эмуляция TG-групп: Доступы, Пароли, Аудит-1, Аудит-2
# Хранятся в state["sus_groups"][group_key] = [entries]
# Каждая entry: {id, text, author_tg_user_id, ts, kind, attachments}
# ═══════════════════════════════════════════════════════════════

SUS_GROUPS = {
    "access":    {"title": "🔑 Доступы",    "desc": "IP-адреса, RDP, служебные креды"},
    "passwords": {"title": "🔐 Пароли",     "desc": "Пароли от банков, ЛК"},
    "audit_1":   {"title": "📋 Аудит-1",   "desc": "Основная группа аудита"},
    "audit_2":   {"title": "📋 Аудит-2",   "desc": "Резервная группа аудита"},
}


def _sus_groups_data() -> dict:
    return storage.state.setdefault("sus_groups", {})


@router.get("/sus-groups")
async def sus_groups_list(
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Список групп + количество записей в каждой."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    data = _sus_groups_data()
    groups = []
    for key, meta in SUS_GROUPS.items():
        entries = data.get(key) or []
        last = entries[-1] if entries else None
        groups.append({
            "key": key,
            "title": meta["title"],
            "desc": meta["desc"],
            "count": len(entries),
            "last_ts": (last or {}).get("ts", 0),
            "last_preview": ((last or {}).get("text") or "")[:80],
        })
    return {"items": groups}


@router.get("/sus-groups/{group_key}")
async def sus_group_entries(
    group_key: str,
    request: Request,
    limit: int = 100,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Записи внутри группы."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    if group_key not in SUS_GROUPS:
        raise HTTPException(404, "group not found")
    entries = (_sus_groups_data().get(group_key) or [])[-limit:]
    return {
        "key": group_key,
        "title": SUS_GROUPS[group_key]["title"],
        "desc": SUS_GROUPS[group_key]["desc"],
        "items": entries,
        "count": len(entries),
    }


class SusEntryReq(BaseModel):
    text: str
    author_tg_user_id: int
    kind: str = "note"     # note | access | password | audit


@router.post("/sus-groups/{group_key}")
async def sus_group_add(
    group_key: str,
    body: SusEntryReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    if group_key not in SUS_GROUPS:
        raise HTTPException(404, "group not found")
    from storage import _lock as _st_lock
    async with _st_lock:
        data = _sus_groups_data()
        entries = data.setdefault(group_key, [])
        seq = int(storage.state.get(f"sus_groups_seq_{group_key}", 0)) + 1
        storage.state[f"sus_groups_seq_{group_key}"] = seq
        entry = {
            "id": seq,
            "text": (body.text or "").strip()[:2000],
            "author_tg_user_id": int(body.author_tg_user_id),
            "ts": time.time(),
            "kind": body.kind or "note",
        }
        entries.append(entry)
        if len(entries) > 500:
            data[group_key] = entries[-500:]
        await storage._save_unlocked()
    return {"entry": entry}


@router.delete("/sus-groups/{group_key}/{entry_id}")
async def sus_group_delete(
    group_key: str,
    entry_id: int,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        data = _sus_groups_data()
        entries = data.get(group_key) or []
        data[group_key] = [e for e in entries if int(e.get("id") or 0) != int(entry_id)]
        await storage._save_unlocked()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# NEW-CLIENT FULL FLOW — полная проверка + следующие шаги
# Создаёт drop сразу в статусе draft со всеми полями pre-verification.
# ═══════════════════════════════════════════════════════════════

class NewClientFlowReq(BaseModel):
    owner_id: str
    fio: str
    phone: str = ""
    material: str = ""       # IP | DEBET
    bank: str = ""           # ALFA | OZON | RAIF | ...
    verification_screenshot_uploaded: bool = False
    verification_video_uploaded: bool = False
    inn: str = ""
    payment_method: str = "" # GUARANTOR | USDT_TRC20
    deal_number: str = ""


@router.post("/new-client-flow")
async def new_client_flow(
    body: NewClientFlowReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Один-endpoint для создания клиента с полными данными флоу.
    Меньше туда-сюда — фронт собирает всё и отправляет одним запросом.
    Дроп создаётся сразу draft с записанными полями pre-check."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    if not storage.get_crm_owner(body.owner_id):
        raise HTTPException(404, "owner not found")
    drop_id = await storage.add_crm_drop(
        owner_id=body.owner_id,
        fio=body.fio.strip(),
        work_chat_id=None,
    )
    # Записываем всё что собрали
    updates = {}
    if body.phone: updates["phone"] = body.phone.strip()
    if body.material: updates["material"] = body.material
    if body.bank: updates["bank_preferred"] = body.bank
    if body.inn: updates["inn"] = body.inn
    if body.payment_method: updates["payment_method"] = body.payment_method
    if body.deal_number: updates["deal_number"] = body.deal_number
    updates["verification"] = {
        "screenshot_uploaded": body.verification_screenshot_uploaded,
        "video_uploaded": body.verification_video_uploaded,
        "inn_provided": bool(body.inn),
    }
    if updates:
        await storage.update_crm_drop(drop_id, **updates)
    return {"drop_id": drop_id}


# ═══════════════════════════════════════════════════════════════
# ACCOUNTS — свой account-слой поверх TG initData.
# Логин/пароль + secret_key для переноса на новый TG-аккаунт.
# Storage: state["ma_accounts"] = { acc_id: {...} }
#          state["ma_accounts_by_tgid"] = { tg_id: acc_id }
#          state["ma_accounts_by_login"] = { login: acc_id }
# ═══════════════════════════════════════════════════════════════

def _accounts_dict() -> dict:
    return storage.state.setdefault("ma_accounts", {})

def _accounts_by_tgid() -> dict:
    return storage.state.setdefault("ma_accounts_by_tgid", {})

def _accounts_by_login() -> dict:
    return storage.state.setdefault("ma_accounts_by_login", {})


def _pepper() -> str:
    return os.getenv("MINIAPP_PEPPER", "pride-2026")


def _hash_pw_scrypt(login: str, password: str) -> str:
    """H2 fix — scrypt(N=16384, r=8, p=1) вместо голого SHA-256.
    Формат: 'scrypt$<salt_hex>$<hash_hex>'. Соль per-пароль."""
    import secrets as _secrets
    salt = _secrets.token_bytes(16)
    key = hashlib.scrypt(
        password=f"{login.lower()}:{password}:{_pepper()}".encode(),
        salt=salt, n=16384, r=8, p=1, dklen=32,
    )
    return f"scrypt${salt.hex()}${key.hex()}"


def _verify_pw(login: str, password: str, stored: str) -> bool:
    """Проверяет пароль. Поддерживает legacy SHA-256 (для миграции)."""
    if not stored:
        return False
    if stored.startswith("scrypt$"):
        try:
            _, salt_hex, hash_hex = stored.split("$", 2)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            got = hashlib.scrypt(
                password=f"{login.lower()}:{password}:{_pepper()}".encode(),
                salt=salt, n=16384, r=8, p=1, dklen=32,
            )
            return hmac.compare_digest(expected, got)
        except Exception:
            return False
    # Legacy SHA-256 (для аккаунтов до H2)
    legacy = hashlib.sha256(f"{login.lower()}:{password}:{_pepper()}".encode()).hexdigest()
    return hmac.compare_digest(stored, legacy)


def _hash_pw(login: str, password: str) -> str:
    """Alias для новых хешей — теперь scrypt."""
    return _hash_pw_scrypt(login, password)


def _hash_secret(secret: str) -> str:
    """Secret-ключ — оставляем SHA-256 (уже 64-bit entropy, не brute-force)."""
    return hashlib.sha256(f"secret:{secret}:{_pepper()}".encode()).hexdigest()


def _gen_secret_key() -> str:
    """Формат: XXXX-XXXX-XXXX-XXXX (16 hex-групп по 4). Читаемо."""
    import secrets as _secrets
    raw = _secrets.token_hex(8).upper()  # 16 hex chars
    return "-".join(raw[i:i+4] for i in range(0, 16, 4))


def _public_account(acc: dict) -> dict:
    """Без hash'ей."""
    return {
        "account_id": acc.get("account_id"),
        "login": acc.get("login"),
        "linked_tg_id": acc.get("linked_tg_id"),
        "linked_tg_username": acc.get("linked_tg_username"),
        "created_at": acc.get("created_at"),
        "secret_last4": acc.get("secret_last4"),
    }


class AccountRegisterReq(BaseModel):
    login: str
    password: str
    tg_id: int
    tg_username: str = ""


@router.post("/accounts/register")
async def account_register(
    body: AccountRegisterReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Регистрация: логин+пароль, генерируется secret_key (показывается 1 раз),
    аккаунт привязывается к текущему tg_id."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    login = (body.login or "").strip().lower()
    if len(login) < 3 or len(login) > 32:
        raise HTTPException(400, "login length 3..32")
    if not login.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "login must be alphanumeric (+ _ -)")
    if len(body.password) < 4:
        raise HTTPException(400, "password too short (min 4)")
    from storage import _lock as _st_lock
    async with _st_lock:
        by_login = _accounts_by_login()
        by_tgid = _accounts_by_tgid()
        if login in by_login:
            raise HTTPException(409, "login taken")
        if str(body.tg_id) in by_tgid:
            raise HTTPException(409, "this tg_id already has account — login instead")
        accounts = _accounts_dict()
        seq = int(storage.state.get("ma_accounts_seq", 0)) + 1
        storage.state["ma_accounts_seq"] = seq
        acc_id = f"acc_{seq:05d}"
        secret_key = _gen_secret_key()
        acc = {
            "account_id": acc_id,
            "login": login,
            "password_hash": _hash_pw(login, body.password),
            "secret_hash": _hash_secret(secret_key),
            "secret_last4": secret_key[-4:],  # для показа
            "linked_tg_id": int(body.tg_id),
            "linked_tg_username": (body.tg_username or "").lstrip("@").lower(),
            "created_at": time.time(),
        }
        accounts[acc_id] = acc
        by_login[login] = acc_id
        by_tgid[str(body.tg_id)] = acc_id
        await storage._save_unlocked()
    return {
        "account_id": acc_id,
        "login": login,
        "secret_key": secret_key,  # ⚠️ показать 1 раз юзеру!
    }


class AccountLoginReq(BaseModel):
    login: str
    password: str
    tg_id: int
    tg_username: str = ""


@router.post("/accounts/login")
async def account_login(
    body: AccountLoginReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Логин по логину/паролю. Обновляет привязку к текущему tg_id."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    login = (body.login or "").strip().lower()
    from storage import _lock as _st_lock
    async with _st_lock:
        by_login = _accounts_by_login()
        acc_id = by_login.get(login)
        if not acc_id:
            raise HTTPException(404, "account not found")
        acc = _accounts_dict().get(acc_id)
        if not acc or not _verify_pw(login, body.password, acc.get("password_hash") or ""):
            raise HTTPException(401, "bad credentials")
        # H2: миграция — если старый SHA-256 хеш, пересчитываем на scrypt при успешном login
        old_hash = acc.get("password_hash") or ""
        if not old_hash.startswith("scrypt$"):
            acc["password_hash"] = _hash_pw_scrypt(login, body.password)
        # Обновляем привязку tg_id
        old_tgid = str(acc.get("linked_tg_id") or "")
        new_tgid = str(body.tg_id)
        by_tgid = _accounts_by_tgid()
        if old_tgid and old_tgid != new_tgid and by_tgid.get(old_tgid) == acc_id:
            del by_tgid[old_tgid]
        by_tgid[new_tgid] = acc_id
        acc["linked_tg_id"] = int(body.tg_id)
        acc["linked_tg_username"] = (body.tg_username or "").lstrip("@").lower()
        await storage._save_unlocked()
    return _public_account(acc)


class AccountMigrateReq(BaseModel):
    secret_key: str
    tg_id: int
    tg_username: str = ""


@router.post("/accounts/migrate")
async def account_migrate(
    body: AccountMigrateReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Перенос аккаунта на новый TG по secret_key. Ищем аккаунт с матчащим
    secret_hash и перепривязываем к новому tg_id.
    H3 fix — rate-limit: max 3 неудачные попытки за 10 мин с одного tg_id.
    После 3 неудач — 429 на 30 мин."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    sk = (body.secret_key or "").strip().upper()
    if not sk:
        raise HTTPException(400, "secret_key required")

    # Rate-limit check
    now = time.time()
    attempts_map = storage.state.setdefault("migrate_attempts", {})
    tg_key = str(body.tg_id)
    old_attempts = [t for t in (attempts_map.get(tg_key) or []) if now - t < 1800]
    fails_10min = [t for t in old_attempts if now - t < 600]
    if len(fails_10min) >= 3:
        oldest_in_lock = min(old_attempts) if old_attempts else now
        wait_left = int(1800 - (now - oldest_in_lock))
        raise HTTPException(429, f"too many migrate attempts. wait {max(60, wait_left)}s")

    target_hash = _hash_secret(sk)
    from storage import _lock as _st_lock
    async with _st_lock:
        accounts = _accounts_dict()
        acc = None
        for a in accounts.values():
            if a.get("secret_hash") == target_hash:
                acc = a
                break
        if not acc:
            # Записываем неудачу
            old_attempts.append(now)
            attempts_map[tg_key] = old_attempts
            await storage._save_unlocked()
            remaining = 3 - len(fails_10min) - 1
            raise HTTPException(404, f"no account with this secret key. attempts left: {max(0, remaining)}")
        # Успех — очищаем attempts
        attempts_map.pop(tg_key, None)
        # Перепривязываем
        by_tgid = _accounts_by_tgid()
        old_tgid = str(acc.get("linked_tg_id") or "")
        new_tgid = str(body.tg_id)
        if old_tgid and old_tgid != new_tgid and by_tgid.get(old_tgid) == acc["account_id"]:
            del by_tgid[old_tgid]
        by_tgid[new_tgid] = acc["account_id"]
        acc["linked_tg_id"] = int(body.tg_id)
        acc["linked_tg_username"] = (body.tg_username or "").lstrip("@").lower()
        acc["migrated_at"] = time.time()
        # Записываем историю миграций
        migrations = acc.setdefault("migration_history", [])
        migrations.append({"from_tg": old_tgid, "to_tg": new_tgid, "ts": time.time()})
        if len(migrations) > 20:
            acc["migration_history"] = migrations[-20:]
        await storage._save_unlocked()
    return _public_account(acc)


@router.get("/accounts/by-tgid/{tg_id}")
async def account_by_tgid(
    tg_id: int,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Есть ли у этого tg_id привязанный аккаунт?"""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    acc_id = _accounts_by_tgid().get(str(tg_id))
    if not acc_id:
        return {"account": None}
    acc = _accounts_dict().get(acc_id)
    return {"account": _public_account(acc) if acc else None}


class AccountRotateReq(BaseModel):
    account_id: str
    password: str  # подтверждение


@router.post("/accounts/rotate-secret")
async def account_rotate_secret(
    body: AccountRotateReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Обновить secret_key. Требует пароль для подтверждения."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        acc = _accounts_dict().get(body.account_id)
        if not acc:
            raise HTTPException(404, "account not found")
        if not _verify_pw(acc.get("login") or "", body.password, acc.get("password_hash") or ""):
            raise HTTPException(401, "bad password")
        new_secret = _gen_secret_key()
        acc["secret_hash"] = _hash_secret(new_secret)
        acc["secret_last4"] = new_secret[-4:]
        acc["secret_rotated_at"] = time.time()
        await storage._save_unlocked()
    return {"secret_key": new_secret}


# ═══════════════════════════════════════════════════════════════
# USER PROFILES — avatar + bio, привязано к account_id
# storage: state["ma_profiles"][account_id] = { avatar_url, bio, ts }
# ═══════════════════════════════════════════════════════════════

def _profiles_dict() -> dict:
    return storage.state.setdefault("ma_profiles", {})


class ProfilePatchReq(BaseModel):
    account_id: str
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    by_tg_user_id: int = 0  # C3 fix — кто редактирует


@router.get("/profiles/{account_id}")
async def get_profile(
    account_id: str,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    acc = _accounts_dict().get(account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    prof = _profiles_dict().get(account_id) or {}
    return {
        "account_id": account_id,
        "login": acc.get("login"),
        "linked_tg_username": acc.get("linked_tg_username"),
        "linked_tg_id": acc.get("linked_tg_id"),
        "avatar_url": prof.get("avatar_url") or "",
        "bio": prof.get("bio") or "",
        "updated_ts": prof.get("ts") or 0,
    }


@router.patch("/profiles")
async def patch_profile(
    body: ProfilePatchReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Обновить свой профиль. Гвард: только владелец аккаунта ИЛИ owner системы.
    C3 fix — раньше был TODO, теперь реально проверяем."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    # Owner-check
    acc = _accounts_dict().get(body.account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    is_system_owner = int(body.by_tg_user_id) in _resolve_owner_ids()
    is_account_owner = int(acc.get("linked_tg_id") or 0) == int(body.by_tg_user_id)
    if not (is_system_owner or is_account_owner):
        raise HTTPException(403, "can only edit own profile")

    from storage import _lock as _st_lock
    async with _st_lock:
        profs = _profiles_dict()
        p = profs.setdefault(body.account_id, {})
        if body.avatar_url is not None:
            p["avatar_url"] = body.avatar_url.strip()[:500] or ""
        if body.bio is not None:
            p["bio"] = body.bio.strip()[:500]
        p["ts"] = time.time()
        await storage._save_unlocked()
    return {"ok": True, "profile": p}


# ═══════════════════════════════════════════════════════════════
# SUPPORT TICKETS
# storage: state["support_tickets"] = [ { id, from_tg_id, from_tg_username,
#                                          subject, text, ts, status, replies[] } ]
# ═══════════════════════════════════════════════════════════════

def _support_tickets() -> list:
    return storage.state.setdefault("support_tickets", [])


class SupportTicketReq(BaseModel):
    from_tg_id: int
    from_tg_username: str = ""
    subject: str = ""
    text: str


@router.post("/support/tickets")
async def support_create(
    body: SupportTicketReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    text = (body.text or "").strip()[:5000]
    if not text:
        raise HTTPException(400, "text required")
    from storage import _lock as _st_lock
    async with _st_lock:
        tickets = _support_tickets()
        seq = int(storage.state.get("support_tickets_seq", 0)) + 1
        storage.state["support_tickets_seq"] = seq
        ticket = {
            "id": seq,
            "from_tg_id": int(body.from_tg_id),
            "from_tg_username": (body.from_tg_username or "").lstrip("@").lower(),
            "subject": (body.subject or "").strip()[:200],
            "text": text,
            "ts": time.time(),
            "status": "open",  # open | answered | closed
            "replies": [],
        }
        tickets.append(ticket)
        if len(tickets) > 1000:
            storage.state["support_tickets"] = tickets[-1000:]
        await storage._save_unlocked()
    return {"ticket_id": ticket["id"]}


@router.get("/support/tickets")
async def support_list(
    request: Request,
    status: Optional[str] = None,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    items = list(_support_tickets())
    if status:
        items = [t for t in items if t.get("status") == status]
    items.sort(key=lambda t: -float(t.get("ts") or 0))
    return {"items": items[:100]}


class SupportReplyReq(BaseModel):
    text: str
    author_tg_username: str = ""


@router.post("/support/tickets/{ticket_id}/reply")
async def support_reply(
    ticket_id: int,
    body: SupportReplyReq,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        for t in _support_tickets():
            if int(t.get("id") or 0) == int(ticket_id):
                t.setdefault("replies", []).append({
                    "text": (body.text or "").strip()[:5000],
                    "author": (body.author_tg_username or "admin").lstrip("@").lower(),
                    "ts": time.time(),
                })
                t["status"] = "answered"
                await storage._save_unlocked()
                return {"ok": True}
        raise HTTPException(404, "ticket not found")


@router.post("/support/tickets/{ticket_id}/close")
async def support_close(
    ticket_id: int,
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        for t in _support_tickets():
            if int(t.get("id") or 0) == int(ticket_id):
                t["status"] = "closed"
                await storage._save_unlocked()
                return {"ok": True}
        raise HTTPException(404, "ticket not found")


# ═══════════════════════════════════════════════════════════════
# MAINTENANCE — purge всех не-team чатов (owner-only гвард на stroy)
# ═══════════════════════════════════════════════════════════════

@router.post("/maintenance/purge-workchats")
async def purge_workchats(
    request: Request,
    x_miniapp_signature: str = Header(default=""),
    x_miniapp_ts: str = Header(default=""),
):
    """Удаляет ВСЕ mini-app чаты кроме team-чата."""
    await _check_hmac(request, x_miniapp_signature, x_miniapp_ts)
    from storage import _lock as _st_lock
    async with _st_lock:
        chats = _ma_chats()
        removed = 0
        for cid in list(chats.keys()):
            if cid != TEAM_CHAT_ID:
                del chats[cid]
                removed += 1
        await storage._save_unlocked()
    return {"ok": True, "removed": removed}


@router.get("/healthz")
async def healthz():
    """Публичный health-check без auth — чтобы stroy-crm-bot мог пинговать."""
    return {"ok": True, "service": "miniapp-bridge", "hmac_configured": bool(_hmac_secret())}

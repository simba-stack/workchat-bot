"""FastAPI дашборд для PRIDE.

Endpoints:
  GET /                     — dashboard HTML (single-page app)
  GET /healthz              — Railway healthcheck
  GET /api/state            — snapshot для первичного рендера
  GET /api/lk_cards         — список карточек ЛК с фильтрами
  GET /api/applications     — заявки V2 за период
  GET /api/chats            — managed_chats (рабочие беседы)
  GET /api/deals            — список сделок
  GET /api/events/stream    — SSE стрим событий из event_bus

Авторизация: HTTP Basic, логин/пароль из env DASHBOARD_USER / DASHBOARD_PASS.
Если DASHBOARD_PASS не задан — дашборд отдаёт 503 (защита от случайного
открытого endpoint'а в проде).

Принципиально READ-ONLY: эндпоинты не пишут в storage. Управление через
команды в Telegram-чатах (как сейчас).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from fastapi import (
    FastAPI, Request, Depends, HTTPException, WebSocket, WebSocketDisconnect,
    UploadFile, File,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import event_bus
import config
from storage import storage

logger = logging.getLogger(__name__)

app = FastAPI(title="PRIDE Dashboard", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _backfill_payouts_on_startup():
    """Бэкфилл: карточки со статусом ПОПОЛНИТЬ_И_ОТПУСТИТЬ или ОТРАБОТАН,
    которые по какой-то причине не попали в очереди выплат, добавляем
    в правильную очередь по payment_method.
    Идемпотентно — пропускаем если карточка уже в какой-то очереди."""
    try:
        storage.reload_sync()
        # Шаг 0: чистка дублей в очередях (если из-за прошлых багов их там накопилось)
        try:
            removed = await storage.dedupe_payouts()
            if removed:
                logger.info("[startup-backfill] deduped %d duplicate payout entries", removed)
        except Exception as e:
            logger.warning("startup dedupe error: %s", e)
        cards = storage.list_lk_cards() or {}
        added = 0
        for cid, card in cards.items():
            try:
                if not cid:
                    continue
                status = (card.get("status") or "").upper()
                method = (card.get("payment_method") or "").upper()
                # Шорткат-статус: миграция на ОТРАБОТАН + метод по умолчанию
                if status == "ПОПОЛНИТЬ_И_ОТПУСТИТЬ":
                    await storage.set_lk_card_status(cid, "ОТРАБОТАН", by="backfill")
                    if not method:
                        await storage.update_lk_card(cid, payment_method="GUARANTOR_AFTER_WORK")
                        method = "GUARANTOR_AFTER_WORK"
                    status = "ОТРАБОТАН"
                if status != "ОТРАБОТАН":
                    continue
                # Уже в очереди? — пропуск
                if storage.find_payout_by_card(cid):
                    continue
                # Положить в правильную очередь
                if method == "USDT_TRC20" and (card.get("usdt_address") or ""):
                    await storage.add_payout("usdt", {
                        "card_id": cid, "bank": card.get("bank") or "",
                        "fio": card.get("fio") or "", "supplier": card.get("supplier") or "",
                        "work_chat_id": card.get("work_chat_id") or 0,
                        "usdt_address": card.get("usdt_address") or "",
                        "amount_usdt": float(card.get("price_usdt") or 0),
                    })
                    added += 1
                elif method in ("GUARANTOR_AFTER_WORK", "GUARANTOR_AFTER"):
                    await storage.add_payout("fund_release", {
                        "card_id": cid, "bank": card.get("bank") or "",
                        "fio": card.get("fio") or "", "supplier": card.get("supplier") or "",
                        "work_chat_id": card.get("work_chat_id") or 0,
                        "amount_usdt": float(card.get("price_usdt") or 0),
                        "deal_id": card.get("deal_id") or "",
                    })
                    added += 1
                elif method == "GUARANTOR_BEFORE":
                    await storage.add_payout("release", {
                        "card_id": cid, "bank": card.get("bank") or "",
                        "fio": card.get("fio") or "", "supplier": card.get("supplier") or "",
                        "work_chat_id": card.get("work_chat_id") or 0,
                        "amount_usdt": float(card.get("price_usdt") or 0),
                        "deal_id": card.get("deal_id") or "",
                    })
                    added += 1
            except Exception as e:
                logger.warning("backfill card %s failed: %s", cid, e)
        if added:
            logger.info("[startup-backfill] added %d cards to payout queues", added)
    except Exception as e:
        logger.warning("startup backfill error: %s", e)
security = HTTPBasic(auto_error=False)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

# Telegram OAuth: ID-владельцы которым разрешён вход через Telegram Login.
# Авто-заполняется из config.ADMIN_ID если env пуст.
_tg_admins_raw = os.getenv("TG_ADMINS", "") or str(config.ADMIN_ID or "")
TG_ADMINS = {
    int(x.strip()) for x in _tg_admins_raw.split(",")
    if x.strip().isdigit()
}
# Захардкоженный список админов (на случай если env потерян / новый член команды).
# Эти ID имеют доступ к дашборду ВСЕГДА — независимо от env TG_ADMINS.
_HARDCODED_TG_ADMINS = {
    8151738775,   # SIMBA (owner)
    397572312,    # admin
    5830088389,   # admin (added 2026-05-14)
    8328099603,   # admin (added 2026-05-14)
    7552445074,   # admin (added 2026-05-15)
    8232753590,   # admin (added 2026-05-15)
    8548697416,   # admin (added 2026-05-15)
}
TG_ADMINS |= _HARDCODED_TG_ADMINS
# Username бота (без @) — нужен для Telegram Login Widget.
# Авто-резолвится из BOT_TOKEN через getMe если env не задан (см. ниже).
TG_BOT_USERNAME = (os.getenv("TG_BOT_USERNAME", "") or "").lstrip("@").strip()
# Секрет для подписи session cookies. Хранится в storage.state чтоб переживал
# рестарты процесса (если volume настроен).
_env_session_secret = os.getenv("SESSION_SECRET", "")
SESSION_TTL_SEC = int(os.getenv("SESSION_TTL_DAYS", "30")) * 86400
SESSION_COOKIE = "jarvis_session"
# Строгий режим: только Telegram OAuth, Basic-auth выключен.
# По умолчанию — strict если есть TG_ADMINS и BOT_TOKEN.
AUTH_STRICT_TELEGRAM = (
    os.getenv("AUTH_STRICT_TELEGRAM", "auto") or "auto"
).lower()


def _get_session_secret() -> str:
    """Возвращает SESSION_SECRET. Если env задан — оттуда. Иначе — из
    storage.state (генерится при первом старте, переживает рестарты если
    есть volume)."""
    if _env_session_secret:
        return _env_session_secret
    s = storage.state.get("session_secret")
    if not s:
        s = secrets.token_urlsafe(32)
        # Запишем синхронно — мы внутри async-приложения, но эта функция
        # вызывается на этапе bootstrap
        try:
            storage.state["session_secret"] = s
            # сохранение асинхронное — пометим что нужно сохранить
            asyncio.get_event_loop().create_task(storage._save_unlocked())
        except Exception:
            pass
    return s


# Кеш для bot username из getMe
_bot_username_cache = {"value": TG_BOT_USERNAME, "checked": False}


async def _resolve_bot_username() -> str:
    """Если TG_BOT_USERNAME не задан в env — резолвим через Telegram getMe."""
    if _bot_username_cache["value"]:
        return _bot_username_cache["value"]
    if _bot_username_cache["checked"]:
        return ""  # уже пытались
    _bot_username_cache["checked"] = True
    if not config.BOT_TOKEN:
        return ""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as cli:
            r = await cli.get(f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe")
            if r.status_code == 200:
                data = r.json()
                if data.get("ok"):
                    uname = (data.get("result") or {}).get("username") or ""
                    _bot_username_cache["value"] = uname
                    logger.info("[auth] resolved bot username: @%s", uname)
                    return uname
    except Exception as e:
        logger.warning("[auth] getMe failed: %s", e)
    return ""


def _is_strict_telegram() -> bool:
    """Включён ли strict mode (только Telegram, без Basic)."""
    if AUTH_STRICT_TELEGRAM == "1" or AUTH_STRICT_TELEGRAM == "true":
        return True
    if AUTH_STRICT_TELEGRAM == "0" or AUTH_STRICT_TELEGRAM == "false":
        return False
    # auto: strict если есть BOT_TOKEN и TG_ADMINS (значит OAuth настроен)
    return bool(config.BOT_TOKEN and TG_ADMINS)

_HTML_PATH = Path(__file__).parent / "dashboard" / "index.html"
_JARVIS_PATH = Path(__file__).parent / "dashboard" / "jarvis.html"
_JARVIS_MOBILE_PATH = Path(__file__).parent / "dashboard" / "jarvis-mobile.html"
DASHBOARD_DEFAULT = (os.getenv("DASHBOARD_DEFAULT", "jarvis") or "").lower()


def _is_mobile_ua(request: Request) -> bool:
    """Простая UA-детекция мобильных устройств."""
    ua = (request.headers.get("user-agent") or "").lower()
    mobile_markers = ["mobile", "android", "iphone", "ipad", "ipod"]
    return any(m in ua for m in mobile_markers)


def _sign_session(user_id: int) -> str:
    """Подписанный куки: '{uid}.{ts}.{hmac}'."""
    payload = f"{int(user_id)}.{int(time.time())}"
    sig = hmac.new(
        _get_session_secret().encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def _verify_session(cookie: str) -> Optional[int]:
    """Возвращает user_id из куки если подпись + ttl ОК, иначе None."""
    try:
        uid_s, ts_s, sig = cookie.rsplit(".", 2)
        payload = f"{uid_s}.{ts_s}"
        expected = hmac.new(
            _get_session_secret().encode(), payload.encode(), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(time.time()) - int(ts_s) > SESSION_TTL_SEC:
            return None
        return int(uid_s)
    except Exception:
        return None


def _verify_telegram_login(data: dict) -> bool:
    """Проверяет hash от Telegram Login Widget.
    Алгоритм: https://core.telegram.org/widgets/login#checking-authorization
      1. data_check_string = sort(keys, без 'hash'), join('\\n', f'{k}={v}')
      2. secret = SHA256(bot_token)
      3. expected = HMAC-SHA256(secret, data_check_string)
      4. hmac.compare_digest(expected, received_hash)
    Также проверяем auth_date свежий (< 1 час).
    """
    if not config.BOT_TOKEN:
        return False
    received_hash = (data.get("hash") or "").strip()
    if not received_hash:
        return False
    # Сборка строки сверки
    keys = sorted(k for k in data.keys() if k != "hash")
    check_string = "\n".join(f"{k}={data[k]}" for k in keys)
    secret = hashlib.sha256(config.BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return False
    # Свежесть auth_date (защита от replay): max 3600 сек
    try:
        auth_age = int(time.time()) - int(data.get("auth_date", 0))
        if auth_age > 3600:
            return False
    except (ValueError, TypeError):
        return False
    return True


def _load_html(which: str = "index") -> str:
    """Читает HTML дашборда с диска."""
    path = _JARVIS_PATH if which == "jarvis" else _HTML_PATH
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            f"<!DOCTYPE html><html><head><title>Dashboard</title></head>"
            f"<body><h1>{path.name} не найден</h1></body></html>"
        )


def _try_session_auth(request: Request) -> Optional[int]:
    """Если в request есть валидный session cookie с Telegram user_id из
    TG_ADMINS — возвращает user_id. Иначе None."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    uid = _verify_session(cookie)
    if uid is None:
        return None
    if TG_ADMINS and uid not in TG_ADMINS:
        return None
    return uid


# Хардкоженные владельцы дашборда — полный доступ ко всему всегда
OWNER_UIDS_HARDCODED = {
    8151738775,   # SIMBA (главный)
    397572312,    # TIMON
}


def _resolve_user_role(uid: int) -> str:
    """Определяет роль пользователя дашборда:
      'owner' — видит всё; 'manager' — только department=managers;
      'system' — только system; 'accounting' — только accounting.
    Owner: SIMBA + TIMON хардкод + ADMIN_ID env. Остальное — через
    worker_sessions[uid].username → storage.get_worker_role(username).role."""
    OWNER_UIDS = set(OWNER_UIDS_HARDCODED)
    try:
        if config.ADMIN_ID:
            OWNER_UIDS.add(int(config.ADMIN_ID))
    except Exception:
        pass
    # Доп — из storage state.owner_uids (юзер может добавить через админку)
    try:
        for u in (storage.state.get("owner_uids") or []):
            try: OWNER_UIDS.add(int(u))
            except Exception: pass
    except Exception:
        pass
    if uid in OWNER_UIDS:
        return "owner"
    # Пытаемся через worker_sessions
    try:
        sess = storage.get_worker_session(uid) or {}
        uname = (sess.get("username") or "").lstrip("@").lower().strip()
        if uname:
            r = storage.get_worker_role(uname) or {}
            role = (r.get("role") or "").lower().strip()
            if role in ("owner", "manager", "system", "accounting", "operationist"):
                return role
    except Exception:
        pass
    # Дефолт — менеджер
    return "manager"


def _try_basic_auth(credentials: Optional[HTTPBasicCredentials]) -> bool:
    if not DASHBOARD_PASS:
        return False
    if credentials is None:
        return False
    user_ok = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    pass_ok = secrets.compare_digest(credentials.password, DASHBOARD_PASS)
    return user_ok and pass_ok


def _check_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials],
) -> None:
    """Принимает либо Telegram session cookie, либо (если не strict) Basic.
    Strict-режим: только Telegram, без fallback. 401 → дашборд редиректит на /login.
    """
    strict = _is_strict_telegram()
    no_auth_configured = (
        not DASHBOARD_PASS and not (config.BOT_TOKEN and TG_ADMINS)
    )
    if no_auth_configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "Dashboard auth not configured. Set DASHBOARD_USER/DASHBOARD_PASS "
                "OR BOT_TOKEN + TG_ADMINS (with ADMIN_ID as fallback)."
            ),
        )
    # 1) Telegram session cookie
    if _try_session_auth(request) is not None:
        return
    # 2) Basic auth — только если strict выключен
    if not strict and _try_basic_auth(credentials):
        return
    # 3) Не авторизован
    if strict:
        # для HTML-запросов отдадим редирект через спецзаголовок
        raise HTTPException(
            status_code=401,
            detail="Telegram login required — open /login",
        )
    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


def _auth(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    _check_auth(request, credentials)


def _get_me(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> dict:
    """Возвращает текущего пользователя как dict: {tg_id, username, role}.
    Используется в endpoints, которым нужна роль для проверки прав."""
    _check_auth(request, credentials)
    uid = _try_session_auth(request) or 0
    role = _resolve_user_role(uid or 0)
    tg_info = storage.get_tg_user_info(uid) if uid else {}
    tg_info = tg_info or {}
    return {
        "tg_id": uid,
        "username": tg_info.get("username") or "",
        "first_name": tg_info.get("first_name") or "",
        "role": role,
    }


# === Static & health ===

@app.get("/healthz")
async def healthz():
    """Public healthcheck — без авторизации (нужно для Railway)."""
    return {"status": "ok", "subscribers": event_bus.subscriber_count()}


# ============== HELPDESK / SUPPORT ==============

@app.get("/api/me")
async def api_me(request: Request, _: None = Depends(_auth)):
    """Профиль текущего пользователя дашборда.
    Источники:
      - uid: из session cookie (Telegram OAuth widget)
      - first_name/username/photo: storage.tg_user_info[uid] (заполняется при login)
      - role: storage.worker_roles + hardcoded OWNER_UIDS
      - tg_session_connected: storage.worker_sessions[uid] — отдельная Telethon-сессия
        для отправки сообщений ОТ СВОЕГО ИМЕНИ (не обязательно, fallback на ASSISTANT).
    """
    uid = _try_session_auth(request) or 0
    role = _resolve_user_role(uid or 0)
    # Базовая инфа из TG login widget
    tg_info = storage.get_tg_user_info(uid) if uid else {}
    tg_info = tg_info or {}
    # Telethon-сессия (опционально — для отправки от своего имени)
    sess = storage.get_worker_session(uid) if uid else None
    sess = sess or {}
    can_see = {
        "support_all_depts": role == "owner",
        "support_managers": role in ("owner", "manager"),
        "support_system": role in ("owner", "system"),
        "support_accounting": role in ("owner", "accounting"),
        "system_panel": role in ("owner", "system", "operationist"),
        "accounting_panel": role in ("owner", "accounting"),
        "admin_workers": role == "owner",
        "all_views": role == "owner",
    }
    # Полный effective_permissions для frontend (фильтрация views/subviews/actions)
    eff_perms = {}
    try:
        if hasattr(storage, "effective_permissions"):
            eff_perms = storage.effective_permissions(role) or {}
    except Exception as e:
        logger.warning("effective_permissions(%s) failed: %s", role, e)
    return {
        "ok": True,
        "uid": uid,
        "role": role,
        # Имя/username берём из TG OAuth, fallback на worker_session
        "username": tg_info.get("username") or sess.get("username") or "",
        "first_name": tg_info.get("first_name") or sess.get("first_name") or "",
        "last_name": tg_info.get("last_name") or "",
        "photo_url": tg_info.get("photo_url") or "",
        "phone": sess.get("phone") or "",
        # Telethon-сессия для отправки от своего имени — ОТДЕЛЬНАЯ привязка
        "tg_session_connected": bool(sess.get("string_session")),
        "permissions": can_see,  # backward-compat
        # Новая система ролей с subviews + readonly
        "perms": {
            "views": eff_perms.get("views", []),
            "view_readonly": eff_perms.get("view_readonly", []),
            "subviews": eff_perms.get("subviews", {}),
            "subview_readonly": eff_perms.get("subview_readonly", {}),
            "edit_actions": eff_perms.get("edit_actions", []),
            "label": eff_perms.get("label", role),
        },
    }


# === Permission dependency для mutation endpoints ===
def require_action(action: str):
    """Создаёт FastAPI dependency которая проверяет что у юзера есть это действие.

    Использование:
        @app.post("/api/x")
        async def x(_perm = Depends(require_action("settings_pricing_set"))):
            ...
    """
    async def _check(request: Request, me: dict = Depends(_get_me)):
        role = (me or {}).get("role") or ""
        # Owner = всё разрешено
        if role == "owner":
            return True
        try:
            if storage.role_can_edit(role, action):
                return True
        except Exception:
            pass
        raise HTTPException(403, f"Forbidden: action '{action}' not allowed for role '{role}'")
    return _check


@app.get("/api/admin/workers")
async def api_admin_workers(request: Request, _: None = Depends(_auth)):
    """Список работников + их ролей (для админки). Только owner может видеть."""
    uid = _try_session_auth(request) or 0
    if _resolve_user_role(uid or 0) != "owner":
        raise HTTPException(403, "owner only")
    storage.reload_sync()
    workers = list(storage.get_workers() or [])  # usernames
    roles = storage.list_worker_roles() or {}
    sessions = (storage.state.get("worker_sessions") or {})
    # Сопоставим uid → username из worker_sessions
    uname_to_uid = {}
    for uid_str, s in sessions.items():
        un = (s.get("username") or "").lstrip("@").lower()
        if un:
            uname_to_uid[un] = int(uid_str)
    out = []
    for uname in workers:
        key = uname.lstrip("@").lower()
        r = roles.get(key) or {}
        out.append({
            "username": uname.lstrip("@"),
            "role": r.get("role") or "manager",
            "is_admin": bool(r.get("is_admin")),
            "uid": uname_to_uid.get(key, 0),
            "session_connected": key in uname_to_uid,
        })
    out.sort(key=lambda x: x["username"].lower())
    return {"ok": True, "workers": out, "available_roles": ["manager", "system", "accounting", "operationist", "owner"]}


@app.delete("/api/admin/workers/{username}")
async def api_admin_remove_worker(username: str, request: Request, _: None = Depends(_auth)):
    """Убрать username из state.workers (юзербот больше не пригласит его
    в новые work_chat'ы). Заодно чистит worker_roles. Только owner."""
    uid = _try_session_auth(request) or 0
    if _resolve_user_role(uid or 0) != "owner":
        raise HTTPException(403, "owner only")
    uname = (username or "").lstrip("@").strip()
    if not uname:
        raise HTTPException(400, "username required")
    await storage.remove_worker(uname)  # уже чистит worker_roles внутри
    return {"ok": True, "username": uname, "removed": True}


@app.post("/api/admin/workers/{username}/role")
async def api_admin_set_role(username: str, request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("admin_worker_role"))):
    """Установить роль работника. Только owner."""
    uid = _try_session_auth(request) or 0
    if _resolve_user_role(uid or 0) != "owner":
        raise HTTPException(403, "owner only")
    data = await request.json()
    new_role = (data.get("role") or "").strip().lower()
    if new_role not in ("manager", "system", "accounting", "operationist", "owner"):
        raise HTTPException(400, "invalid role")
    is_admin = bool(data.get("is_admin", True))
    uname_clean = username.lstrip("@").strip()
    if not uname_clean:
        raise HTTPException(400, "username required")
    ok = await storage.set_worker_role(uname_clean, new_role, is_admin=is_admin)
    if not ok:
        raise HTTPException(500, "failed to set role")
    try:
        event_bus.emit_event("admin-role-changed", {
            "username": uname_clean, "role": new_role, "is_admin": is_admin,
            "by_uid": uid,
        })
    except Exception:
        pass
    return {"ok": True, "username": uname_clean, "role": new_role, "is_admin": is_admin}


@app.get("/api/support/inbox")
async def api_support_inbox(
    status: Optional[str] = None,
    department: Optional[str] = None,
    request: Request = None,
    _: None = Depends(_auth),
):
    """Inbox чатов с role-based фильтром:
      owner — всё (можно фильтровать через ?department=...);
      manager — только chat.department='managers' + awaiting_department;
      system  — только chat.department='system';
      accounting — только chat.department='accounting'.
    """
    storage.reload_sync()
    # Определяем роль текущего пользователя
    uid = _try_session_auth(request) if request else 0
    role = _resolve_user_role(uid or 0)
    # Если не owner — принудительно ставим department по роли
    effective_dept = department
    if role != "owner":
        if role == "manager":
            effective_dept = "managers"
        elif role == "system":
            effective_dept = "system"
        elif role == "accounting":
            effective_dept = "accounting"
        elif role == "operationist":
            # Операционист видит system-чаты (как сус) — для финализации
            effective_dept = "system"
    # Фетчим
    if status:
        chats = storage.list_support_inbox(status=status, department=effective_dept)
    else:
        # awaiting_department показываем только менеджерам и owner
        # (системе/бухгалтерии незачем — клиент ещё не выбрал их).
        chats_aw = []
        if role in ("owner", "manager"):
            chats_aw = storage.list_support_inbox(
                status="awaiting_department",
                department=None if role == "owner" else None,  # awaiting не имеет dept
            )
        ch1 = storage.list_support_inbox(status="operator_requested", department=effective_dept)
        ch2 = storage.list_support_inbox(status="in_progress", department=effective_dept)
        chats = chats_aw + ch1 + ch2
    return {
        "ok": True,
        "chats": chats,
        "viewer_role": role,
        "viewer_uid": uid or 0,
    }


@app.get("/api/support/chat/{chat_id}/info")
async def api_support_chat_info(chat_id: int, _: None = Depends(_auth)):
    """Возвращает support-state + базовые данные о клиенте + список ЛК-карточек."""
    storage.reload_sync()
    info = storage.get_chat_info(chat_id)
    if not info:
        raise HTTPException(404, "chat not found")
    # ЛК-карточки этого work_chat
    lks = []
    try:
        for cid, c in (storage.list_lk_cards() or {}).items():
            if (c.get("work_chat_id") or 0) == int(chat_id):
                lks.append({
                    "card_id": cid,
                    "bank": c.get("bank") or "",
                    "fio": c.get("fio") or "",
                    "price_usdt": c.get("price_usdt") or 0,
                    "payment_method": c.get("payment_method") or "",
                    "status": c.get("status") or "",
                    "deal_id": c.get("deal_id") or "",
                })
    except Exception as e:
        logger.warning("inbox chat lks: %s", e)
    return {
        "ok": True,
        "chat_id": chat_id,
        "client_name": info.get("client_name") or "",
        "client_username": info.get("client_username") or "",
        "client_id": info.get("client_id") or 0,
        "support": info.get("support") or {},
        "lk_cards": lks,
    }


@app.post("/api/support/chat/{chat_id}/take")
async def api_support_take(chat_id: int, request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("support_take"))):
    """Менеджер берёт чат на себя — клиенту шлём 'X присоединился к чату'."""
    manager_uid = _try_session_auth(request) or 0
    if not manager_uid:
        raise HTTPException(401, "auth required")
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    dept = data.get("department") or "managers"
    ok = await storage.support_take(chat_id, manager_uid=manager_uid, department=dept)
    if not ok:
        raise HTTPException(404, "chat not found")
    # Получаем имя менеджера: из worker_sessions (если он логинился TG-сессией)
    # или из @username admin-сессии. По умолчанию — "Оператор".
    mgr_label = "Оператор"
    try:
        sess = storage.get_worker_session(manager_uid) or {}
        phone = (sess.get("phone") or "").strip()
        first = (sess.get("first_name") or "").strip()
        nick = (sess.get("username") or "").lstrip("@").strip()
        if first:
            mgr_label = first + (f" (@{nick})" if nick else "")
        elif nick:
            mgr_label = f"@{nick}"
        elif phone:
            mgr_label = f"Оператор"
    except Exception:
        pass
    # Уведомление в чат клиента — через userbot dashboard_commands
    try:
        await storage.enqueue_dashboard_command(
            f"__support_take_notify {chat_id} {manager_uid} {mgr_label}",
            source="dashboard-support-take",
        )
    except Exception:
        pass
    try:
        event_bus.emit_event("support-chat-taken", {
            "chat_id": chat_id, "manager_uid": manager_uid, "department": dept,
            "manager_label": mgr_label,
        })
    except Exception:
        pass
    return {"ok": True, "chat_id": chat_id, "manager_uid": manager_uid, "manager_label": mgr_label}


@app.post("/api/support/chat/{chat_id}/reply")
async def api_support_reply(chat_id: int, request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("support_reply"))):
    """Отправка ответа менеджера в work_chat клиента.
    Body: { text: str, as_assistant: bool (default false) }
      as_assistant=True → шлём через PRIDE ASSISTANT
      as_assistant=False (default) → шлём через сессию менеджера (если подключена),
        иначе fallback на PRIDE ASSISTANT.
    """
    manager_uid = _try_session_auth(request) or 0
    data = await request.json()
    text = (data.get("text") or "").strip()
    as_assistant = bool(data.get("as_assistant", False))
    if not text:
        raise HTTPException(400, "text required")
    if len(text) > 4000:
        raise HTTPException(400, "text too long (max 4000)")
    # Используем спец-команду если as_assistant=True
    cmd_prefix = "__support_reply_assistant" if as_assistant else "__support_reply"
    try:
        await storage.enqueue_dashboard_command(
            f"{cmd_prefix} {chat_id} {manager_uid} {text}",
            source="dashboard-support-reply",
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "chat_id": chat_id, "queued": True, "as_assistant": as_assistant}


@app.post("/api/support/chat/{chat_id}/transfer")
async def api_support_transfer(chat_id: int, request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("support_transfer"))):
    """Передать чат в другое подразделение + уведомить клиента."""
    manager_uid = _try_session_auth(request) or 0
    data = await request.json()
    dept = data.get("department") or ""
    if dept not in ("managers", "system", "accounting"):
        raise HTTPException(400, "invalid department")
    ok = await storage.support_transfer(chat_id, dept, from_manager=manager_uid)
    if not ok:
        raise HTTPException(404, "chat not found")
    # Уведомление клиенту через userbot
    dept_label = {
        "managers": "👤 Менеджеры",
        "system": "⚙️ System (перевязка/установка ЛК)",
        "accounting": "💰 Бухгалтерия (выплаты/финансы)",
    }.get(dept, dept)
    try:
        await storage.enqueue_dashboard_command(
            f"__support_transfer_notify {chat_id} {dept}|||{dept_label}",
            source="dashboard-support-transfer",
        )
    except Exception:
        pass
    try:
        event_bus.emit_event("support-chat-transferred", {
            "chat_id": chat_id, "department": dept,
            "department_label": dept_label, "from": manager_uid,
        })
    except Exception:
        pass
    return {"ok": True, "chat_id": chat_id, "department": dept, "department_label": dept_label}


@app.post("/api/support/chat/{chat_id}/close")
async def api_support_close(chat_id: int, request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("support_close"))):
    """Закрыть саппорт-сессию + теги (купил_РС/отказался/молчит/передал_дальше)."""
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    rating = int(data.get("rating") or 0)
    note = (data.get("note") or "").strip()
    tag = (data.get("tag") or "").strip()
    if rating < 0 or rating > 5:
        raise HTTPException(400, "rating must be 0-5")
    ok = await storage.support_close(chat_id, rating=rating)
    if not ok:
        raise HTTPException(404, "chat not found")
    if tag or note:
        await storage.set_support_state(chat_id, close_tag=tag, close_note=note)
    # Asynchronous: snять AI silence через userbot
    try:
        await storage.enqueue_dashboard_command(
            f"__support_after_close {chat_id}",
            source="dashboard-support-close",
        )
    except Exception:
        pass
    try:
        event_bus.emit_event("support-chat-closed", {
            "chat_id": chat_id, "rating": rating, "tag": tag,
        })
    except Exception:
        pass
    return {"ok": True, "chat_id": chat_id, "rating": rating, "tag": tag}


@app.get("/api/support/templates")
async def api_support_templates(_: None = Depends(_auth)):
    """Quick reply шаблоны для оператора. Берёт из storage.support_templates
    или возвращает дефолты + актуальный прайс."""
    storage.reload_sync()
    custom = (storage.state.get("support_templates") or {})
    # Генерим прайс динамически
    price_lines = []
    try:
        prices = storage.state.get("lk_prices") or {}
        if prices:
            for bank, p in sorted(prices.items(), key=lambda x: x[0]):
                price_lines.append(f"• {bank}: {p}$")
        else:
            price_lines.append("Цены уточняются у менеджера.")
    except Exception:
        price_lines.append("Цены уточняются у менеджера.")
    default_price = "💰 <b>Прайс на РС/ЛК:</b>\n\n" + "\n".join(price_lines)
    defaults = {
        "price": custom.get("price") or default_price,
        "terms": custom.get("terms") or (
            "📋 <b>Условия работы:</b>\n\n"
            "✅ Работаем через гарант-сервис Continental или прямые USDT TRC20\n"
            "✅ Полная анонимность сделки\n"
            "✅ Поддержка 24/7\n"
            "✅ Гарантия чистоты ЛК"
        ),
        "hold": custom.get("hold") or (
            "⏳ <b>Холд (заморозка ЛК):</b>\n\n"
            "Если ЛК нужен временно, мы можем заморозить его за вами:\n"
            "• 50$/месяц\n"
            "• Полная сохранность данных\n"
            "• Возврат в любой момент"
        ),
        "deal_instructions": custom.get("deal_instructions") or (
            "🔄 <b>Инструкция по сделке:</b>\n\n"
            "1️⃣ Согласуем банк, цену и метод оплаты\n"
            "2️⃣ Вы вносите средства (Continental или USDT TRC20)\n"
            "3️⃣ Мы передаём вам данные ЛК + дедик\n"
            "4️⃣ Перепривязка ЛК через СМС-код\n"
            "5️⃣ Готово — ЛК ваш"
        ),
    }
    return {"ok": True, "templates": defaults}


@app.get("/api/support/chat/{chat_id}/messages")
async def api_support_messages(
    chat_id: int, limit: int = 50, refresh: int = 0,
    _: None = Depends(_auth),
):
    """Сообщения work_chat. refresh=1 — принудительно дёрнуть userbot за историей."""
    if limit > 200:
        limit = 200
    cache = storage.state.setdefault("support_msg_cache", {})
    from storage import _norm_chat_id as _nrm
    key = str(_nrm(chat_id))
    msgs = cache.get(key, [])
    need_fetch = refresh == 1 or len(msgs) < 5
    if need_fetch:
        try:
            await storage.enqueue_dashboard_command(
                f"__support_fetch_messages {chat_id} {limit}",
                source="dashboard-support-msgs",
            )
        except Exception:
            pass
    return {
        "ok": True, "chat_id": chat_id,
        "messages": msgs[-limit:],
        "fetching": need_fetch,
    }


# ============== MANAGER TG SESSIONS ==============
# Хранилище pending login-клиентов: manager_uid -> {client, phone, phone_code_hash}
# В памяти процесса — на время одной login-сессии (5-15 минут).
_pending_login_clients: dict = {}


@app.get("/api/support/me/session/status")
async def api_manager_session_status(request: Request, _: None = Depends(_auth)):
    """Проверка статуса TG-сессии текущего менеджера."""
    manager_uid = _try_session_auth(request) or 0
    if not manager_uid:
        return {"ok": False, "connected": False, "error": "not_authenticated"}
    sess = storage.get_worker_session(manager_uid)
    if not sess:
        return {"ok": True, "connected": False}
    return {
        "ok": True, "connected": True,
        "phone": sess.get("phone") or "",
        "connected_at": sess.get("connected_at") or 0,
    }


@app.post("/api/support/me/session/connect")
async def api_manager_session_connect(request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("tg_session_connect"))):
    """Старт TG-логина: получаем телефон, отправляем код."""
    manager_uid = _try_session_auth(request) or 0
    if not manager_uid:
        raise HTTPException(401, "auth required")
    data = await request.json()
    phone = (data.get("phone") or "").strip()
    if not phone or len(phone) < 8:
        raise HTTPException(400, "phone required (+7...)")
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        cli = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
        await cli.connect()
        sent = await cli.send_code_request(phone)
        # Сохраняем pending-объект до завершения логина
        _pending_login_clients[manager_uid] = {
            "client": cli, "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "created_at": _time.time() if False else __import__("time").time(),
        }
        return {"ok": True, "code_sent": True, "phone": phone}
    except Exception as e:
        logger.exception("manager session connect failed: %s", e)
        raise HTTPException(500, f"connect failed: {e}")


@app.post("/api/support/me/session/verify")
async def api_manager_session_verify(request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("tg_session_verify"))):
    """Завершение TG-логина: код + опциональный 2FA-пароль."""
    manager_uid = _try_session_auth(request) or 0
    if not manager_uid:
        raise HTTPException(401, "auth required")
    pending = _pending_login_clients.get(manager_uid)
    if not pending:
        raise HTTPException(400, "no pending login — start with /connect first")
    data = await request.json()
    code = (data.get("code") or "").strip()
    password = (data.get("password") or "").strip()
    if not code:
        raise HTTPException(400, "code required")
    cli = pending["client"]
    phone = pending["phone"]
    phone_code_hash = pending["phone_code_hash"]
    try:
        from telethon.errors import SessionPasswordNeededError
        try:
            await cli.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                return {"ok": False, "need_password": True}
            await cli.sign_in(password=password)
        # Сохраняем зашифрованную сессию
        from storage import encrypt_session
        from telethon.sessions import StringSession
        string_sess = cli.session.save()
        encrypted = encrypt_session(string_sess)
        await storage.set_worker_session(manager_uid, encrypted, phone=phone)
        # Закрываем pending client (он больше не нужен в этом коннекте)
        try:
            await cli.disconnect()
        except Exception:
            pass
        _pending_login_clients.pop(manager_uid, None)
        return {"ok": True, "connected": True, "phone": phone}
    except Exception as e:
        logger.exception("manager session verify failed: %s", e)
        raise HTTPException(400, f"verify failed: {e}")


@app.post("/api/support/me/session/disconnect")
async def api_manager_session_disconnect(request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("tg_session_disconnect"))):
    """Удалить сохранённую TG-сессию менеджера."""
    manager_uid = _try_session_auth(request) or 0
    if not manager_uid:
        raise HTTPException(401, "auth required")
    ok = await storage.remove_worker_session(manager_uid)
    # Если был pending — закрыть
    pending = _pending_login_clients.pop(manager_uid, None)
    if pending:
        try:
            await pending["client"].disconnect()
        except Exception:
            pass
    return {"ok": True, "removed": ok}


# ============== END MANAGER SESSIONS ==============


# ============== SYSTEM DEPARTMENT (SMS / ДОСТУПЫ / ПАРОЛИ) ==============

def _compute_lk_slot(drop: dict, droplk_id: str) -> tuple:
    """Возвращает (slot_number, slot_total) — порядковый номер ЛК внутри анкеты.
    slot_number = 1..N (индекс в drop.lk_card_ids + 1), 0 если не найден.
    slot_total = N (всего ЛК в анкете). Применимо и для crm_drops, и для credit_drops.
    """
    try:
        siblings = (drop or {}).get("lk_card_ids") or []
        total = len(siblings)
        if droplk_id in siblings:
            return (siblings.index(droplk_id) + 1, total)
        return (0, total)
    except Exception:
        return (0, 0)


@app.get("/api/system/pending_lk")
async def api_system_pending_lk(
    period: Optional[str] = None,  # 'day' | 'week' | 'month' | None
    _: None = Depends(_auth),
):
    """Список ЛК-карточек в активном SMS-флоу: ждут код входа, перевязки или
    в процессе. Источник — crm_drop_lks из storage (CRM-бот ведёт sms_stage)."""
    storage.reload_sync()
    lks_raw = (
        storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}
    ) or {}
    drops_raw = (
        storage.list_crm_drops() if hasattr(storage, "list_crm_drops") else {}
    ) or {}
    out = []
    # Сортировка стадий: пустые (новые анкеты) первыми, потом активные, done в конце
    stage_order = {
        "": 0,
        "ready_asked": 1,
        "ready_confirmed": 2,
        "login_asked": 3,
        "login_received": 4,
        "perevyaz_asked": 5,
        "perevyaz_received": 6,
        "done": 99,
    }
    for lkid, lk in lks_raw.items():
        stage = (lk.get("sms_stage") or "").strip()
        # ЛК со стадией "done" (успешно перевязаны) ОСТАЮТСЯ в Доступах
        # с флагом completed=True. UI рисует «✅ Успешно перевязано».
        drop = drops_raw.get(lk.get("drop_id"), {}) if drops_raw else {}
        # Резолв supplier: из drop.supplier / owner_id → owner.username
        supplier = (drop.get("supplier") or "").lstrip("@")
        if not supplier and drop.get("owner_id"):
            try:
                owner = storage.get_crm_owner(drop["owner_id"]) or {}
                supplier = (owner.get("username") or "").lstrip("@")
            except Exception:
                pass
        # TG-ссылки: ДОСТУПЫ = -1003852131311, ПАРОЛИ = -1003788743917
        access_chat_id = -1003852131311
        pass_chat_id = -1003788743917
        sms_msg_id = lk.get("sms_tracker_msg_id") or 0
        pass_msg_id = lk.get("msgid_pass") or 0
        tg_access_link = (
            f"https://t.me/c/{str(access_chat_id)[4:]}/{sms_msg_id}"
            if sms_msg_id else ""
        )
        tg_pass_link = (
            f"https://t.me/c/{str(pass_chat_id)[4:]}/{pass_msg_id}"
            if pass_msg_id else ""
        )
        # Резолв work_chat владельца (для перехода в чат)
        work_chat_id = 0
        try:
            owner = storage.get_crm_owner(drop.get("owner_id", "")) if drop.get("owner_id") else {}
            work_chat_id = (owner or {}).get("work_chat_id") or 0
        except Exception:
            pass
        # Сосед-банки этого drop'а уже с готовым статусом
        existing_done = []
        existing_in_work = []
        try:
            for other_lkid, other_lk in lks_raw.items():
                if other_lkid == lkid:
                    continue
                if other_lk.get("drop_id") != lk.get("drop_id"):
                    continue
                other_stage = (other_lk.get("sms_stage") or "").strip()
                other_bank = other_lk.get("bank") or "—"
                if other_stage == "done":
                    existing_done.append(other_bank)
                elif other_stage and other_stage != "done":
                    existing_in_work.append(other_bank)
        except Exception:
            pass
        out.append({
            "droplk_id": lkid,
            "drop_id": lk.get("drop_id"),
            "bank": lk.get("bank") or "",
            "fio": drop.get("fio") or "—",
            "existing_done_banks": existing_done,
            "existing_in_work_banks": existing_in_work,
            "social": drop.get("social") or "",
            "residence": drop.get("residence") or "",
            "owner_id": drop.get("owner_id") or "",
            "owner_username": drop.get("owner_username") or "",
            "owner_work_chat_id": work_chat_id,
            "supplier": supplier,
            "scan_count": len(drop.get("scan_file_ids") or []),
            "scan_file_ids": list(drop.get("scan_file_ids") or [])[:6],  # max 6 фото в API
            "value": lk.get("value") or "",
            "price": lk.get("price") or "",
            "new_login": lk.get("new_login") or "",
            "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "",
            "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "",
            "ded_password": lk.get("ded_pass") or lk.get("ded_password") or "",
            "ded_pass": lk.get("ded_pass") or lk.get("ded_password") or "",
            "ded_ip": lk.get("ded_ip") or "",
            "ded_location": lk.get("ded_location") or "",
            "sms_stage": stage,
            "_stage_order": stage_order.get(stage, 50),
            "completed": stage == "done",
            "tg_access_link": tg_access_link,
            "tg_pass_link": tg_pass_link,
            "sms_login_code": lk.get("sms_login_code") or "",
            "sms_perevyaz_code": lk.get("sms_perevyaz_code") or "",
            "value": lk.get("value") or "",
            "ded_login": lk.get("ded_login") or "",
            "created_at": lk.get("created_at") or 0,
            # Нумерация для UI
            "slot_number": _compute_lk_slot(drop, lkid)[0],
            "slot_total": _compute_lk_slot(drop, lkid)[1],
            "track": "supplier",
            "drop_number": drop.get("drop_id") or "",
        })
    # Фильтр по периоду (если задан)
    if period:
        import time as _t
        now = _t.time()
        thresholds = {"day": 86400, "week": 86400*7, "month": 86400*31}
        sec = thresholds.get(period)
        if sec:
            cutoff = now - sec
            out = [x for x in out if (x.get("created_at") or 0) >= cutoff]
    # Сортировка: по дате создания desc (самые свежие сверху).
    out.sort(key=lambda x: -(x.get("created_at") or 0))
    return {"ok": True, "items": out, "lks": out, "count": len(out)}


@app.get("/api/system/lk/{droplk_id}/full")
async def api_system_lk_full(droplk_id: str, _: None = Depends(_auth)):
    """Полная инфа о ЛК для System control panel: drop + lk + owner + history."""
    storage.reload_sync()
    lk = storage.get_crm_drop_lk(droplk_id) if hasattr(storage, "get_crm_drop_lk") else None
    if not lk:
        raise HTTPException(404, "lk not found")
    drop = storage.get_crm_drop(lk.get("drop_id")) if hasattr(storage, "get_crm_drop") else None
    owner = storage.get_crm_owner(drop.get("owner_id") if drop else "") if hasattr(storage, "get_crm_owner") else None
    return {
        "ok": True,
        "lk": lk,
        "drop": drop or {},
        "owner": owner or {},
    }


@app.post("/api/system/lk/{droplk_id}/fill")
async def api_system_lk_fill(droplk_id: str, request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("lk_fill"))):
    """Заполнение credentials/дедика для ЛК. Принимает любые поля:
    new_login, new_password, new_mail, new_number, code_word,
    ded_login, ded_password, ded_ip, ded_location, sms_code (вручную).
    Сохраняет в CRM и эмитит system-lk-updated SSE."""
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "json object required")
    allowed = {
        "new_login", "new_password", "new_mail", "new_number", "code_word",
        "ded_login", "ded_password", "ded_pass", "ded_ip", "ded_location",
        "sms_login_code", "sms_perevyaz_code",
        "value", "price",
        "notes",
    }
    fields = {k: v for k, v in data.items() if k in allowed and v is not None}
    # КРИТИЧНО: в storage поле называется 'ded_pass' (не 'ded_password').
    # Если дашборд прислал 'ded_password' — мапим в 'ded_pass'.
    if "ded_password" in fields:
        fields["ded_pass"] = fields.pop("ded_password")
    if not fields:
        raise HTTPException(400, "no valid fields")
    storage.reload_sync()
    lk = storage.get_crm_drop_lk(droplk_id) if hasattr(storage, "get_crm_drop_lk") else None
    if not lk:
        raise HTTPException(404, "lk not found")
    if hasattr(storage, "update_crm_drop_lk"):
        await storage.update_crm_drop_lk(droplk_id, **fields)
    # Шлём CRM-боту команды обновить ОБЕ TG-группы: ДОСТУПЫ (sms-tracker)
    # и ПАРОЛИ (password-post). Чтобы при заполнении из дашборда оба
    # сообщения в TG обновились синхронно.
    try:
        await storage.enqueue_dashboard_command(
            f"__sms_refresh_tracker {droplk_id}",
            source="dashboard-system-fill",
        )
    except Exception:
        pass
    try:
        await storage.enqueue_dashboard_command(
            f"__refresh_password_post {droplk_id}",
            source="dashboard-system-fill",
        )
    except Exception:
        pass
    try:
        event_bus.emit_event("system-lk-updated", {
            "droplk_id": droplk_id,
            "fields": list(fields.keys()),
        })
    except Exception:
        pass
    return {"ok": True, "droplk_id": droplk_id, "updated": list(fields.keys())}


@app.get("/api/system/lk/{droplk_id}/photo/{idx}")
async def api_system_lk_photo(droplk_id: str, idx: int, _: None = Depends(_auth)):
    """Прокси для фото документов из Telegram. Скачивает через CRM_BOT_TOKEN."""
    from fastapi.responses import Response, StreamingResponse
    import httpx
    storage.reload_sync()
    lk = storage.get_crm_drop_lk(droplk_id) if hasattr(storage, "get_crm_drop_lk") else None
    if not lk:
        raise HTTPException(404, "lk not found")
    drop = storage.get_crm_drop(lk.get("drop_id")) if hasattr(storage, "get_crm_drop") else None
    if not drop:
        raise HTTPException(404, "drop not found")
    file_ids = drop.get("scan_file_ids") or []
    if idx >= len(file_ids):
        raise HTTPException(404, "photo idx out of range")
    file_id = file_ids[idx]
    tok = os.getenv("CRM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
    if not tok:
        raise HTTPException(500, "bot token not set")
    try:
        async with httpx.AsyncClient(timeout=15.0) as cli:
            r1 = await cli.get(
                f"https://api.telegram.org/bot{tok}/getFile",
                params={"file_id": file_id},
            )
            if r1.status_code != 200:
                raise HTTPException(404, "tg getFile failed")
            j = r1.json()
            if not j.get("ok"):
                raise HTTPException(404, "tg file not found")
            file_path = j["result"]["file_path"]
            r2 = await cli.get(
                f"https://api.telegram.org/file/bot{tok}/{file_path}"
            )
            if r2.status_code != 200:
                raise HTTPException(404, "tg file download failed")
            # Content-Type
            ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpg"
            ct_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
            ct = ct_map.get(ext, "application/octet-stream")
            return Response(
                content=r2.content, media_type=ct,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"photo fetch error: {e}")


@app.get("/api/system/stats")
async def api_system_stats(_: None = Depends(_auth)):
    """Статистика перевязов System: сегодня/неделя/месяц + по банкам."""
    import time as _t
    storage.reload_sync()
    lks_raw = (storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}) or {}
    now = _t.time()
    day_cut = now - 86400
    week_cut = now - 86400 * 7
    month_cut = now - 86400 * 31

    def init_bucket():
        return {"total": 0, "by_bank": {}}

    daily_done = init_bucket()
    weekly_done = init_bucket()
    monthly_done = init_bucket()
    active_count = 0
    by_stage = {}

    for lkid, lk in lks_raw.items():
        stage = lk.get("sms_stage") or ""
        bank = lk.get("bank") or "—"
        created = lk.get("created_at") or 0
        updated = lk.get("updated_at") or created or 0
        # Активные (не done)
        if stage != "done":
            active_count += 1
            by_stage[stage or "empty"] = by_stage.get(stage or "empty", 0) + 1
        # Завершённые — по дате updated
        if stage == "done":
            for cut, b in ((day_cut, daily_done), (week_cut, weekly_done), (month_cut, monthly_done)):
                if updated >= cut:
                    b["total"] += 1
                    b["by_bank"][bank] = b["by_bank"].get(bank, 0) + 1
    return {
        "ok": True,
        "active": {"total": active_count, "by_stage": by_stage},
        "done": {
            "today": daily_done,
            "week": weekly_done,
            "month": monthly_done,
        },
    }


@app.get("/api/system/installed_lks")
async def api_system_installed_lks(
    period: Optional[str] = None,
    _: None = Depends(_auth),
):
    """УСПЕШНО УСТАНОВЛЕННЫЕ ЛК: stage=done + заполнены credentials + дедик.
    Карточка идёт сюда когда оба действия выполнены."""
    import time as _t
    storage.reload_sync()
    lks_raw = (storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}) or {}
    drops_raw = (storage.list_crm_drops() if hasattr(storage, "list_crm_drops") else {}) or {}
    out = []
    for lkid, lk in lks_raw.items():
        stage = (lk.get("sms_stage") or "").strip()
        # Установленные = перевязка завершена/фактически готова.
        # done — финальный статус (после нажатия кнопки «Финал»)
        # perevyaz_received — перевязка принята, ждёт финальной кнопки —
        # фактически ЛК уже работает, его и показываем в Установленных.
        if stage not in ("done", "perevyaz_received"):
            continue
        # Заполнены credentials (хотя бы один из ключевых полей).
        # ВАЖНО: дедик БОЛЬШЕ НЕ ТРЕБУЕТСЯ — многие ЛК работают без RDP,
        # они тоже должны попадать в «Установленные» сразу после перевязки.
        # Раньше требование `creds_ok AND dedik_ok` блокировало ЛК без дедика —
        # они зависали в «Паролях» и не появлялись в «Установленных».
        creds_ok = bool(
            (lk.get("new_password") or "").strip()
            or (lk.get("new_login") or "").strip()
            or (lk.get("new_mail") or "").strip()
            or (lk.get("new_number") or "").strip()
        )
        if not creds_ok:
            continue
        # Дедик опционален — просто пометим флаг has_dedik для UI
        has_dedik = bool(
            (lk.get("ded_ip") or "").strip()
            or (lk.get("ded_pass") or "").strip()
            or (lk.get("ded_password") or "").strip()
        )
        drop = drops_raw.get(lk.get("drop_id"), {}) or {}
        supplier = (drop.get("supplier") or "").lstrip("@")
        if not supplier and drop.get("owner_id"):
            try:
                owner = storage.get_crm_owner(drop["owner_id"]) or {}
                supplier = (owner.get("username") or "").lstrip("@")
            except Exception:
                pass
        out.append({
            "droplk_id": lkid,
            "drop_id": lk.get("drop_id"),
            "bank": lk.get("bank") or "",
            "fio": drop.get("fio") or "—",
            "supplier": supplier,
            "new_login": lk.get("new_login") or "",
            "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "",
            "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "Administrator",
            "ded_password": lk.get("ded_pass") or lk.get("ded_password") or "",
            "ded_pass": lk.get("ded_pass") or lk.get("ded_password") or "",
            "ded_ip": lk.get("ded_ip") or "",
            "ded_location": lk.get("ded_location") or "",
            "value": lk.get("value") or "",
            "has_dedik": has_dedik,  # для UI индикатора (ЛК с/без RDP)
            "installed_at": lk.get("updated_at") or lk.get("created_at") or 0,
            "created_at": lk.get("created_at") or 0,
        })
    if period:
        now = _t.time()
        thresholds = {"day": 86400, "week": 86400*7, "month": 86400*31}
        sec = thresholds.get(period)
        if sec:
            cutoff = now - sec
            out = [x for x in out if (x.get("installed_at") or 0) >= cutoff]
    out.sort(key=lambda x: -(x.get("installed_at") or 0))
    return {"ok": True, "items": out, "count": len(out)}


@app.get("/api/system/passwords_inbox")
async def api_system_passwords_inbox(_: None = Depends(_auth)):
    """Inbox для CRM | Password — все ЛК которые в работе (perevyaz_received,
    done или активны) и требуют заполнения credentials/дедика.
    Возвращает карточки как в TG-группе ПАРОЛИ."""
    storage.reload_sync()
    lks_raw = (
        storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}
    ) or {}
    drops_raw = (
        storage.list_crm_drops() if hasattr(storage, "list_crm_drops") else {}
    ) or {}
    out = []
    for lkid, lk in lks_raw.items():
        stage = (lk.get("sms_stage") or "").strip()
        # Карточка для ПАРОЛЕЙ создаётся только когда ЛК уже в активной работе
        # (perevyaz_received или done) — то есть готов к заполнению или уже заполнен.
        # Можно показывать и ранее — но фильтруем для соответствия TG.
        if stage not in ("perevyaz_received", "done", "login_received",
                          "perevyaz_asked"):
            continue
        drop = drops_raw.get(lk.get("drop_id"), {}) if drops_raw else {}
        supplier = (drop.get("supplier") or "").lstrip("@")
        if not supplier and drop.get("owner_id"):
            try:
                owner = storage.get_crm_owner(drop["owner_id"]) or {}
                supplier = (owner.get("username") or "").lstrip("@")
            except Exception:
                pass
        # Считаем заполнено ли всё
        filled_creds = bool(
            (lk.get("new_login") or "").strip() and
            (lk.get("new_password") or "").strip()
        )
        filled_dedik = bool(
            (lk.get("ded_ip") or "").strip() and
            ((lk.get("ded_pass") or "").strip() or (lk.get("ded_password") or "").strip())
        )
        pass_chat_id = -1003788743917
        pass_msg_id = lk.get("msgid_pass") or 0
        tg_pass_link = (
            f"https://t.me/c/{str(pass_chat_id)[4:]}/{pass_msg_id}"
            if pass_msg_id else ""
        )
        # Сосед-банки этого drop'а уже с готовым статусом
        existing_done = []
        existing_in_work = []
        try:
            for other_lkid, other_lk in lks_raw.items():
                if other_lkid == lkid:
                    continue
                if other_lk.get("drop_id") != lk.get("drop_id"):
                    continue
                other_stage = (other_lk.get("sms_stage") or "").strip()
                other_bank = other_lk.get("bank") or "—"
                if other_stage == "done":
                    existing_done.append(other_bank)
                elif other_stage and other_stage != "done":
                    existing_in_work.append(other_bank)
        except Exception:
            pass
        out.append({
            "droplk_id": lkid,
            "drop_id": lk.get("drop_id"),
            "bank": lk.get("bank") or "",
            "fio": drop.get("fio") or "—",
            "existing_done_banks": existing_done,
            "existing_in_work_banks": existing_in_work,
            "supplier": supplier,
            "new_login": lk.get("new_login") or "",
            "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "",
            "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "Administrator",
            "ded_password": lk.get("ded_pass") or lk.get("ded_password") or "",
            "ded_pass": lk.get("ded_pass") or lk.get("ded_password") or "",
            "ded_ip": lk.get("ded_ip") or "",
            "ded_location": lk.get("ded_location") or "",
            "sms_stage": stage,
            "filled_creds": filled_creds,
            "filled_dedik": filled_dedik,
            "filled": filled_creds and filled_dedik,
            "tg_pass_link": tg_pass_link,
            "updated_at": lk.get("updated_at") or 0,
            "created_at": lk.get("created_at") or 0,
            # Нумерация для UI
            "slot_number": _compute_lk_slot(drop, lkid)[0],
            "slot_total": _compute_lk_slot(drop, lkid)[1],
            "track": "supplier",
            "drop_number": drop.get("drop_id") or "",
        })
    # Заполненные в конец, заполняемые первыми
    out.sort(key=lambda x: (x.get("filled"), -(x.get("created_at") or 0)))
    return {"ok": True, "items": out, "count": len(out)}


# =====================================================================
# CREDIT (Кредитование) — параллельные эндпоинты к CRM-поставщикам
# =====================================================================

@app.get("/api/system/credit_pending_lk")
async def api_system_credit_pending_lk(_: None = Depends(_auth)):
    """Список ЛК кредитования в активном SMS-флоу.
    Источник — credit_drop_lks (юристы заполняют через CRM-бота)."""
    storage.reload_sync()
    lks_raw = (
        storage.list_credit_drop_lks() if hasattr(storage, "list_credit_drop_lks") else {}
    ) or {}
    drops_raw = (
        storage.list_credit_drops() if hasattr(storage, "list_credit_drops") else {}
    ) or {}
    out = []
    stage_order = {
        "": 0, "ready_asked": 1, "ready_confirmed": 2,
        "login_asked": 3, "login_received": 4,
        "perevyaz_asked": 5, "perevyaz_received": 6, "done": 99,
    }
    for lkid, lk in lks_raw.items():
        stage = (lk.get("sms_stage") or "").strip()
        # done ЛК остаются с флагом completed=True (см. CRM pending выше)
        drop = drops_raw.get(lk.get("credit_drop_id"), {}) if drops_raw else {}
        manager = (lk.get("manager_username") or drop.get("manager_username") or "").lstrip("@")
        out.append({
            "droplk_id": lkid,
            "credit_drop_id": lk.get("credit_drop_id"),
            "bank": lk.get("bank") or "",
            "fio": drop.get("fio") or "—",
            "manager": manager,
            "scan_count": len(drop.get("scan_file_ids") or []),
            "value": lk.get("value") or "",
            "new_login": lk.get("new_login") or "",
            "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "",
            "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "",
            "ded_password": lk.get("ded_pass") or "",
            "ded_pass": lk.get("ded_pass") or "",
            "ded_ip": lk.get("ded_ip") or "",
            "ded_location": lk.get("ded_location") or "",
            "sms_stage": stage,
            "_stage_order": stage_order.get(stage, 50),
            "completed": stage == "done",
            "sms_login_code": lk.get("sms_login_code") or "",
            "sms_perevyaz_code": lk.get("sms_perevyaz_code") or "",
            "created_at": lk.get("created_at") or 0,
            "updated_at": lk.get("updated_at") or 0,
            # Нумерация для UI
            "slot_number": _compute_lk_slot(drop, lkid)[0],
            "slot_total": _compute_lk_slot(drop, lkid)[1],
            "track": "credit",
            "drop_number": drop.get("drop_id") or "",
        })
    # Свежие сверху (новые сообщения = новые msg_id ~ created_at)
    out.sort(key=lambda x: -(x.get("created_at") or 0))
    return {"ok": True, "items": out, "lks": out, "count": len(out)}


@app.get("/api/system/credit_passwords_inbox")
async def api_system_credit_passwords_inbox(_: None = Depends(_auth)):
    """Inbox для КРЕДИТ | Пароли — все ЛК кредитования с заполнением credentials/дедика."""
    storage.reload_sync()
    lks_raw = (
        storage.list_credit_drop_lks() if hasattr(storage, "list_credit_drop_lks") else {}
    ) or {}
    drops_raw = (
        storage.list_credit_drops() if hasattr(storage, "list_credit_drops") else {}
    ) or {}
    out = []
    for lkid, lk in lks_raw.items():
        stage = (lk.get("sms_stage") or "").strip()
        if stage not in ("perevyaz_received", "done", "login_received", "perevyaz_asked"):
            continue
        drop = drops_raw.get(lk.get("credit_drop_id"), {}) if drops_raw else {}
        manager = (lk.get("manager_username") or drop.get("manager_username") or "").lstrip("@")
        filled_creds = bool(
            (lk.get("new_login") or "").strip() and (lk.get("new_password") or "").strip()
        )
        filled_dedik = bool(
            (lk.get("ded_ip") or "").strip() and (lk.get("ded_pass") or "").strip()
        )
        out.append({
            "droplk_id": lkid,
            "credit_drop_id": lk.get("credit_drop_id"),
            "bank": lk.get("bank") or "",
            "fio": drop.get("fio") or "—",
            "manager": manager,
            "new_login": lk.get("new_login") or "",
            "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "",
            "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "Administrator",
            "ded_password": lk.get("ded_pass") or "",
            "ded_pass": lk.get("ded_pass") or "",
            "ded_ip": lk.get("ded_ip") or "",
            "ded_location": lk.get("ded_location") or "",
            "sms_stage": stage,
            "filled_creds": filled_creds,
            "filled_dedik": filled_dedik,
            "filled": filled_creds and filled_dedik,
            "updated_at": lk.get("updated_at") or 0,
            "created_at": lk.get("created_at") or 0,
            # Нумерация для UI
            "slot_number": _compute_lk_slot(drop, lkid)[0],
            "slot_total": _compute_lk_slot(drop, lkid)[1],
            "track": "credit",
            "drop_number": drop.get("drop_id") or "",
        })
    out.sort(key=lambda x: -(x.get("created_at") or 0))
    return {"ok": True, "items": out, "count": len(out)}


@app.get("/api/system/credit_managers")
async def api_system_credit_managers(_: None = Depends(_auth)):
    """Список менеджеров кредитования со статистикой."""
    storage.reload_sync()
    mgrs = (
        storage.list_credit_managers() if hasattr(storage, "list_credit_managers") else {}
    ) or {}
    chats = (
        storage.list_credit_chats() if hasattr(storage, "list_credit_chats") else {}
    ) or {}
    # Считаем сколько чатов у каждого менеджера
    chats_per_manager = {}
    for chat_entry in chats.values():
        u = chat_entry.get("manager_username") or ""
        if not u:
            continue
        chats_per_manager[u] = chats_per_manager.get(u, 0) + 1
    out = []
    for u, m in mgrs.items():
        out.append({
            "username": u,
            "tg_user_id": m.get("tg_user_id") or 0,
            "first_seen_ts": m.get("first_seen_ts") or 0,
            "last_active_ts": m.get("last_active_ts") or 0,
            "stats": m.get("stats") or {},
            "chats_count": chats_per_manager.get(u, 0),
        })
    out.sort(key=lambda x: -(x.get("last_active_ts") or 0))
    return {"ok": True, "items": out, "count": len(out)}


# =====================================================================
# KUC (Кружок Удостоверения Клиента — KYC через одноразовую ссылку)
# =====================================================================
import os as _os_kuc
from pathlib import Path as _Path_kuc

_KUC_VIDEO_DIR = _Path_kuc(_os_kuc.environ.get("KUC_VIDEO_DIR", "/app/data/kuc"))
_KUC_MAX_BYTES = int(_os_kuc.environ.get("KUC_MAX_BYTES", 20 * 1024 * 1024))  # 20 MB


def _kuc_base_url(request: Request) -> str:
    """Возвращает базовый URL для ссылок (https://workchat-bot-production.up.railway.app)."""
    return _os_kuc.environ.get("KUC_BASE_URL") or str(request.base_url).rstrip("/")


@app.post("/api/system/lk/{droplk_id}/kuc/request")
async def api_system_lk_kuc_request(droplk_id: str, request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("request_kuc"))):
    """Работник создаёт KUC-запрос для ЛК. Body: {message: str}.
    Возвращает токен + ссылку. Userbot затем отправит её клиенту в work_chat."""
    if me.get("role") not in ("owner", "manager", "system"):
        raise HTTPException(403, "forbidden")
    body = {}
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        pass
    request_text = (body.get("message") or "").strip() or (
        "Здравствуйте! Для подтверждения принадлежности счёта, пожалуйста, "
        "запишите короткий видео-кружок: чётко скажите ваше ФИО и фразу "
        "«счёт в [банк] принадлежит мне»."
    )
    # Проверим что ЛК существует
    lk = storage.get_drop_lk_any(droplk_id)
    if not lk:
        raise HTTPException(404, f"ЛК {droplk_id} не найден")
    work_chat_id = storage.get_work_chat_for_droplk(droplk_id) if hasattr(storage, "get_work_chat_for_droplk") else None
    if not work_chat_id:
        raise HTTPException(400, "Не найден work_chat_id для этого ЛК — невозможно отправить ссылку клиенту")
    # Создаём токен
    token = await storage.create_kuc_request(
        droplk_id=droplk_id, work_chat_id=work_chat_id,
        requested_by=me.get("username") or "", request_text=request_text,
    )
    url = f"{_kuc_base_url(request)}/kuc/{token}"
    # Ставим команду userbot'у отправить ссылку в work_chat
    try:
        await storage.enqueue_dashboard_command(
            f"__send_kuc_link {work_chat_id} {token}",
            source=f"dashboard-kuc-request-by-{me.get('username') or 'unknown'}",
        )
    except Exception as e:
        logger.warning("KUC enqueue cmd failed: %s", e)
    try:
        event_bus.emit_event("kuc-requested", {
            "droplk_id": droplk_id, "token": token,
            "requested_by": me.get("username") or "",
        }, severity="info")
    except Exception:
        pass
    return {"ok": True, "token": token, "url": url, "work_chat_id": work_chat_id}


@app.get("/kuc/{token}")
async def kuc_capture_page(token: str):
    """Отдаёт kuc_capture.html для клиента (без auth — публичный доступ по токену)."""
    kuc = storage.get_kuc_request(token)
    if not kuc:
        return HTMLResponse("<h1>Ссылка недействительна</h1>", status_code=404)
    path = _Path_kuc(__file__).parent / "dashboard" / "kuc_capture.html"
    if not path.exists():
        return HTMLResponse("<h1>Страница не найдена</h1>", status_code=500)
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/kuc/{token}/info")
async def kuc_info(token: str):
    """Клиентский endpoint — возвращает request_text и status. Без auth."""
    kuc = storage.get_kuc_request(token)
    if not kuc:
        raise HTTPException(404, "Токен не найден")
    return {
        "token": token,
        "request_text": kuc.get("request_text") or "",
        "status": kuc.get("status") or "pending",
        "created_at": kuc.get("created_at") or 0,
    }


@app.post("/kuc/{token}/open")
async def kuc_mark_opened(token: str):
    """Клиент открыл страницу — отмечаем used_at. Без auth."""
    if not storage.get_kuc_request(token):
        raise HTTPException(404, "not found")
    await storage.mark_kuc_opened(token)
    return {"ok": True}


@app.post("/kuc/{token}/submit")
async def kuc_submit(token: str, video: UploadFile = File(...)):
    """Клиент загружает видео. Без auth (доверяем token).
    Видео сохраняется в _KUC_VIDEO_DIR, статус → submitted."""
    kuc = storage.get_kuc_request(token)
    if not kuc:
        raise HTTPException(404, "Токен не найден")
    if kuc.get("status") not in ("pending",):
        raise HTTPException(400, f"Видео уже отправлено (статус: {kuc.get('status')})")
    # Создаём папку
    _KUC_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".webm"
    if video.content_type and "mp4" in video.content_type:
        ext = ".mp4"
    file_path = _KUC_VIDEO_DIR / f"{token}{ext}"
    total = 0
    try:
        with open(file_path, "wb") as f:
            while True:
                chunk = await video.read(1024 * 64)
                if not chunk:
                    break
                total += len(chunk)
                if total > _KUC_MAX_BYTES:
                    f.close()
                    try: file_path.unlink()
                    except Exception: pass
                    raise HTTPException(413, f"Видео слишком большое (>{_KUC_MAX_BYTES // 1024 // 1024} MB)")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("KUC video save failed: %s", e)
        raise HTTPException(500, "Ошибка сохранения видео")
    await storage.mark_kuc_submitted(
        token=token, video_file_path=str(file_path),
        video_size_bytes=total, video_mime=video.content_type or "video/webm",
    )
    # Notify работника через SSE
    try:
        event_bus.emit_event("kuc-submitted", {
            "droplk_id": kuc.get("droplk_id"), "token": token,
            "size_bytes": total,
        }, severity="success")
    except Exception:
        pass
    return {"ok": True, "token": token, "size_bytes": total}


@app.get("/api/system/kuc/{token}/video")
async def kuc_get_video(token: str, _: None = Depends(_auth)):
    """Работник смотрит видео. Auth обязателен."""
    kuc = storage.get_kuc_request(token)
    if not kuc:
        raise HTTPException(404, "not found")
    path = kuc.get("video_file_path") or ""
    if not path or not _Path_kuc(path).exists():
        raise HTTPException(404, "video file missing on disk")

    def _iter():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
    mime = kuc.get("video_mime") or "video/webm"
    return StreamingResponse(_iter(), media_type=mime)


@app.post("/api/system/kuc/{token}/decide")
async def kuc_decide(token: str, request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("decide_kuc"))):
    """Работник одобряет/отклоняет КУЦ. Body: {decision: 'approved'|'rejected', note?: str}."""
    if me.get("role") not in ("owner", "manager", "system"):
        raise HTTPException(403, "forbidden")
    body = {}
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        pass
    decision = (body.get("decision") or "").strip().lower()
    note = (body.get("note") or "").strip()
    if decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")
    ok = await storage.decide_kuc(
        token=token, decision=decision,
        decision_by=me.get("username") or "", decision_note=note,
    )
    if not ok:
        raise HTTPException(404, "kuc not found")
    # Notify клиента в work_chat: бот пишет о результате проверки
    try:
        kuc_obj = storage.get_kuc_request(token) or {}
        wc = int(kuc_obj.get("work_chat_id") or 0)
        if wc:
            await storage.enqueue_dashboard_command(
                f"__notify_client_kuc_result {wc} {token} {decision}",
                source=f"dashboard-kuc-decide-by-{me.get('username') or 'unknown'}",
            )
    except Exception as e:
        logger.warning("KUC notify enqueue failed: %s", e)
    try:
        event_bus.emit_event("kuc-decided", {
            "token": token, "decision": decision,
            "decision_by": me.get("username") or "",
        }, severity="info")
    except Exception:
        pass
    return {"ok": True, "token": token, "decision": decision}


@app.get("/api/system/kuc/by_droplk/{droplk_id}")
async def kuc_history_by_droplk(droplk_id: str, _: None = Depends(_auth)):
    """История ВСЕХ КУЦ-запросов для этого ЛК — для отображения в JARVIS."""
    if not hasattr(storage, "list_kuc_for_droplk"):
        return {"ok": True, "items": [], "count": 0}
    items = storage.list_kuc_for_droplk(droplk_id) or []
    return {"ok": True, "items": items, "count": len(items)}


@app.get("/api/system/kuc/list")
async def kuc_list(_: None = Depends(_auth)):
    """Возвращает все KUC-запросы (для JARVIS отображения статуса в карточках ЛК)."""
    kucs = storage.list_kuc_requests() if hasattr(storage, "list_kuc_requests") else {}
    # Возвращаем dict {droplk_id: kuc} — последний активный для каждого ЛК
    by_droplk = {}
    for k in kucs.values():
        did = k.get("droplk_id")
        if not did:
            continue
        existing = by_droplk.get(did)
        # active > decided ; новее > старее
        if not existing:
            by_droplk[did] = k
            continue
        a_active = (existing.get("status") in ("pending", "submitted"))
        b_active = (k.get("status") in ("pending", "submitted"))
        if b_active and not a_active:
            by_droplk[did] = k
        elif (b_active == a_active) and (k.get("created_at") or 0) > (existing.get("created_at") or 0):
            by_droplk[did] = k
    return {"ok": True, "by_droplk": by_droplk, "count": len(by_droplk)}


# =====================================================================
# SETTINGS UI — управление настройками из JARVIS (без бота)
# =====================================================================
def _require_owner_or_manager(me: dict):
    role = (me.get("role") or "").lower()
    if role not in ("owner", "manager"):
        raise HTTPException(403, "owner or manager only")


@app.get("/api/settings/all")
async def settings_get_all(me: dict = Depends(_get_me)):
    """Возвращает все настройки одним вызовом — для дашборда."""
    _require_owner_or_manager(me)
    storage.reload_sync()
    return {
        "ok": True,
        # Прайс
        "pricing": storage.list_pricing() or {},
        # AI
        "ai_enabled": bool(storage.state.get("ai_enabled")),
        "ai_model": storage.get_ai_model() or "",
        "ai_max_tokens": int(storage.state.get("ai_max_tokens") or 512),
        "ai_history_limit": int(storage.state.get("ai_history_limit") or 15),
        "ai_typing_delay_min": float(storage.state.get("ai_typing_delay_min") or 3),
        "ai_typing_delay_max": float(storage.state.get("ai_typing_delay_max") or 8),
        "client_idle_minutes": int(storage.state.get("client_idle_minutes") or 5),
        # TG-чаты
        "brain_chat_id": int(storage.state.get("brain_chat_id") or 0),
        "lk_group_id": int(storage.state.get("lk_group_id") or 0),
        "coordination_chat_id": int(storage.state.get("coordination_chat_id") or 0),
        "ideas_chat_id": int(storage.state.get("ideas_chat_id") or 0) if hasattr(storage, "get_ideas_chat_id") else 0,
        "accounting_group_id": int(storage.state.get("accounting_group_id") or 0),
        # Invite-бот
        "welcome_message": storage.get_welcome() or "",
        "invite_welcome_gif_id": storage.get_invite_welcome_gif() if hasattr(storage, "get_invite_welcome_gif") else "",
        "invite_jobs_text": storage.get_invite_jobs_text() if hasattr(storage, "get_invite_jobs_text") else "",
        "invite_premium_emoji": storage.get_invite_premium_emoji() if hasattr(storage, "get_invite_premium_emoji") else {},
        # Default triggers + workers (для базовых настроек)
        "trigger_phrases": list(storage.state.get("trigger_phrases") or []),
        "cooldown_minutes": int(storage.state.get("cooldown_minutes") or 60),
        # Outsource payment (USDT TRC20)
        "outsource_corp_wallet_trc20": storage.get_outsource_corp_wallet() if hasattr(storage, "get_outsource_corp_wallet") else "",
    }


# --- Outsource Payment (USDT TRC20) ---
@app.post("/api/settings/outsource_payment/set_wallet")
async def settings_set_outsource_wallet(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("settings_payment_wallet"))):
    """Body: {address: str}. TRC20 адрес (начинается с T, 34 символа)."""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    address = (body.get("address") or "").strip()
    if not hasattr(storage, "set_outsource_corp_wallet"):
        raise HTTPException(500, "set_outsource_corp_wallet not available")
    ok = await storage.set_outsource_corp_wallet(address)
    if not ok:
        raise HTTPException(400, "Неверный TRC20 адрес (должен начинаться с T и быть 34 символа)")
    return {"ok": True, "address": address}


@app.get("/api/system/outsource/topups")
async def api_outsource_topups_list(me: dict = Depends(_get_me)):
    """Список всех top-up requests (pending + history)."""
    _require_owner_or_manager(me)
    reqs = storage.list_outsource_topup_requests() if hasattr(storage, "list_outsource_topup_requests") else {}
    items = []
    for rid, r in reqs.items():
        items.append({
            "id": rid,
            "username": r.get("username") or "",
            "base_amount": float(r.get("base_amount") or 0),
            "unique_amount": float(r.get("unique_amount") or 0),
            "status": r.get("status") or "",
            "txid": r.get("txid") or "",
            "created_at": r.get("created_at") or 0,
            "expires_at": r.get("expires_at") or 0,
            "credited_at": r.get("credited_at") or 0,
        })
    items.sort(key=lambda x: -(x["created_at"] or 0))
    return {"items": items}


@app.post("/api/system/outsource/topups/{request_id}/credit")
async def api_outsource_topup_manual_credit(request_id: str, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("topup_credit"))):
    """Ручное зачисление (admin override) — если auto-monitor не сработал."""
    _require_owner_or_manager(me)
    if not hasattr(storage, "credit_outsource_topup"):
        raise HTTPException(500, "credit_outsource_topup not available")
    result = await storage.credit_outsource_topup(
        request_id=request_id, txid="manual", manual_by=me.get("username") or "",
    )
    if not result:
        raise HTTPException(400, "Не удалось зачислить (возможно уже обработано)")
    try:
        await _log_event("outsource_topup_credited_manual", {
            "request_id": request_id, "by": me.get("username") or "",
        }, severity="info")
    except Exception: pass
    return {"ok": True}


@app.post("/api/system/outsource/topups/{request_id}/reject")
async def api_outsource_topup_reject(request_id: str, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("topup_reject"))):
    _require_owner_or_manager(me)
    if not hasattr(storage, "reject_outsource_topup"):
        raise HTTPException(500, "reject_outsource_topup not available")
    ok = await storage.reject_outsource_topup(request_id, manual_by=me.get("username") or "")
    if not ok:
        raise HTTPException(400, "Не удалось отклонить (возможно уже обработано)")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# OUTSOURCE BOT TEXTS — редактируемые тексты @marketplace_PRIDE_BOT
# ═══════════════════════════════════════════════════════════════
# Группировка ключей для UI (порядок и сворачивание)
_OUTSOURCE_TEXT_GROUPS = [
    {
        "id": "buttons", "label": "🎛 Кнопки главного меню",
        "keys": ["btn_catalog", "btn_balance", "btn_myorders", "btn_profile", "btn_terms"],
    },
    {
        "id": "start", "label": "🚀 /start и общие",
        "keys": ["start_welcome", "no_username", "help"],
    },
    {
        "id": "catalog", "label": "📋 Каталог",
        "keys": ["catalog_header", "catalog_singles_header", "catalog_singles_empty",
                 "catalog_bundles_header", "catalog_bundles_empty"],
    },
    {
        "id": "balance", "label": "💲 Баланс и пополнение",
        "keys": ["balance_header", "topup_no_wallet", "topup_ask_amount",
                 "topup_amount_too_small", "topup_amount_too_large",
                 "topup_amount_invalid", "topup_create_failed",
                 "topup_instructions", "topup_credited_notify",
                 "withdraw_message", "history_coming"],
    },
    {
        "id": "orders", "label": "🧾 Мои заказы и профиль",
        "keys": ["myorders_empty", "myorders_header", "profile_text"],
    },
    {
        "id": "buy", "label": "💼 Покупка ЛК и связки",
        "keys": ["buy_not_found", "buy_taken", "buy_no_funds_alert",
                 "buy_success_alert", "buy_success_message",
                 "bundle_not_found", "bundle_taken", "bundle_view_text",
                 "bundle_buy_success_alert", "bundle_buy_success_message"],
    },
    {
        "id": "terms", "label": "📋 Условия (показываются перед покупкой/оплатой)",
        "keys": ["terms_menu", "terms_purchase", "terms_payment",
                 "terms_agree_btn", "terms_decline_btn", "terms_declined"],
    },
]


@app.get("/api/system/outsource/bot_texts")
async def api_outsource_bot_texts(me: dict = Depends(_get_me)):
    """Возвращает все ключи бота с дефолтами + текущими значениями + группировкой."""
    _require_owner_or_manager(me)
    if not hasattr(storage, "list_outsource_texts"):
        return {"groups": [], "items": {}}
    items = storage.list_outsource_texts() or {}
    # Описания переменных доступных в каждом ключе (для UI hint)
    placeholders = {
        "start_welcome": ["name"],
        "catalog_header": ["singles_cnt", "bundles_cnt"],
        "catalog_singles_header": ["count"],
        "catalog_bundles_header": ["count"],
        "balance_header": ["balance", "paid"],
        "topup_instructions": ["id", "unique", "wallet", "base", "expires_min"],
        "topup_credited_notify": ["base", "new_bal", "txid"],
        "myorders_header": ["count"],
        "profile_text": ["username", "tg_id", "days", "balance", "paid",
                         "drops_total", "lks_total", "lks_done"],
        "buy_no_funds_alert": ["balance", "price"],
        "buy_success_alert": ["price"],
        "buy_success_message": ["bank", "fio", "price", "new_balance"],
        "bundle_view_text": ["name", "count", "price"],
        "bundle_buy_success_alert": ["price"],
        "bundle_buy_success_message": ["name", "count", "price", "new_balance"],
    }
    return {
        "groups": _OUTSOURCE_TEXT_GROUPS,
        "items": items,
        "placeholders": placeholders,
    }


@app.post("/api/system/outsource/bot_texts/set")
async def api_outsource_bot_texts_set(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("bot_text_set"))):
    """Body: {key: str, value: str}. Пустой value = сбросить к дефолту."""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    key = (body.get("key") or "").strip()
    value = body.get("value") if body.get("value") is not None else ""
    if not key:
        raise HTTPException(400, "key required")
    if not hasattr(storage, "set_outsource_text"):
        raise HTTPException(500, "set_outsource_text not available")
    await storage.set_outsource_text(key, value)
    return {"ok": True}


@app.post("/api/system/outsource/bot_texts/reset")
async def api_outsource_bot_texts_reset(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("bot_text_reset"))):
    """Body: {key: str}. Удаляет override → возвращает дефолт."""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key required")
    if not hasattr(storage, "reset_outsource_text"):
        raise HTTPException(500, "reset_outsource_text not available")
    await storage.reset_outsource_text(key)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# ОТКУПЫ — обмен RUB → USDT TRC20 через ручных Откупщиков
# ═══════════════════════════════════════════════════════════════
@app.get("/api/outkup/orders")
async def api_outkup_orders(
    status: Optional[str] = None,
    me: dict = Depends(_get_me),
):
    """Список всех заявок Откупов с опциональным фильтром по статусу."""
    if me.get("role") not in ("owner", "manager") and not storage.role_can_view(me.get("role") or "", "outkup"):
        raise HTTPException(403, "forbidden")
    orders = storage.list_outkup_orders() if hasattr(storage, "list_outkup_orders") else {}
    items = list(orders.values())
    if status:
        items = [o for o in items if (o.get("status") or "") == status]
    items.sort(key=lambda x: -(x.get("created_at") or 0))
    return {"items": items, "count": len(items)}


@app.get("/api/settings/outkup")
async def api_outkup_settings_get(me: dict = Depends(_get_me)):
    if me.get("role") not in ("owner", "manager") and not storage.role_can_view(me.get("role") or "", "outkup"):
        raise HTTPException(403, "forbidden")
    return storage.get_outkup_settings() if hasattr(storage, "get_outkup_settings") else {}


@app.post("/api/settings/outkup")
async def api_outkup_settings_update(
    request: Request,
    me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("outkup_settings_update")),
):
    """Body: {rate_rub_per_usdt?, payments_chat_id?, outkup_team_chat_id?,
              min_amount_rub?, max_amount_rub?, enabled?}"""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    if not hasattr(storage, "update_outkup_settings"):
        raise HTTPException(500, "outkup not available")
    allowed = ("rate_rub_per_usdt", "payments_chat_id", "outkup_team_chat_id",
               "min_amount_rub", "max_amount_rub", "enabled")
    fields = {k: body[k] for k in allowed if k in body}
    s = await storage.update_outkup_settings(**fields)
    return {"ok": True, "settings": s}


@app.post("/api/outkup/orders/{order_id}/take")
async def api_outkup_take(
    order_id: str,
    me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("outkup_take")),
):
    o = await storage.take_outkup_order(order_id, username=me.get("username") or "")
    if not o:
        raise HTTPException(400, "Не удалось взять заявку (возможно уже закрыта)")
    return {"ok": True, "order": o}


@app.post("/api/outkup/orders/{order_id}/mark_paid")
async def api_outkup_mark_paid(
    order_id: str,
    me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("outkup_mark_paid")),
):
    o = await storage.mark_outkup_paid(order_id, by=me.get("username") or "")
    if not o:
        raise HTTPException(400, "Не удалось пометить (status mismatch)")
    return {"ok": True, "order": o}


@app.post("/api/outkup/orders/{order_id}/complete")
async def api_outkup_complete(
    order_id: str, request: Request,
    me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("outkup_complete")),
):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    txid = (body.get("txid") or "").strip()
    o = await storage.complete_outkup_order(order_id, txid=txid, by=me.get("username") or "")
    if not o:
        raise HTTPException(400, "Не удалось завершить")
    # === Auto-accounting: +kassa с пометкой outkup ===
    # Для откупа: клиент даёт нам RUB, мы платим ему USDT — у нас остаётся margin.
    # В кассу пишем USDT-эквивалент (rub_amount / rate).
    try:
        rub = float(o.get("amount_rub") or 0)
        rate = float(o.get("rate_rub_per_usdt") or 100)
        if rub > 0 and rate > 0:
            usdt_eq = rub / rate
            await storage.add_accounting_entry(
                category="kassa",
                amount_usdt=usdt_eq,
                amount_rub=rub,
                note=f"outkup #{order_id} (rate {rate} ₽/$, txid {txid[:16]}...)",
                created_by="auto:outkup_complete",
            )
            try:
                await storage.add_notification(
                    type="success",
                    text=f"💱 Откуп завершён: +{usdt_eq:.2f}$ ({rub} ₽)",
                    dedup_key=f"auto_outkup:{order_id}",
                )
            except Exception:
                pass
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("auto-acc outkup failed: %s", _e)
    return {"ok": True, "order": o}


@app.post("/api/outkup/orders/{order_id}/cancel")
async def api_outkup_cancel(
    order_id: str, request: Request,
    me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("outkup_cancel")),
):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    reason = (body.get("reason") or "").strip()
    o = await storage.cancel_outkup_order(order_id, reason=reason, by=me.get("username") or "")
    if not o:
        raise HTTPException(400, "Не удалось отменить")
    return {"ok": True, "order": o}


# --- Operational: типы отчётности (Owner only CRUD) ---
@app.get("/api/operational/report_types")
async def op_list_report_types(me: dict = Depends(_get_me)):
    return {"types": storage.list_operational_report_types()}


@app.post("/api/operational/report_types")
async def op_add_report_type(request: Request, me: dict = Depends(_get_me)):
    if me.get("role") != "owner":
        raise HTTPException(403, "owner only")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    ok = await storage.add_operational_report_type(name)
    return {"ok": ok, "types": storage.list_operational_report_types()}


@app.delete("/api/operational/report_types/{name}")
async def op_delete_report_type(name: str, me: dict = Depends(_get_me)):
    if me.get("role") != "owner":
        raise HTTPException(403, "owner only")
    ok = await storage.remove_operational_report_type(name)
    return {"ok": ok, "types": storage.list_operational_report_types()}


# --- CRM Balances ---
class WithdrawRequestPayload(BaseModel):
    user_key: str
    amount_usdt: float
    address: str
    method: Optional[str] = "USDT_TRC20"
    note: Optional[str] = ""


@app.get("/api/balance")
async def balance_list(me: dict = Depends(_get_me)):
    """Все балансы — для бухгалтерии. Owner/Manager/Accounting."""
    if me.get("role") not in ("owner", "manager", "accounting"):
        raise HTTPException(403, "forbidden")
    storage.reload_sync()
    raw = storage.list_balances()
    items = []
    for ukey, b in raw.items():
        items.append({
            "user_key": ukey,
            "kind": "owner" if ukey.startswith("owner:") else "worker",
            "id": ukey.split(":", 1)[1] if ":" in ukey else ukey,
            "pending_usdt": float(b.get("pending_usdt") or 0),
            "available_usdt": float(b.get("available_usdt") or 0),
            "total_earned": float(b.get("total_earned") or 0),
            "total_withdrawn": float(b.get("total_withdrawn") or 0),
            "usdt_address": b.get("usdt_address") or "",
            "last_payout_ts": float(b.get("last_payout_ts") or 0),
        })
    items.sort(key=lambda x: x["available_usdt"] + x["pending_usdt"], reverse=True)
    return {"balances": items, "total": len(items)}


@app.get("/api/balance/{user_key}")
async def balance_get(user_key: str, me: dict = Depends(_get_me)):
    storage.reload_sync()
    return {"balance": storage.get_balance(user_key)}


@app.get("/api/balance/{user_key}/tx")
async def balance_tx(user_key: str, limit: int = 50, me: dict = Depends(_get_me)):
    storage.reload_sync()
    return {"transactions": storage.list_balance_tx(user_key, limit=limit)}


@app.post("/api/balance/withdraw")
async def balance_withdraw(payload: WithdrawRequestPayload, me: dict = Depends(_get_me)):
    """Запрос на вывод. Может ставить юзер сам (из CRM-бота) либо owner от имени юзера.
    Если сумма ≤ auto_threshold — авто-вывод; иначе остаётся pending для апрува."""
    storage.reload_sync()
    s = storage.get_balance_settings()
    if payload.amount_usdt < s["min_payout_usdt"]:
        raise HTTPException(400, f"min payout = {s['min_payout_usdt']} USDT")
    b = storage.get_balance(payload.user_key)
    if b["available_usdt"] < payload.amount_usdt:
        raise HTTPException(400, "insufficient available balance")
    req_id = await storage.request_withdrawal(
        payload.user_key, payload.amount_usdt, payload.address,
        method=payload.method or "USDT_TRC20", note=payload.note or "",
    )
    if not req_id:
        raise HTTPException(500, "failed to create request")
    # Авто-выплата для мелких сумм
    auto_paid = False
    if payload.amount_usdt <= s["auto_threshold_usdt"]:
        try:
            from auto_payouts_runner import process_withdrawal
            ok, tx_hash = await process_withdrawal(req_id)
            if ok:
                auto_paid = True
        except Exception as e:
            logger.warning("auto withdraw failed for %s: %s", req_id, e)
    return {"req_id": req_id, "auto_paid": auto_paid}


@app.get("/api/balance/withdrawals/list")
async def balance_withdrawals_list(status: str = "", me: dict = Depends(_get_me)):
    if me.get("role") not in ("owner", "manager", "accounting"):
        raise HTTPException(403, "forbidden")
    storage.reload_sync()
    return {"requests": storage.list_withdrawals(status=status)}


@app.post("/api/balance/withdrawals/{req_id}/approve")
async def balance_withdrawal_approve(req_id: str, me: dict = Depends(_get_me)):
    if me.get("role") != "owner":
        raise HTTPException(403, "owner only")
    try:
        from auto_payouts_runner import process_withdrawal
        ok, tx_hash = await process_withdrawal(
            req_id, approved_by=me.get("username") or str(me.get("id") or ""),
        )
        return {"ok": ok, "tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"payout failed: {e}")


@app.post("/api/balance/withdrawals/{req_id}/cancel")
async def balance_withdrawal_cancel(req_id: str, me: dict = Depends(_get_me)):
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    ok = await storage.cancel_withdrawal(req_id, by=me.get("username") or "")
    return {"ok": ok}


@app.get("/api/balance/settings/get")
async def balance_settings_get(me: dict = Depends(_get_me)):
    return storage.get_balance_settings()


@app.post("/api/balance/settings/set")
async def balance_settings_set(request: Request, me: dict = Depends(_get_me)):
    if me.get("role") != "owner":
        raise HTTPException(403, "owner only")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    await storage.set_balance_settings(**body)
    return {"ok": True, "settings": storage.get_balance_settings()}


@app.post("/api/balance/{user_key}/address")
async def balance_set_address(user_key: str, request: Request, me: dict = Depends(_get_me)):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    await storage.set_balance_address(
        user_key, address=body.get("address") or "", method=body.get("method") or "",
    )
    return {"ok": True, "balance": storage.get_balance(user_key)}


# --- Hot wallet status (для индикатора в JARVIS) ---
@app.get("/api/tron/balance")
async def api_tron_balance(me: dict = Depends(_get_me)):
    if me.get("role") not in ("owner", "manager", "accounting"):
        raise HTTPException(403, "forbidden")
    import os as _os
    diag = {
        "env_TRON_MNEMONIC_set": bool((_os.environ.get("TRON_MNEMONIC") or "").strip()),
        "env_TRON_PRIVATE_KEY_set": bool((_os.environ.get("TRON_PRIVATE_KEY") or "").strip()),
        "env_TRON_HOT_WALLET_ADDRESS_set": bool((_os.environ.get("TRON_HOT_WALLET_ADDRESS") or "").strip()),
        "env_TRON_OWNER_TG_ID_set": bool((_os.environ.get("TRON_OWNER_TG_ID") or "").strip()),
        "env_GUARD_BOT_TOKEN_set": bool((_os.environ.get("GUARD_BOT_TOKEN") or "").strip()),
        "env_TRON_DERIVATION_PATH": _os.environ.get("TRON_DERIVATION_PATH", "") or "(default m/44'/195'/0'/0/0)",
    }
    # Проверка BIP39 зависимостей
    bip39_ok = False
    bip39_err = ""
    try:
        import mnemonic as _m  # noqa: F401
        import bip_utils as _b  # noqa: F401
        bip39_ok = True
    except Exception as e:
        bip39_err = str(e)
    diag["bip39_libs_installed"] = bip39_ok
    diag["bip39_libs_error"] = bip39_err

    try:
        from tron_payouts import is_configured, get_hot_wallet_address, get_hot_wallet_balance
    except Exception as e:
        return {**diag, "error": f"tron_payouts import failed: {e}", "configured": False}

    # Принудительно пробуем derive если есть мнемоника но нет privkey
    try:
        from tron_payouts import _ensure_derived, _DERIVED_CACHE
        _ensure_derived()
        diag["derived_cache_has_priv"] = bool(_DERIVED_CACHE.get("priv"))
        diag["derived_cache_address"] = _DERIVED_CACHE.get("address") or ""
    except Exception as e:
        diag["derive_attempt_error"] = str(e)

    if not is_configured():
        diag["configured"] = False
        diag["error"] = "not configured — см. diagnostic выше"
        return diag
    try:
        bal = await get_hot_wallet_balance()
        bal["address"] = get_hot_wallet_address()
        bal["configured"] = True
        bal["diagnostic"] = diag
        return bal
    except Exception as e:
        return {**diag, "error": str(e), "configured": False}


# --- 2FA: список pending запросов (для JARVIS owner view) ---
@app.get("/api/2fa/pending")
async def api_2fa_pending(me: dict = Depends(_get_me)):
    if me.get("role") != "owner":
        raise HTTPException(403, "owner only")
    storage.reload_sync()
    return {"requests": storage.list_2fa_requests(status="pending")}


# --- Cleanup данных (Owner only): фикс багa где CRM-бот стал supplier'ом ---
@app.post("/api/admin/cleanup_lk_suppliers")
async def admin_cleanup_lk_suppliers(request: Request, me: dict = Depends(_get_me)):
    """Чистит ЛК-карточки где supplier = системный аккаунт (PrideCONTROLE_bot
    и т.п.). Для каждой такой берёт client_username из managed_chats[work_chat_id]
    и проставляет туда. Owner-only."""
    if me.get("role") != "owner":
        raise HTTPException(403, "owner only")
    storage.reload_sync()
    SYS = {
        "pridecontrole_bot", "prideкоntrol", "prideконтроль",
        "pride_sys01", "pride_sys02", "pride_manager1",
        "simba_pride_adm", "timonskupcl", "aleksandrkarpov_aw",
        "prideassistant", "pride_assistant", "pride_invite_bot",
        "pridework_invite_bot", "prideoutsource_bot",
    }
    cards = storage.list_lk_cards() or {}
    managed = storage.state.get("managed_chats") or {}

    # Index managed_chats by work_chat_id (как Telethon отдаёт — с -100).
    # Ключ может быть в разных форматах, поэтому индексируем оба варианта.
    from storage import _norm_chat_id as _norm
    by_chat = {}
    for k, info in managed.items():
        try:
            by_chat[_norm(k)] = info
        except Exception:
            pass

    fixed = []
    cleared = []
    for cid, c in cards.items():
        if not c:
            continue
        sup = ((c.get("supplier") or "").lstrip("@") or "").lower().strip()
        if sup not in SYS:
            continue
        wc = c.get("work_chat_id") or 0
        try:
            wc_norm = _norm(wc) if wc else 0
        except Exception:
            wc_norm = 0
        info = by_chat.get(wc_norm) or {}
        new_supplier = (info.get("client_username") or "").lstrip("@").strip()
        if new_supplier:
            await storage.update_lk_card(cid, supplier=new_supplier)
            fixed.append({"card_id": cid, "old": sup, "new": new_supplier})
        else:
            # Чат не найден — просто очищаем supplier (notify будет использовать
            # work_chat_id напрямую, что и так уже исправлено в коде)
            await storage.update_lk_card(cid, supplier="")
            cleared.append({"card_id": cid, "old": sup, "wc": wc})
    return {"ok": True, "fixed": fixed, "cleared": cleared, "total": len(fixed) + len(cleared)}


@app.post("/api/admin/cleanup_crm_owners")
async def admin_cleanup_crm_owners(request: Request, me: dict = Depends(_get_me)):
    """Удаляет crm_owners где username = системный аккаунт. Owner-only."""
    if me.get("role") != "owner":
        raise HTTPException(403, "owner only")
    storage.reload_sync()
    SYS = {
        "pridecontrole_bot", "prideкоntrol", "prideконтроль",
        "pride_sys01", "pride_sys02", "pride_manager1",
        "simba_pride_adm", "timonskupcl", "aleksandrkarpov_aw",
        "prideassistant", "pride_assistant", "pride_invite_bot",
        "pridework_invite_bot", "prideoutsource_bot",
    }
    owners = storage.state.get("crm_owners") or {}
    removed = []
    for oid, o in list(owners.items()):
        if not o:
            continue
        uname = ((o.get("username") or "").lstrip("@") or "").lower().strip()
        if uname in SYS:
            del owners[oid]
            removed.append({"owner_id": oid, "username": uname, "name": o.get("name")})
    await storage.save()
    return {"ok": True, "removed": removed, "count": len(removed)}


# --- Рассылки Асика (утро/вечер во все managed_chats) ---
@app.get("/api/settings/asik_broadcasts")
async def settings_get_asik_broadcasts(me: dict = Depends(_get_me)):
    return {
        "ok": True,
        "morning": storage.get_asik_broadcast("morning"),
        "evening": storage.get_asik_broadcast("evening"),
    }


@app.post("/api/settings/asik_broadcasts/{slot}")
async def settings_set_asik_broadcast(
    slot: str, request: Request, me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("settings_pricing_set")),
):
    """slot = morning | evening. Body:
    {enabled?: bool, time_hhmm?: 'HH:MM', text?: str, append_pricing?: bool}"""
    _require_owner_or_manager(me)
    if slot not in ("morning", "evening"):
        raise HTTPException(400, "slot must be morning or evening")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    await storage.set_asik_broadcast(
        slot,
        enabled=body.get("enabled"),
        time_hhmm=body.get("time_hhmm"),
        text=body.get("text"),
        append_pricing=body.get("append_pricing"),
    )
    return {"ok": True, "slot": slot, "current": storage.get_asik_broadcast(slot)}


# --- Knowledge overrides (Прайс + Правила забора ЛК) ---
@app.get("/api/settings/knowledge")
async def settings_get_knowledge(me: dict = Depends(_get_me)):
    """Возвращает текущие raw-тексты прайса/правил + chat_id админ-беседы."""
    ov = storage.get_knowledge_overrides()
    return {
        "ok": True,
        "pricing": ov.get("pricing") or "",
        "lk_rules": ov.get("lk_rules") or "",
        "pricing_updated_at": ov.get("pricing_updated_at") or 0,
        "lk_rules_updated_at": ov.get("lk_rules_updated_at") or 0,
        "pricing_updated_by": ov.get("pricing_updated_by") or "",
        "lk_rules_updated_by": ov.get("lk_rules_updated_by") or "",
        "knowledge_admin_chat_id": storage.get_knowledge_admin_chat_id(),
    }


@app.post("/api/settings/knowledge/pricing")
async def settings_set_knowledge_pricing(
    request: Request, me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("settings_pricing_set")),
):
    """Body: {text: str}. Сохраняет raw-прайс."""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    text = (body.get("text") or "").strip()
    uname = me.get("username") or str(me.get("id") or "")
    await storage.set_knowledge_override("pricing", text, updated_by=uname)
    return {"ok": True, "len": len(text)}


@app.post("/api/settings/knowledge/lk_rules")
async def settings_set_knowledge_rules(
    request: Request, me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("settings_pricing_set")),
):
    """Body: {text: str}. Сохраняет raw-правила забора ЛК."""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    text = (body.get("text") or "").strip()
    uname = me.get("username") or str(me.get("id") or "")
    await storage.set_knowledge_override("lk_rules", text, updated_by=uname)
    return {"ok": True, "len": len(text)}


@app.post("/api/settings/knowledge/chat_id")
async def settings_set_knowledge_chat_id(
    request: Request, me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("settings_pricing_set")),
):
    """Body: {chat_id: int}. Telegram chat_id беседы где userbot ловит
    «ОБНОВИ ПРАЙС / ПРАВИЛА ЗАБОРА ЛК»."""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    try:
        cid = int(body.get("chat_id") or 0)
    except Exception:
        cid = 0
    await storage.set_knowledge_admin_chat_id(cid)
    return {"ok": True, "chat_id": cid}


# --- Прайс ЛК ---
@app.post("/api/settings/pricing/set")
async def settings_set_pricing(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("settings_pricing_set"))):
    """Body: {bank: str, price: number}. Удалить — price=0."""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    bank = (body.get("bank") or "").strip().upper()
    price = float(body.get("price") or 0)
    if not bank:
        raise HTTPException(400, "bank required")
    await storage.set_pricing(bank, price)
    return {"ok": True, "bank": bank, "price": price}


@app.post("/api/settings/pricing/delete")
async def settings_delete_pricing(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("settings_pricing_delete"))):
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    bank = (body.get("bank") or "").strip().upper()
    if not bank:
        raise HTTPException(400, "bank required")
    pr = storage.state.setdefault("pricing", {})
    if bank in pr:
        del pr[bank]
        await storage.save()
    return {"ok": True, "bank": bank}


# --- AI настройки ---
@app.post("/api/settings/ai/update")
async def settings_update_ai(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("settings_ai"))):
    """Body: {ai_enabled?, ai_model?, ai_max_tokens?, ai_history_limit?,
             ai_typing_delay_min?, ai_typing_delay_max?, client_idle_minutes?}"""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    if "ai_enabled" in body:
        await storage.set_ai_enabled(bool(body["ai_enabled"]))
    if "ai_model" in body:
        await storage.set_ai_model(str(body["ai_model"]).strip())
    for k in ["ai_max_tokens", "ai_history_limit"]:
        if k in body:
            try: storage.state[k] = int(body[k])
            except Exception: pass
    for k in ["ai_typing_delay_min", "ai_typing_delay_max"]:
        if k in body:
            try: storage.state[k] = float(body[k])
            except Exception: pass
    if "client_idle_minutes" in body:
        try: storage.state["client_idle_minutes"] = int(body["client_idle_minutes"])
        except Exception: pass
    await storage.save()
    return {"ok": True}


# --- Telegram чаты ---
@app.post("/api/settings/tg_chats/update")
async def settings_update_tg_chats(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("settings_tg_chats"))):
    """Body: {brain_chat_id?, lk_group_id?, coordination_chat_id?, ideas_chat_id?, accounting_group_id?}"""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    field_to_setter = {
        "brain_chat_id": storage.set_brain_chat_id,
        "lk_group_id": storage.set_lk_group_id,
        "coordination_chat_id": storage.set_coordination_chat_id,
    }
    for f, setter in field_to_setter.items():
        if f in body:
            try:
                cid = int(body[f] or 0)
                # Нормализация: если положительное и > 0 — добавляем -100 префикс
                if cid > 0 and cid < 10**12:
                    cid = -1000000000000 - cid
                await setter(cid)
            except Exception as e:
                logger.warning("set %s fail: %s", f, e)
    # ideas + accounting через прямой state-set
    for f in ["ideas_chat_id", "accounting_group_id"]:
        if f in body:
            try:
                cid = int(body[f] or 0)
                if cid > 0 and cid < 10**12:
                    cid = -1000000000000 - cid
                storage.state[f] = cid
            except Exception:
                pass
    await storage.save()
    return {"ok": True}


# --- Invite-бот ---
@app.post("/api/settings/invite/update")
async def settings_update_invite(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("settings_invite"))):
    """Body: {welcome_message?, invite_jobs_text?, invite_welcome_gif_id?, trigger_phrases?, cooldown_minutes?}"""
    _require_owner_or_manager(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    if "welcome_message" in body:
        await storage.set_welcome(str(body["welcome_message"]))
    if "invite_jobs_text" in body and hasattr(storage, "set_invite_jobs_text"):
        await storage.set_invite_jobs_text(str(body["invite_jobs_text"]))
    if "invite_welcome_gif_id" in body and hasattr(storage, "set_invite_welcome_gif"):
        await storage.set_invite_welcome_gif(str(body["invite_welcome_gif_id"]))
    if "trigger_phrases" in body and isinstance(body["trigger_phrases"], list):
        storage.state["trigger_phrases"] = [str(s).strip() for s in body["trigger_phrases"] if str(s).strip()]
    if "cooldown_minutes" in body:
        try: storage.state["cooldown_minutes"] = int(body["cooldown_minutes"])
        except Exception: pass
    await storage.save()
    return {"ok": True}


# =====================================================================
# OWNER PANEL — управление ролями и разрешениями
# =====================================================================
# Доступ только для simba_pride_adm (или role == "owner" из worker_roles).

def _require_owner(me: dict):
    """Пускаем только владельца. Raise HTTPException(403) иначе."""
    if (me.get("role") or "").lower() != "owner":
        raise HTTPException(403, "owner only")


@app.get("/api/owner/roles")
async def owner_list_roles(me: dict = Depends(_get_me)):
    _require_owner(me)
    if not hasattr(storage, "list_role_permissions"):
        return {"ok": True, "roles": {}, "all_views": [], "all_actions": []}
    return {
        "ok": True,
        "roles": storage.list_role_permissions(),
        "all_views": storage.list_all_known_views(),
        "all_actions": storage.list_all_known_actions(),
        "all_subviews": storage.list_all_known_subviews() if hasattr(storage, "list_all_known_subviews") else {},
    }


@app.post("/api/owner/roles")
async def owner_set_role(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("owner_role_create_update"))):
    """Body: {role, label?, views?, edit_actions?, view_readonly?, subviews?, subview_readonly?}"""
    _require_owner(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    role = (body.get("role") or "").strip().lower()
    if not role or not role.replace("_", "").isalnum():
        raise HTTPException(400, "role must be alphanumeric (or underscore)")
    entry = await storage.set_role_permission(
        role,
        label=body.get("label") or "",
        views=body.get("views"),
        edit_actions=body.get("edit_actions"),
        view_readonly=body.get("view_readonly"),
        subviews=body.get("subviews"),
        subview_readonly=body.get("subview_readonly"),
    )
    return {"ok": True, "role": role, "data": entry}


@app.delete("/api/owner/roles/{role}")
async def owner_delete_role(role: str, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("owner_role_delete"))):
    _require_owner(me)
    ok = await storage.delete_role_permission(role)
    if not ok:
        raise HTTPException(400, "Default role (cannot delete) or not found")
    return {"ok": True, "role": role}


@app.get("/api/owner/users")
async def owner_list_users(me: dict = Depends(_get_me)):
    """Список всех известных юзеров (из tg_user_info) + их роли. Доступно только owner."""
    _require_owner(me)
    storage.reload_sync()
    users = storage.state.get("tg_user_info") or {}
    roles_map = storage.list_worker_roles() or {}
    out = []
    for uid_str, info in users.items():
        username = (info.get("username") or "").lstrip("@").lower()
        role_info = roles_map.get(username) if username else None
        out.append({
            "tg_user_id": int(uid_str) if str(uid_str).lstrip("-").isdigit() else 0,
            "username": username,
            "first_name": info.get("first_name") or "",
            "last_name": info.get("last_name") or "",
            "photo_url": info.get("photo_url") or "",
            "last_seen_ts": info.get("last_seen_ts") or 0,
            "role": (role_info or {}).get("role") or "",
            "is_admin": bool((role_info or {}).get("is_admin")),
        })
    out.sort(key=lambda x: -(x.get("last_seen_ts") or 0))
    return {"ok": True, "users": out, "count": len(out)}


@app.post("/api/owner/users/{username}/role")
async def owner_set_user_role(username: str, request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("owner_user_role"))):
    """Body: {role: str, is_admin?: bool}. Доступно только owner."""
    _require_owner(me)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    new_role = (body.get("role") or "").strip().lower()
    is_admin = bool(body.get("is_admin", False))
    uname = (username or "").lstrip("@").lower()
    if not uname:
        raise HTTPException(400, "username required")
    # Если role пустой — снимаем роль
    if not new_role:
        await storage.delete_worker_role(uname) if hasattr(storage, "delete_worker_role") else None
        return {"ok": True, "username": uname, "role": ""}
    # Иначе проверим что такая роль существует
    if not storage.get_role_permission(new_role):
        raise HTTPException(400, f"Unknown role: {new_role}")
    await storage.set_worker_role(uname, role=new_role, is_admin=is_admin)
    return {"ok": True, "username": uname, "role": new_role, "is_admin": is_admin}


# =====================================================================
# GUEST CALLS — звонки по одноразовой ссылке (Яндекс.Телемост-стиль)
# =====================================================================

@app.post("/api/calls/create")
async def calls_create(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("call_create"))):
    """Создаёт комнату для звонка. Любой авторизованный пользователь.
    Body: {name?: str, password?: str, max_participants?: int}.
    Возвращает {room_id, password, url}."""
    if not me.get("username"):
        raise HTTPException(403, "auth required")
    body = {}
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        pass
    name = (body.get("name") or "").strip() or "Звонок"
    password = (body.get("password") or "").strip()
    max_participants = int(body.get("max_participants") or 10)
    entry = await storage.create_guest_call(
        created_by=me.get("username") or "",
        name=name, password=password, max_participants=max_participants,
    )
    base_url = _os_kuc.environ.get("KUC_BASE_URL") or str(request.base_url).rstrip("/")
    url = f"{base_url}/call/{entry['room_id']}"
    return {
        "ok": True, "room_id": entry["room_id"], "password": entry["password"],
        "url": url, "name": entry["name"],
    }


@app.get("/api/calls/list")
async def calls_list(me: dict = Depends(_get_me)):
    """Список активных звонков. Все авторизованные видят."""
    if not me.get("username"):
        raise HTTPException(403, "auth required")
    items = storage.list_guest_calls(only_active=True) if hasattr(storage, "list_guest_calls") else {}
    return {"ok": True, "calls": items, "count": len(items)}


@app.post("/api/calls/{room_id}/end")
async def calls_end(room_id: str, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("call_end"))):
    """Завершить звонок. Только creator или owner."""
    gc = storage.get_guest_call(room_id)
    if not gc:
        raise HTTPException(404, "room not found")
    if (me.get("role") or "") != "owner" and gc.get("created_by") != (me.get("username") or ""):
        raise HTTPException(403, "only creator or owner can end")
    await storage.end_guest_call(room_id, ended_by=me.get("username") or "")
    return {"ok": True, "room_id": room_id}


@app.get("/call/{room_id}")
async def guest_call_page(room_id: str):
    """Отдаёт guest_call.html для гостя. Без auth — публичный по ссылке."""
    gc = storage.get_guest_call(room_id)
    if not gc:
        return HTMLResponse("<h1>Звонок не найден или завершён</h1>", status_code=404)
    if gc.get("ended_at"):
        return HTMLResponse("<h1>Звонок завершён</h1>", status_code=410)
    path = _Path_kuc(__file__).parent / "dashboard" / "guest_call.html"
    if not path.exists():
        return HTMLResponse("<h1>Страница не найдена</h1>", status_code=500)
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/calls/{room_id}/info")
async def calls_info_public(room_id: str):
    """Публичная инфа о звонке (без password). Для guest-страницы."""
    gc = storage.get_guest_call(room_id)
    if not gc or gc.get("ended_at"):
        raise HTTPException(404, "room not found or ended")
    return {
        "ok": True, "room_id": room_id,
        "name": gc.get("name") or "Звонок",
        "active_participants": gc.get("active_participants") or [],
        "created_at": gc.get("created_at") or 0,
    }


@app.post("/api/calls/{room_id}/join")
async def calls_join(room_id: str, request: Request, _perm: bool = Depends(require_action("call_join"))):
    """Гость пытается войти в комнату. Body: {name: str, password: str}.
    Возвращает participant_id для WS-подключения."""
    gc = storage.get_guest_call(room_id)
    if not gc or gc.get("ended_at"):
        raise HTTPException(404, "room not found or ended")
    body = {}
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        pass
    name = (body.get("name") or "").strip() or "Гость"
    password = (body.get("password") or "").strip()
    if password != gc.get("password"):
        raise HTTPException(401, "wrong password")
    # Лимит участников
    parts = gc.get("active_participants") or []
    if len(parts) >= int(gc.get("max_participants") or 10):
        raise HTTPException(429, "Room is full")
    # Создаём participant_id
    import uuid as _uuid
    participant_id = _uuid.uuid4().hex[:12]
    await storage.add_guest_participant(room_id, participant_id, name=name)
    return {
        "ok": True, "participant_id": participant_id, "name": name,
        "room_id": room_id,
        "ws_url": f"/ws-guest-call?room_id={room_id}&participant_id={participant_id}",
        "active_participants": gc.get("active_participants") or [],
    }


# WebSocket signaling для гостей
_guest_call_sessions = {}  # {participant_id: {ws, room_id, name}}


@app.websocket("/ws-guest-call")
async def guest_call_ws(ws: WebSocket):
    """WebSocket signaling для гостевых звонков. Параметры query: room_id, participant_id.
    Протокол похож на /ws-discord:
      Клиент → Сервер: {type: "signal", target, payload}, {type: "ping"}, {type: "leave"}
      Сервер → Клиент: {type: "ready", participant_id, peers}, {type: "peer-joined", participant},
                       {type: "peer-left", participant_id}, {type: "signal", from, payload}
    """
    room_id = ws.query_params.get("room_id", "")
    participant_id = ws.query_params.get("participant_id", "")
    if not room_id or not participant_id:
        await ws.close(code=4400)
        return
    gc = storage.get_guest_call(room_id)
    if not gc or gc.get("ended_at"):
        await ws.close(code=4404)
        return
    # Проверим что participant зарегистрирован
    parts = gc.get("active_participants") or []
    me_part = next((p for p in parts if p.get("participant_id") == participant_id), None)
    if not me_part:
        await ws.close(code=4401)
        return
    try:
        await ws.accept()
    except Exception:
        return
    # Сохраним сессию
    _guest_call_sessions[participant_id] = {"ws": ws, "room_id": room_id, "name": me_part.get("name", "")}
    # Список других участников в комнате (для mesh)
    peers_in_room = [
        {"participant_id": pid, "name": s.get("name", "")}
        for pid, s in _guest_call_sessions.items()
        if s.get("room_id") == room_id and pid != participant_id
    ]
    # Шлём ready
    try:
        await ws.send_json({"type": "ready", "participant_id": participant_id, "peers": peers_in_room})
    except Exception:
        pass
    # Уведомим остальных peers что этот вошёл
    for pid, s in list(_guest_call_sessions.items()):
        if s.get("room_id") == room_id and pid != participant_id:
            try:
                await s["ws"].send_json({"type": "peer-joined",
                                          "participant": {"participant_id": participant_id,
                                                          "name": me_part.get("name", "")}})
            except Exception:
                pass
    # Loop
    try:
        while True:
            data = await ws.receive_json()
            mtype = data.get("type", "")
            if mtype == "ping":
                try: await ws.send_json({"type": "pong"})
                except Exception: pass
                continue
            if mtype == "leave":
                break
            if mtype == "signal":
                target = data.get("target", "")
                payload = data.get("payload", {})
                target_s = _guest_call_sessions.get(target)
                if target_s and target_s.get("room_id") == room_id:
                    try:
                        await target_s["ws"].send_json({
                            "type": "signal", "from": participant_id, "payload": payload,
                        })
                    except Exception:
                        pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("guest-call ws %s: %s", participant_id, e)
    finally:
        # Cleanup
        _guest_call_sessions.pop(participant_id, None)
        try:
            await storage.remove_guest_participant(room_id, participant_id)
        except Exception:
            pass
        # Уведомим остальных
        for pid, s in list(_guest_call_sessions.items()):
            if s.get("room_id") == room_id:
                try:
                    await s["ws"].send_json({"type": "peer-left", "participant_id": participant_id})
                except Exception:
                    pass


# =====================================================================
# OUTSOURCE (Аутсорс — маркетплейс ЛК для управляющих)
# =====================================================================

@app.get("/api/system/outsource_pending_lk")
async def api_system_outsource_pending_lk(_: None = Depends(_auth)):
    storage.reload_sync()
    lks_raw = storage.list_outsource_drop_lks() if hasattr(storage, "list_outsource_drop_lks") else {}
    drops_raw = storage.list_outsource_drops() if hasattr(storage, "list_outsource_drops") else {}
    out = []
    stage_order = {"": 0, "ready_asked": 1, "ready_confirmed": 2, "login_asked": 3,
                   "login_received": 4, "perevyaz_asked": 5, "perevyaz_received": 6, "done": 99}
    for lkid, lk in lks_raw.items():
        stage = (lk.get("sms_stage") or "").strip()
        # done ЛК остаются — с completed=True (см. CRM pending выше)
        drop = drops_raw.get(lk.get("outsource_drop_id"), {}) if drops_raw else {}
        manager = (lk.get("manager_username") or drop.get("manager_username") or "").lstrip("@")
        out.append({
            "droplk_id": lkid, "outsource_drop_id": lk.get("outsource_drop_id"),
            "bank": lk.get("bank") or "", "fio": drop.get("fio") or "—",
            "manager": manager, "value": lk.get("value") or "",
            "new_login": lk.get("new_login") or "", "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "", "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "", "ded_pass": lk.get("ded_pass") or "",
            "ded_password": lk.get("ded_pass") or "",
            "ded_ip": lk.get("ded_ip") or "", "ded_location": lk.get("ded_location") or "",
            "sms_stage": stage, "_stage_order": stage_order.get(stage, 50),
            "completed": stage == "done",
            "created_at": lk.get("created_at") or 0, "updated_at": lk.get("updated_at") or 0,
            "slot_number": _compute_lk_slot(drop, lkid)[0],
            "slot_total": _compute_lk_slot(drop, lkid)[1],
            "track": "outsource", "drop_number": drop.get("drop_id") or "",
        })
    out.sort(key=lambda x: -(x.get("created_at") or 0))
    return {"ok": True, "items": out, "lks": out, "count": len(out)}


@app.get("/api/system/outsource_passwords_inbox")
async def api_system_outsource_passwords_inbox(_: None = Depends(_auth)):
    storage.reload_sync()
    lks_raw = storage.list_outsource_drop_lks() if hasattr(storage, "list_outsource_drop_lks") else {}
    drops_raw = storage.list_outsource_drops() if hasattr(storage, "list_outsource_drops") else {}
    out = []
    for lkid, lk in lks_raw.items():
        stage = (lk.get("sms_stage") or "").strip()
        if stage not in ("perevyaz_received", "done", "login_received", "perevyaz_asked"):
            continue
        drop = drops_raw.get(lk.get("outsource_drop_id"), {}) if drops_raw else {}
        manager = (lk.get("manager_username") or drop.get("manager_username") or "").lstrip("@")
        filled_creds = bool((lk.get("new_login") or "").strip() and (lk.get("new_password") or "").strip())
        filled_dedik = bool((lk.get("ded_ip") or "").strip() and (lk.get("ded_pass") or "").strip())
        out.append({
            "droplk_id": lkid, "outsource_drop_id": lk.get("outsource_drop_id"),
            "bank": lk.get("bank") or "", "fio": drop.get("fio") or "—",
            "manager": manager,
            "new_login": lk.get("new_login") or "", "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "", "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "Administrator", "ded_pass": lk.get("ded_pass") or "",
            "ded_password": lk.get("ded_pass") or "",
            "ded_ip": lk.get("ded_ip") or "", "ded_location": lk.get("ded_location") or "",
            "sms_stage": stage, "filled_creds": filled_creds, "filled_dedik": filled_dedik,
            "filled": filled_creds and filled_dedik,
            "updated_at": lk.get("updated_at") or 0, "created_at": lk.get("created_at") or 0,
            "slot_number": _compute_lk_slot(drop, lkid)[0],
            "slot_total": _compute_lk_slot(drop, lkid)[1],
            "track": "outsource", "drop_number": drop.get("drop_id") or "",
        })
    out.sort(key=lambda x: -(x.get("created_at") or 0))
    return {"ok": True, "items": out, "count": len(out)}


@app.get("/api/system/outsource_managers")
async def api_system_outsource_managers(_: None = Depends(_auth)):
    storage.reload_sync()
    mgrs = storage.list_outsource_managers() if hasattr(storage, "list_outsource_managers") else {}
    chats = storage.list_outsource_chats() if hasattr(storage, "list_outsource_chats") else {}
    chats_per_manager = {}
    for chat_entry in chats.values():
        u = chat_entry.get("manager_username") or ""
        if u: chats_per_manager[u] = chats_per_manager.get(u, 0) + 1
    out = []
    for u, m in mgrs.items():
        out.append({
            "username": u, "tg_user_id": m.get("tg_user_id") or 0,
            "first_seen_ts": m.get("first_seen_ts") or 0,
            "last_active_ts": m.get("last_active_ts") or 0,
            "stats": m.get("stats") or {}, "chats_count": chats_per_manager.get(u, 0),
            "wallet_balance_usdt": m.get("wallet_balance_usdt") or 0.0,
            "paid_total_usdt": m.get("paid_total_usdt") or 0.0,
        })
    out.sort(key=lambda x: -(x.get("last_active_ts") or 0))
    return {"ok": True, "items": out, "count": len(out)}


@app.post("/api/system/lk/{droplk_id}/move_to_outsource")
async def api_system_lk_move_to_outsource(droplk_id: str, request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("move_lk_to_outsource"))):
    """Перенос в АУТСОРС-КАТАЛОГ (общий пул).
    Body: {list_price_usdt: float, manager_username?: str (опц.)}
    Если manager_username не указан — ЛК идёт в пул каталога без владельца.
    Управляющий потом сам "выкупает" из бота."""
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    manager_username = (body.get("manager_username") or "").strip().lstrip("@").lower()
    list_price_usdt = float(body.get("list_price_usdt") or 0)
    if not hasattr(storage, "move_any_lk_to_outsource"):
        raise HTTPException(500, "move_any_lk_to_outsource not available")
    try:
        if manager_username:
            await storage.register_outsource_manager(username=manager_username)
        new_id = await storage.move_any_lk_to_outsource(droplk_id, manager_username=manager_username)
        if not new_id:
            raise HTTPException(404, f"ЛК {droplk_id} не найден")
        # Сохраняем цену каталога
        if list_price_usdt > 0:
            await storage.update_outsource_drop_lk(new_id,
                list_price_usdt=list_price_usdt,
                listed_at=__import__("time").time(),
                listed_by=me.get("username") or "",
                # pool-флаг для бота: если manager_username пуст — в каталог
                in_pool=(not manager_username),
            )
        try:
            event_bus.emit_event("lk-moved-to-outsource", {
                "from_droplk_id": droplk_id, "to_outsource_droplk_id": new_id,
                "manager": manager_username, "moved_by": me.get("username") or "",
            }, severity="info")
        except Exception: pass
        return {"ok": True, "new_outsource_droplk_id": new_id, "manager": manager_username}
    except HTTPException: raise
    except Exception as e:
        logger.exception("move_to_outsource failed: %s", e)
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════
# СВЯЗКИ ЛК (bundles) — продаются одним пакетом через @marketplace_PRIDE_BOT
# ═══════════════════════════════════════════════════════════════
@app.get("/api/system/outsource/bundles")
async def api_outsource_bundles_list(me: dict = Depends(_get_me)):
    """Список всех связок outsource (с обогащёнными данными ЛК)."""
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    bundles = storage.list_outsource_bundles() if hasattr(storage, "list_outsource_bundles") else {}
    all_lks = storage.list_outsource_drop_lks() if hasattr(storage, "list_outsource_drop_lks") else {}
    all_drops = storage.list_outsource_drops() if hasattr(storage, "list_outsource_drops") else {}
    items = []
    for bid, b in bundles.items():
        lks_info = []
        for lkid in b.get("lk_ids", []):
            lk = all_lks.get(str(lkid)) or {}
            drop = all_drops.get(lk.get("outsource_drop_id")) or {}
            lks_info.append({
                "droplk_id": lkid,
                "bank": lk.get("bank") or "—",
                "fio": drop.get("fio") or "—",
            })
        items.append({
            "bundle_id": bid,
            "name": b.get("name") or "",
            "list_price_usdt": float(b.get("list_price_usdt") or 0),
            "in_pool": bool(b.get("in_pool")),
            "manager_username": b.get("manager_username") or "",
            "created_at": b.get("created_at") or 0,
            "bought_at": b.get("bought_at") or 0,
            "lks": lks_info,
            "lk_count": len(lks_info),
        })
    items.sort(key=lambda x: -(x["created_at"] or 0))
    return {"items": items}


@app.post("/api/system/outsource/bundles/create")
async def api_outsource_bundle_create(request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("bundle_create"))):
    """Body: {lk_ids: [...], list_price_usdt: float, name?: str}
    Все ЛК должны быть в пуле outsource (in_pool=True, без manager, не в связке).
    """
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    lk_ids = body.get("lk_ids") or []
    list_price_usdt = float(body.get("list_price_usdt") or 0)
    name = (body.get("name") or "").strip()
    if not isinstance(lk_ids, list) or len(lk_ids) < 2:
        raise HTTPException(400, "Нужно >= 2 ЛК")
    if list_price_usdt <= 0:
        raise HTTPException(400, "Цена должна быть > 0")
    if not hasattr(storage, "create_outsource_bundle"):
        raise HTTPException(500, "create_outsource_bundle not available")
    bid = await storage.create_outsource_bundle(
        lk_ids=lk_ids, list_price_usdt=list_price_usdt,
        name=name, created_by=me.get("username") or "",
    )
    if not bid:
        raise HTTPException(400, "Не удалось создать связку (проверьте что все ЛК свободны)")
    try:
        await _log_event("outsource_bundle_created", {
            "bundle_id": bid, "lk_count": len(lk_ids),
            "list_price_usdt": list_price_usdt,
            "created_by": me.get("username") or "",
        }, severity="info")
    except Exception: pass
    return {"ok": True, "bundle_id": bid}


@app.post("/api/system/outsource/bundles/{bundle_id}/dissolve")
async def api_outsource_bundle_dissolve(bundle_id: str, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("bundle_dissolve"))):
    """Расформировать связку (только пока не куплена). ЛК вернутся в одиночки."""
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    if not hasattr(storage, "dissolve_outsource_bundle"):
        raise HTTPException(500, "dissolve_outsource_bundle not available")
    ok = await storage.dissolve_outsource_bundle(bundle_id)
    if not ok:
        raise HTTPException(400, "Не удалось расформировать (возможно уже куплена)")
    try:
        await _log_event("outsource_bundle_dissolved", {
            "bundle_id": bundle_id, "by": me.get("username") or "",
        }, severity="info")
    except Exception: pass
    return {"ok": True}


@app.post("/api/system/outsource_lk/{outsource_droplk_id}/move_to_supplier")
async def api_system_outsource_lk_move_to_supplier(outsource_droplk_id: str, request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("move_lk_to_supplier"))):
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    owner_id = (body.get("owner_id") or "").strip() or None
    if not hasattr(storage, "move_outsource_lk_to_crm"):
        raise HTTPException(500, "move_outsource_lk_to_crm not available")
    try:
        new_id = await storage.move_outsource_lk_to_crm(outsource_droplk_id, owner_id=owner_id)
        if not new_id:
            raise HTTPException(404, f"ЛК {outsource_droplk_id} не найден")
        return {"ok": True, "new_droplk_id": new_id, "owner_id": owner_id or ""}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


# =====================================================================
# MOVE LK BETWEEN TRACKS (Поставщики ↔ Кредитование)
# =====================================================================

@app.post("/api/system/lk/{droplk_id}/move_to_credit")
async def api_system_lk_move_to_credit(droplk_id: str, request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("move_lk_to_credit"))):
    """Переносит ЛК поставщика в КРЕДИТОВАНИЕ.
    Body: { "manager_username": "ivan" } — менеджер-юрист, к кому перейдёт ЛК.
    Доступно только owner/manager роли."""
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        body = {}
    manager_username = (body.get("manager_username") or "").strip().lstrip("@").lower()
    if not manager_username:
        raise HTTPException(400, "manager_username required")
    if not hasattr(storage, "move_crm_lk_to_credit"):
        raise HTTPException(500, "move_crm_lk_to_credit not available — обновите storage.py")
    try:
        # Регистрируем менеджера если впервые
        await storage.register_credit_manager(username=manager_username)
        new_id = await storage.move_crm_lk_to_credit(droplk_id, manager_username=manager_username)
        if not new_id:
            raise HTTPException(404, f"ЛК {droplk_id} не найден в crm_drop_lks")
        try:
            event_bus.emit_event("lk-moved-to-credit", {
                "from_droplk_id": droplk_id, "to_credit_droplk_id": new_id,
                "manager": manager_username, "moved_by": me.get("username") or "",
            }, severity="info")
        except Exception:
            pass
        return {"ok": True, "new_credit_droplk_id": new_id, "manager": manager_username}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("move_to_credit failed: %s", e)
        raise HTTPException(500, str(e))


@app.post("/api/system/credit_lk/{credit_droplk_id}/move_to_supplier")
async def api_system_credit_lk_move_to_supplier(credit_droplk_id: str, request: Request, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("move_lk_to_supplier"))):
    """Переносит ЛК из КРЕДИТОВАНИЯ обратно в crm_drop_lks (к поставщику).
    Body (опционально): { "owner_id": "owner-uuid" }. Если не указано — создастся draft."""
    if me.get("role") not in ("owner", "manager"):
        raise HTTPException(403, "forbidden")
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        body = {}
    owner_id = (body.get("owner_id") or "").strip() or None
    if not hasattr(storage, "move_credit_lk_to_crm"):
        raise HTTPException(500, "move_credit_lk_to_crm not available — обновите storage.py")
    try:
        new_id = await storage.move_credit_lk_to_crm(credit_droplk_id, owner_id=owner_id)
        if not new_id:
            raise HTTPException(404, f"ЛК {credit_droplk_id} не найден в credit_drop_lks")
        try:
            event_bus.emit_event("lk-moved-to-supplier", {
                "from_credit_droplk_id": credit_droplk_id, "to_droplk_id": new_id,
                "owner_id": owner_id, "moved_by": me.get("username") or "",
            }, severity="info")
        except Exception:
            pass
        return {"ok": True, "new_droplk_id": new_id, "owner_id": owner_id or ""}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("move_to_supplier failed: %s", e)
        raise HTTPException(500, str(e))


@app.post("/api/system/lk/{droplk_id}/sms_action")
async def api_system_sms_action(droplk_id: str, request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("lk_sms_action"))):
    """Триггерит SMS-stage переход через очередь команд CRM-боту.
    Эквивалент кнопки smsadv в TG-группе ЛК."""
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    action = (data.get("action") or "").strip()  # 'advance' | 'reset'
    if action not in ("advance", "reset"):
        raise HTTPException(400, "action must be 'advance' or 'reset'")
    # Шлём команду через dashboard_commands — userbot/crm_bot подхватит
    try:
        await storage.enqueue_dashboard_command(
            f"__sms_{action} {droplk_id}",
            source=f"dashboard-system-sms-{action}",
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    try:
        event_bus.emit_event("system-sms-action", {
            "droplk_id": droplk_id, "action": action,
        })
    except Exception:
        pass
    return {"ok": True, "droplk_id": droplk_id, "action": action}


@app.get("/api/system/access_codes")
async def api_system_access_codes(limit: int = 50, _: None = Depends(_auth)):
    """Последние полученные СМС-коды от клиентов (раздел ДОСТУПЫ)."""
    storage.reload_sync()
    lks_raw = (
        storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}
    ) or {}
    drops_raw = (
        storage.list_crm_drops() if hasattr(storage, "list_crm_drops") else {}
    ) or {}
    codes = []
    for lkid, lk in lks_raw.items():
        history = lk.get("sms_history") or []
        drop = drops_raw.get(lk.get("drop_id"), {}) if drops_raw else {}
        for h in history:
            codes.append({
                "droplk_id": lkid,
                "bank": lk.get("bank") or "",
                "fio": drop.get("fio") or "",
                "kind": h.get("kind") or "?",
                "code": h.get("code") or "",
                "ts": h.get("ts") or 0,
                "stage": h.get("stage") or "",
            })
    codes.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    limited = codes[:limit]
    return {"ok": True, "items": limited, "codes": limited}


@app.get("/api/system/passwords")
async def api_system_passwords(_: None = Depends(_auth)):
    """Список ЛК с заполненными паролями / дедиками (раздел ПАРОЛИ)."""
    storage.reload_sync()
    lks_raw = (
        storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}
    ) or {}
    drops_raw = (
        storage.list_crm_drops() if hasattr(storage, "list_crm_drops") else {}
    ) or {}
    out = []
    for lkid, lk in lks_raw.items():
        # Только те где есть хоть какие-то новые credentials
        if not any(lk.get(k) for k in (
            "new_login", "new_password", "new_mail", "new_number",
            "ded_login", "ded_password", "ded_ip", "code_word",
        )):
            continue
        drop = drops_raw.get(lk.get("drop_id"), {}) if drops_raw else {}
        out.append({
            "droplk_id": lkid,
            "bank": lk.get("bank") or "",
            "fio": drop.get("fio") or "",
            "new_login": lk.get("new_login") or "",
            "new_password": lk.get("new_password") or "",
            "new_mail": lk.get("new_mail") or "",
            "new_number": lk.get("new_number") or "",
            "code_word": lk.get("code_word") or "",
            "ded_login": lk.get("ded_login") or "",
            "ded_password": lk.get("ded_password") or "",
            "ded_ip": lk.get("ded_ip") or "",
            "updated_at": lk.get("updated_at") or 0,
        })
    out.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return {"ok": True, "items": out}


# ============== ACCOUNTING (Бухгалтерия) ==============

@app.get("/api/accounting/payouts_full")
async def api_accounting_payouts_full(_: None = Depends(_auth)):
    """Полный обзор выплат для бухгалтерии: 3 очереди + статистика."""
    storage.reload_sync()
    usdt = storage.list_payouts("usdt") or []
    release = storage.list_payouts("release") or []
    fund_release = storage.list_payouts("fund_release") or []
    total_amount_usdt = sum(
        float(p.get("amount_usdt") or 0) for p in (usdt + release + fund_release)
    )
    return {
        "ok": True,
        "queues": {
            "usdt": usdt, "release": release, "fund_release": fund_release,
        },
        "stats": {
            "count_usdt": len(usdt),
            "count_release": len(release),
            "count_fund_release": len(fund_release),
            "total_pending_usdt": total_amount_usdt,
        },
    }


@app.post("/api/accounting/payouts/{queue}/{payout_id}/note")
async def api_accounting_add_note(
    queue: str, payout_id: int, request: Request, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("accounting_payout_note")),
):
    """Добавить заметку бухгалтера к payout-записи."""
    if queue not in ("usdt", "release", "fund_release"):
        raise HTTPException(400, "invalid queue")
    data = await request.json()
    note = (data.get("note") or "").strip()
    if not note:
        raise HTTPException(400, "note required")
    if len(note) > 500:
        raise HTTPException(400, "note too long (max 500)")
    manager_uid = _try_session_auth(request) or 0
    ok = await storage.update_payout(queue, payout_id,
                                     accounting_note=note,
                                     accounting_note_by=manager_uid,
                                     accounting_note_at=__import__("time").time())
    if not ok:
        raise HTTPException(404, "payout not found")
    return {"ok": True, "payout_id": payout_id, "queue": queue}


# ============== END HELPDESK ==============


@app.get("/api/health/full")
async def api_health_full(_: None = Depends(_auth)):
    """Полная проверка всех систем (требует авторизации админа).
    Возвращает JSON со списком проверок: {ok, warn, fail}.
    Также доступно через TG: команда /healthcheck в @PrideCRMv4 (для админов)."""
    try:
        from health_check import HealthChecker
        h = HealthChecker()
        results = await h.run_all()
        summary = {
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "warn": sum(1 for r in results if r["status"] == "warn"),
            "fail": sum(1 for r in results if r["status"] == "fail"),
        }
        return {
            "ok": True,
            "summary": summary,
            "results": results,
            "telegram_message": h.format_telegram_message(),
        }
    except Exception as e:
        logger.exception("/api/health/full failed: %s", e)
        return {"ok": False, "error": str(e)}


# === Telegram OAuth ===

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Логин-страница с Telegram Login Widget. Если у юзера уже валидный
    session cookie — редиректит на /."""
    if _try_session_auth(request) is not None:
        return RedirectResponse(url="/", status_code=302)
    # Резолвим bot username из BOT_TOKEN если env не задан
    bot_uname = TG_BOT_USERNAME or await _resolve_bot_username()
    # Если бот не настроен — показываем заглушку
    if not (bot_uname and config.BOT_TOKEN and TG_ADMINS):
        return HTMLResponse(
            "<!DOCTYPE html><html><head><title>Login</title>"
            "<style>body{background:#050818;color:#d6e3ff;"
            "font-family:monospace;text-align:center;padding-top:80px;}</style>"
            "</head><body><h2>Telegram Login не настроен</h2>"
            "<p>Установи env:</p>"
            "<pre style='display:inline-block;text-align:left;'>"
            "BOT_TOKEN=...  (уже есть)\n"
            "TG_ADMINS=12345,67890   (твой Telegram user_id)\n"
            "ADMIN_ID=12345          (или этот — fallback)</pre>"
            "<p>TG_BOT_USERNAME резолвится автоматически из BOT_TOKEN.</p>"
            "<p>Или используй Basic auth: <code>AUTH_STRICT_TELEGRAM=0</code> + DASHBOARD_USER/PASS</p>"
            "</body></html>",
            status_code=200,
        )
    # Telegram Login Widget — рендерим страницу
    html = """<!DOCTYPE html><html><head>
<meta charset='UTF-8'><title>J.A.R.V.I.S. · Login</title>
<style>
  body {
    margin: 0; min-height: 100vh;
    background: radial-gradient(ellipse at center, #0a1240 0%, #050818 80%);
    color: #d6e3ff;
    font-family: "JetBrains Mono", monospace;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
  }
  body::before {
    content: ''; position: fixed; inset: 0;
    background:
      linear-gradient(rgba(0,229,255,0.06) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,229,255,0.06) 1px, transparent 1px);
    background-size: 40px 40px;
    mask: radial-gradient(ellipse at center, black 40%, transparent 80%);
    pointer-events: none;
  }
  .logo {
    font-size: 48px; font-weight: 900;
    letter-spacing: 14px; color: #00e5ff;
    text-shadow: 0 0 24px #00e5ff;
    margin-bottom: 8px;
  }
  .sub {
    font-size: 11px; letter-spacing: 4px; color: #5b7299;
    margin-bottom: 50px;
  }
  .login-box {
    border: 1px solid rgba(0,229,255,0.3);
    background: rgba(8,14,30,0.7);
    padding: 30px 40px; border-radius: 8px;
    text-align: center;
    box-shadow: 0 0 30px rgba(0,229,255,0.15);
  }
  .label {
    font-size: 10px; letter-spacing: 2px;
    color: #5b7299; text-transform: uppercase;
    margin-bottom: 16px;
  }
  .hint {
    font-size: 10px; color: #5b7299;
    margin-top: 20px; max-width: 300px;
    line-height: 1.6;
  }
</style>
</head><body>
  <div class="logo">JARVIS</div>
  <div class="sub">PRIDE OPERATIONS · ACCESS REQUIRED</div>
  <div class="login-box">
    <div class="label">Войдите через Telegram</div>
    <script async src="https://telegram.org/js/telegram-widget.js?22"
      data-telegram-login="__BOT_USERNAME__"
      data-size="large"
      data-radius="6"
      data-auth-url="/tg/callback"
      data-request-access="write"></script>
    <div class="hint">
      Только админы из TG_ADMINS env могут войти.<br>
      Для альтернативного входа открой <code style="color:#00e5ff">/</code> — браузер запросит Basic auth.
    </div>
  </div>
</body></html>"""
    return HTMLResponse(html.replace("__BOT_USERNAME__", bot_uname))


@app.get("/tg/callback")
async def tg_callback(request: Request):
    """Callback от Telegram Login Widget — проверяет hash, ставит session cookie."""
    if not config.BOT_TOKEN:
        raise HTTPException(status_code=503, detail="BOT_TOKEN not configured")
    # Telegram передаёт поля как query string
    data = dict(request.query_params)
    if not _verify_telegram_login(data):
        return HTMLResponse(
            "<h2>❌ Invalid Telegram login signature</h2>"
            "<p>Hash check failed. <a href='/login'>Retry</a>.</p>",
            status_code=401,
        )
    try:
        uid = int(data.get("id", 0))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="bad user id")
    if TG_ADMINS and uid not in TG_ADMINS:
        return HTMLResponse(
            f"<h2>🚫 User {uid} не в списке TG_ADMINS</h2>"
            "<p>Свяжись с владельцем.</p>",
            status_code=403,
        )
    # Подписанный куки + HTML который закрывает popup и обновляет parent.
    # Telegram Login Widget открывает /tg/callback в popup, и наш RedirectResponse
    # просто рендерил дашборд внутри popup — а родительское окно оставалось
    # на /login. Вместо редиректа отдаём HTML с JS-обработкой.
    cookie_val = _sign_session(uid)
    # Сохраняем профиль админа (имя/аватарка/юзернейм) для Discord-хаба
    try:
        await storage.record_tg_user_info(
            user_id=uid,
            username=data.get("username", "") or "",
            first_name=data.get("first_name", "") or "",
            last_name=data.get("last_name", "") or "",
            photo_url=data.get("photo_url", "") or "",
        )
    except Exception as e:
        logger.warning("record_tg_user_info failed: %s", e)
    success_html = """<!DOCTYPE html><html><head>
<meta charset='UTF-8'><title>JARVIS · auth ok</title>
<style>
  body {
    margin: 0; min-height: 100vh; background: #050818;
    color: #00e5ff; font-family: "JetBrains Mono", monospace;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    text-align: center;
  }
  .logo {
    font-size: 32px; font-weight: 900;
    letter-spacing: 8px;
    text-shadow: 0 0 16px #00e5ff;
    margin-bottom: 14px;
  }
  .ok { font-size: 13px; letter-spacing: 2px; color: #4ade80; }
  .hint { font-size: 11px; color: #5b7299; margin-top: 20px; }
</style>
</head><body>
<div class="logo">JARVIS</div>
<div class="ok">✓ AUTHORIZED</div>
<div class="hint">Это окно закроется автоматически…</div>
<script>
(function() {
  try {
    if (window.opener && !window.opener.closed) {
      window.opener.location.href = '/';
      window.close();
      return;
    }
  } catch (e) {}
  // Не popup или opener закрыт — просто редиректим текущее окно
  setTimeout(function() { location.href = '/'; }, 700);
})();
</script>
</body></html>"""
    resp = HTMLResponse(success_html)
    resp.set_cookie(
        SESSION_COOKIE,
        cookie_val,
        max_age=SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    try:
        event_bus.emit_event(
            "dashboard-login",
            {"user_id": uid, "username": data.get("username", "")},
            severity="info",
        )
    except Exception:
        pass
    return resp


@app.get("/logout")
async def logout():
    """Удалить session cookie."""
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", response_class=HTMLResponse)
async def root(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    # В strict-режиме редиректим на /login если не авторизован
    if _is_strict_telegram() and _try_session_auth(request) is None:
        return RedirectResponse(url="/login", status_code=302)
    _check_auth(request, credentials)
    # UA-детект: телефон → мобильный дашборд
    if _is_mobile_ua(request) and _JARVIS_MOBILE_PATH.exists():
        return HTMLResponse(
            _JARVIS_MOBILE_PATH.read_text(encoding="utf-8"),
            headers=_NO_CACHE_HEADERS,
        )
    return HTMLResponse(_load_html(DASHBOARD_DEFAULT), headers=_NO_CACHE_HEADERS)


@app.get("/mobile", response_class=HTMLResponse)
async def mobile_root(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    """Явный /mobile эндпоинт для тестирования мобильного дашборда с десктопа."""
    if _is_strict_telegram() and _try_session_auth(request) is None:
        return RedirectResponse(url="/login", status_code=302)
    _check_auth(request, credentials)
    if _JARVIS_MOBILE_PATH.exists():
        return HTMLResponse(_JARVIS_MOBILE_PATH.read_text(encoding="utf-8"))
    raise HTTPException(404, "mobile dashboard not found")


@app.get("/classic", response_class=HTMLResponse)
async def classic_dashboard(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    if _is_strict_telegram() and _try_session_auth(request) is None:
        return RedirectResponse(url="/login", status_code=302)
    _check_auth(request, credentials)
    return HTMLResponse(_load_html("index"))


@app.get("/jarvis", response_class=HTMLResponse)
async def jarvis_dashboard(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    if _is_strict_telegram() and _try_session_auth(request) is None:
        return RedirectResponse(url="/login", status_code=302)
    _check_auth(request, credentials)
    return HTMLResponse(_load_html("jarvis"))


# === Desktop app download page ===

_DESKTOP_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>PRIDE J.A.R.V.I.S. Desktop</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    background: radial-gradient(ellipse at top, #0a1a2c 0%, #050810 80%);
    color: #e0e8f0;
    display: flex; align-items: center; justify-content: center;
    padding: 40px 20px;
  }
  .card {
    max-width: 540px; width: 100%;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(0,229,255,0.2);
    border-radius: 16px;
    padding: 40px;
    box-shadow: 0 0 60px rgba(0,229,255,0.1);
  }
  h1 {
    margin: 0 0 8px 0; font-size: 26px; letter-spacing: 1px;
    color: #00e5ff;
  }
  .sub { color: #8898a8; font-size: 13px; margin-bottom: 24px; }
  .platforms {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
    margin-bottom: 20px;
  }
  .platform {
    padding: 16px;
    background: rgba(0,229,255,0.05);
    border: 1px solid rgba(0,229,255,0.2);
    border-radius: 10px;
    text-decoration: none; color: inherit;
    display: flex; flex-direction: column;
    transition: all 0.2s;
  }
  .platform:hover {
    background: rgba(0,229,255,0.1);
    border-color: #00e5ff;
    transform: translateY(-2px);
  }
  .platform .icon { font-size: 32px; margin-bottom: 8px; }
  .platform .name { font-weight: 700; font-size: 14px; }
  .platform .size { font-size: 11px; color: #8898a8; margin-top: 4px; }
  .platform.recommended {
    border-color: #00e5ff;
    background: rgba(0,229,255,0.12);
    grid-column: 1 / -1;
  }
  .platform.recommended .icon { font-size: 40px; }
  .features {
    list-style: none; padding: 0; margin: 0 0 20px 0;
    color: #aab;
    font-size: 13px;
  }
  .features li { padding: 4px 0; }
  .features li::before { content: "✓ "; color: #00e5ff; }
  .footer {
    margin-top: 24px; padding-top: 20px;
    border-top: 1px solid rgba(255,255,255,0.05);
    font-size: 11px; color: #56657a; text-align: center;
  }
  .footer a { color: #5aa0c8; }
  .version { font-family: monospace; }
  .loading { color: #aaa; font-size: 12px; }
</style>
</head>
<body>
<div class="card">
  <h1>🦁 PRIDE J.A.R.V.I.S.</h1>
  <div class="sub">Desktop клиент · <span id="ver" class="version loading">загружаю версию...</span></div>

  <ul class="features">
    <li>Native окно без браузерной строки</li>
    <li>Иконка в трее с быстрым доступом</li>
    <li>Native push-уведомления о новых клиентах / выплатах / блоках</li>
    <li>Hotkey Ctrl+Shift+J — показать/скрыть</li>
    <li>Авто-обновления через GitHub Releases</li>
  </ul>

  <div class="platforms" id="platforms">
    <div class="loading" style="grid-column: 1/-1; text-align: center; padding: 30px;">⏳ Получаю ссылки с GitHub Releases...</div>
  </div>

  <div class="footer">
    Сборки на <a href="https://github.com/simba-stack/workchat-bot/releases/latest" target="_blank">GitHub Releases</a> ·
    Auto-update встроен · v1.0.0+
  </div>
</div>

<script>
  // Подгружаем манифест через НАШ proxy (а не напрямую GitHub — обходит CORS,
  // и кнопки скачивания качают через наш домен).
  async function fetchManifest(refresh) {
    const url = "/api/desktop/manifest" + (refresh ? "?refresh=1" : "");
    const r = await fetch(url, { cache: "no-store" });
    return r.json();
  }
  // Сначала обычный запрос. Если ассеты пустые (GitHub ещё не успел залить) —
  // повторяем с refresh=1 чтобы пробить серверный кеш.
  fetchManifest(false)
    .then(async data => {
      if (data && data.ok && (!data.assets || data.assets.length === 0)) {
        await new Promise(r => setTimeout(r, 800));
        const d2 = await fetchManifest(true);
        if (d2 && d2.assets && d2.assets.length > 0) return d2;
      }
      return data;
    })
    .then(data => {
      const verEl = document.getElementById("ver");
      const platforms = document.getElementById("platforms");
      if (!data.ok) {
        verEl.textContent = "релизов пока нет";
        verEl.classList.remove("loading");
        platforms.innerHTML =
          '<div class="loading" style="grid-column:1/-1;text-align:center;padding:30px;">' +
          '⚠️ Сборок пока нет (Actions ещё не выкатил релиз).<br>' +
          'Подожди 5 минут после git push --tags' +
          '</div>';
        return;
      }
      verEl.textContent = "версия " + data.version;
      verEl.classList.remove("loading");
      platforms.innerHTML = "";

      // Определяем текущую ОС
      const ua = navigator.userAgent.toLowerCase();
      const isWin = ua.includes("win") && !ua.includes("mac");
      const isMac = ua.includes("mac");
      const isLinux = ua.includes("linux") && !ua.includes("android");

      // Сортируем: сначала рекомендуемая, потом остальные
      const order = isWin ? ["win","mac","linux","zip"]
                  : isMac ? ["mac","win","linux","zip"]
                  :         ["linux","win","mac","zip"];
      const sorted = order
        .map(p => data.assets.find(a => a.platform === p))
        .filter(Boolean);
      sorted.forEach((asset, idx) => {
        const recommended = idx === 0;
        const labels = {win: "Windows (.exe)", mac: "macOS (.dmg)", linux: "Linux (.deb)", zip: "Архив (.zip)"};
        const card = document.createElement("a");
        card.className = "platform" + (recommended ? " recommended" : "");
        card.href = asset.url; // наш /desktop/download/{platform}
        card.setAttribute("download", asset.name);
        card.innerHTML = `
          <span class="icon">${asset.icon}</span>
          <span class="name">${recommended ? "⬇️ " : ""}${labels[asset.platform] || asset.platform}</span>
          <span class="size">${asset.size_mb} MB · ${asset.name}</span>
        `;
        platforms.appendChild(card);
      });
      if (sorted.length === 0) {
        platforms.innerHTML = '<div class="loading" style="grid-column:1/-1;text-align:center;padding:30px;">Сборок нет</div>';
      }
    })
    .catch(err => {
      document.getElementById("ver").textContent = "ошибка загрузки";
      document.getElementById("ver").classList.remove("loading");
      document.getElementById("platforms").innerHTML =
        '<div class="loading" style="grid-column:1/-1;text-align:center;padding:30px;">' +
        '⚠️ ' + err + '</div>';
    });
</script>
</body>
</html>"""


@app.get("/desktop", response_class=HTMLResponse)
async def desktop_download_page(request: Request):
    """Страница скачивания desktop-приложения. Тянет последний релиз с GitHub
    через server-side proxy (без CORS) и стримит файлы через свой домен."""
    return HTMLResponse(_DESKTOP_PAGE_HTML)


# Кеш манифеста релиза (5 минут) чтобы не дёргать GitHub каждый клик
_DESKTOP_MANIFEST_CACHE = {"ts": 0, "data": None}


@app.get("/api/desktop/manifest")
async def api_desktop_manifest(refresh: int = 0):
    """Сервер-сайд получение последнего релиза + перевод download URL'ов
    на наш домен для проксирования."""
    import time as _t
    now = _t.time()
    cached = _DESKTOP_MANIFEST_CACHE["data"]
    cached_assets = (cached or {}).get("assets") or []
    # Используем кеш только если он непустой (assets есть) И клиент не запросил refresh.
    # Пустой ответ кешируем максимум 30 сек чтобы не долбить GitHub, но всё равно перепроверяем.
    age = now - _DESKTOP_MANIFEST_CACHE["ts"]
    if not refresh and cached:
        if cached_assets and age < 300:
            return cached
        if not cached_assets and age < 30:
            return cached
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.github.com/repos/simba-stack/workchat-bot/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"github {r.status_code}"}
            release = r.json()
    except Exception as e:
        logger.warning("desktop manifest fetch failed: %s", e)
        return {"ok": False, "error": str(e)}

    assets_out = []
    for a in release.get("assets", []) or []:
        name = a.get("name") or ""
        low = name.lower()
        platform = None
        icon = None
        if low.endswith(".exe"):
            platform, icon = "win", "🪟"
        elif low.endswith(".dmg"):
            platform, icon = "mac", "🍎"
        elif low.endswith(".deb"):
            platform, icon = "linux", "🐧"
        elif low.endswith(".zip"):
            platform, icon = "zip", "📦"
        if not platform:
            continue
        assets_out.append({
            "name": name,
            "platform": platform,
            "icon": icon,
            "size_mb": round((a.get("size") or 0) / 1e6, 1),
            "url": f"/desktop/download/{platform}",  # наш прокси
            "github_url": a.get("browser_download_url"),
        })
    data = {
        "ok": True,
        "version": release.get("tag_name") or "?",
        "name": release.get("name") or release.get("tag_name") or "?",
        "published_at": release.get("published_at"),
        "assets": assets_out,
    }
    _DESKTOP_MANIFEST_CACHE["ts"] = now
    _DESKTOP_MANIFEST_CACHE["data"] = data
    return data


@app.get("/desktop/download/{platform}")
async def desktop_download_proxy(platform: str):
    """Скачивание сборки через наш домен. Стримит файл с GitHub Releases
    чтобы пользователь видел загрузку с workchat-bot-production.up.railway.app,
    а не с github.com."""
    manifest = await api_desktop_manifest()
    if not manifest.get("ok"):
        raise HTTPException(503, f"manifest fetch failed: {manifest.get('error')}")
    asset = next(
        (a for a in manifest.get("assets", []) if a.get("platform") == platform),
        None,
    )
    if not asset:
        raise HTTPException(404, f"no asset for platform={platform}")
    github_url = asset.get("github_url")
    fname = asset.get("name") or f"pride-jarvis-{platform}.bin"

    import httpx

    async def file_stream():
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", github_url) as resp:
                if resp.status_code != 200:
                    yield b""
                    return
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk

    return StreamingResponse(
        file_stream(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Source": "github-releases-proxy",
        },
    )


# === API endpoints (READ-ONLY) ===

def _slim_card(cid: str, c: dict) -> dict:
    """Урезанная версия карточки для API — без history/raw полей."""
    return {
        "card_id": cid,
        "supplier": c.get("supplier"),
        "bank": c.get("bank"),
        "fio": c.get("fio"),
        "price_usdt": c.get("price_usdt"),
        "payment_method": c.get("payment_method"),
        "status": c.get("status"),
        "deal_id": c.get("deal_id"),
        "usdt_address": c.get("usdt_address"),
        "client_username": c.get("client_username"),
        "work_chat_id": c.get("work_chat_id"),
        "block_amount_rub": c.get("block_amount_rub"),
        "block_note": c.get("block_note"),
        "brak_reason": c.get("brak_reason"),
        "created_at": c.get("created_at"),
        "created_by": (
            (c.get("history") or [{}])[0].get("by")
            if c.get("history") else None
        ),
    }


@app.get("/api/state")
async def api_state(_: None = Depends(_auth)):
    """Снимок для первичного рендера. Лёгкий — без полных списков."""
    storage.reload_sync()  # подтянуть свежие данные от userbot.py
    cards = storage.list_lk_cards() or {}
    managed = storage.state.get("managed_chats") or {}
    deals = storage.list_deals() or {}

    cards_by_status: dict = {}
    cards_by_method: dict = {}
    for c in cards.values():
        st = c.get("status") or "—"
        cards_by_status[st] = cards_by_status.get(st, 0) + 1
        m = c.get("payment_method") or "—"
        cards_by_method[m] = cards_by_method.get(m, 0) + 1

    today = datetime.now()
    margin_today = 0.0
    margin_week = 0.0
    apps_recent: list = []
    for i in range(7):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        apps = storage.get_applications_v2(d) or []
        day_margin = 0.0
        for a in apps:
            m_v = float(a.get("computed", {}).get("margin_usdt", 0) or 0)
            day_margin += m_v
            apps_recent.append({
                "date": d,
                "id": a.get("id"),
                "margin_usdt": m_v,
                "intake_bank": (a.get("intake") or {}).get("bank"),
                "intake_fio": (a.get("intake") or {}).get("fio"),
                "outputs_count": len(a.get("outputs") or []),
            })
        margin_week += day_margin
        if i == 0:
            margin_today = day_margin

    # Воронка — сегодня + 7 дней суммарно
    funnel_today = storage.get_funnel()
    funnel_week_rows = storage.get_funnel_range(7)
    funnel_week_total: dict = {}
    for row in funnel_week_rows:
        for k, v in row.items():
            if k == "date" or not isinstance(v, (int, float)):
                continue
            funnel_week_total[k] = funnel_week_total.get(k, 0) + float(v)

    # БЛОК — карточки в статусе БЛОК
    blocks_count = sum(
        1 for c in cards.values()
        if (c.get("status") or "").upper() in ("БЛОК", "БЛОК_БЕЗ_ОТРАБОТКИ")
    )
    blocks_nowork_count = sum(
        1 for c in cards.values()
        if (c.get("status") or "").upper() == "БЛОК_БЕЗ_ОТРАБОТКИ"
    )
    # ЛК отработанных (статус ОТРАБОТАН или ЗАВЕРШЁН)
    lk_done_count = sum(
        1 for c in cards.values()
        if (c.get("status") or "").upper() in ("ОТРАБОТАН", "ЗАВЕРШЁН", "ЗАВЕРШЕН")
    )
    lk_in_work = sum(
        1 for c in cards.values() if (c.get("status") or "").upper() == "В_РАБОТЕ"
    )

    return {
        "stats": {
            "lk_cards_total": len(cards),
            "lk_cards_by_status": cards_by_status,
            "lk_cards_by_method": cards_by_method,
            "lk_in_work": lk_in_work,
            "lk_done": lk_done_count,
            "lk_blocks": blocks_count,
            "lk_blocks_nowork": blocks_nowork_count,
            "managed_chats_active": len(managed),
            "deals_total": len(deals),
            "margin_today_usdt": margin_today,
            "margin_week_usdt": margin_week,
            "ai": dict(storage.state.get("ai_stats", {}) or {}),
            "escalate": dict(storage.state.get("escalate_stats", {}) or {}),
            "writeback": dict(storage.state.get("writeback_stats", {}) or {}),
            "deals_stats": dict(storage.state.get("deals_stats", {}) or {}),
            "ai_enabled": storage.is_ai_enabled(),
            "writeback_enabled": storage.is_writeback_enabled(),
            "ai_model": storage.state.get("ai_model") or "",
            "subscribers": event_bus.subscriber_count(),
            "funnel_today": funnel_today,
            "funnel_week_total": funnel_week_total,
        },
        "recent_applications": sorted(
            apps_recent, key=lambda x: (x["date"], x["id"]), reverse=True
        )[:20],
        "server_ts": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/lk_cards")
async def api_lk_cards(
    status_filter: Optional[str] = None,
    method: Optional[str] = None,
    bank: Optional[str] = None,
    supplier: Optional[str] = None,
    limit: int = 200,
    _: None = Depends(_auth),
):
    """Список карточек ЛК. Фильтры: status_filter, method, bank, supplier."""
    storage.reload_sync()
    cards = storage.list_lk_cards() or {}
    result = []
    for cid, c in cards.items():
        if status_filter and (c.get("status") or "") != status_filter:
            continue
        if method and (c.get("payment_method") or "") != method:
            continue
        if bank and bank.lower() not in (c.get("bank") or "").lower():
            continue
        if supplier and supplier.lower().lstrip("@") not in (
            c.get("supplier") or ""
        ).lower().lstrip("@"):
            continue
        result.append(_slim_card(cid, c))
    result.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return {"cards": result[:limit], "total": len(result)}


# ============================================================================
# Разделы ЛК (май 2026): 4 sub-tabs для дашборда
# ============================================================================

def _filter_lk_cards(predicate):
    storage.reload_sync()
    cards = storage.list_lk_cards() or {}
    out = []
    for cid, c in cards.items():
        if not c:
            continue
        try:
            if predicate(c):
                out.append(_slim_card(cid, c))
        except Exception:
            continue
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return out


@app.get("/api/lk/all")
async def api_lk_all(_: None = Depends(_auth)):
    out = _filter_lk_cards(lambda c: True)
    return {"cards": out, "total": len(out)}


@app.get("/api/lk/to_top_up")
async def api_lk_to_top_up(_: None = Depends(_auth)):
    def pred(c):
        m = (c.get("payment_method") or "").upper()
        s = (c.get("status") or "В_РАБОТЕ").upper()
        return m in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER") and s in ("В_РАБОТЕ", "ОТРАБОТАН")
    out = _filter_lk_cards(pred)
    return {"cards": out, "total": len(out)}


@app.get("/api/lk/to_pay")
async def api_lk_to_pay(_: None = Depends(_auth)):
    def pred(c):
        m = (c.get("payment_method") or "").upper()
        s = (c.get("status") or "В_РАБОТЕ").upper()
        return m == "USDT_TRC20" and s == "ОТРАБОТАН"
    out = _filter_lk_cards(pred)
    return {"cards": out, "total": len(out)}


@app.get("/api/lk/to_release")
async def api_lk_to_release(_: None = Depends(_auth)):
    def pred(c):
        m = (c.get("payment_method") or "").upper()
        s = (c.get("status") or "В_РАБОТЕ").upper()
        return m == "GUARANTOR_AFTER_WORK" and s in ("ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ")
    out = _filter_lk_cards(pred)
    return {"cards": out, "total": len(out)}


@app.get("/api/lk/summary")
async def api_lk_summary(_: None = Depends(_auth)):
    storage.reload_sync()
    cards = storage.list_lk_cards() or {}
    cats = {
        "all": {"count": 0, "sum": 0.0},
        "to_top_up": {"count": 0, "sum": 0.0},
        "to_pay": {"count": 0, "sum": 0.0},
        "to_release": {"count": 0, "sum": 0.0},
    }
    for c in cards.values():
        if not c: continue
        m = (c.get("payment_method") or "").upper()
        s = (c.get("status") or "В_РАБОТЕ").upper()
        price = float(c.get("price_usdt") or 0)
        cats["all"]["count"] += 1; cats["all"]["sum"] += price
        if m in ("GUARANTOR_BEFORE","GUARANTOR_AFTER") and s in ("В_РАБОТЕ","ОТРАБОТАН"):
            cats["to_top_up"]["count"] += 1; cats["to_top_up"]["sum"] += price
        if m == "USDT_TRC20" and s == "ОТРАБОТАН":
            cats["to_pay"]["count"] += 1; cats["to_pay"]["sum"] += price
        if m == "GUARANTOR_AFTER_WORK" and s in ("ОТРАБОТАН","ПОПОЛНИТЬ_И_ОТПУСТИТЬ"):
            cats["to_release"]["count"] += 1; cats["to_release"]["sum"] += price
    return cats


# ============================================================================
# OPERATIONAL (Операционная — список ЛК в работе + фикс заявок)
# ============================================================================

@app.get("/api/operational/lk_in_work")
async def api_op_lk_in_work(_: None = Depends(_auth)):
    storage.reload_sync()
    rows = storage.list_lk_in_work()
    singles = [r for r in rows if not r.get("is_combo")]
    combos = [r for r in rows if r.get("is_combo")]
    grouped = {}
    for r in combos:
        key = (r.get("fio"), r.get("supplier"))
        grouped.setdefault(key, []).append(r)
    return {
        "singles": singles,
        "combos": [
            {"fio": k[0], "supplier": k[1], "size": len(items), "items": items}
            for k, items in grouped.items()
        ],
    }


class ExchangeRequestPayload(BaseModel):
    bank_in: str
    fio_in: str
    lk_card_id_in: Optional[str] = ""
    amount_in: float
    lk_in_price_usdt: float = 0
    lk_in_status: Optional[str] = "ОТРАБОТАН"
    outs: list  # [{bank, fio, lk_card_id, amount_out, lk_out_price_usdt,
                #   op_status (IN_WORK_RELEASE|IN_WORK_HOLD|DONE|BLOCK),
                #   block_amount_rub, block_note,
                #   jur_jur_receivers: [{bank, fio, lk_card_id, lk_price_usdt, op_status}]}]
    partner_pct: float = 0
    exchange_rate: float = 0
    commission_usdt: float = 0   # % комиссии откупа
    # === Новые поля (отчётность v2) ===
    report_date: Optional[str] = ""   # YYYY-MM-DD
    report_type: Optional[str] = ""   # из storage.operational_report_types
    payout_rate_partner: float = 0    # курс выплаты партнёру (отдельно от exchange_rate)
    losses: list = []                  # [{amount_rub, reason}]
    commissions: list = []             # [{amount_rub, where}]
    remains: list = []                 # [{amount_rub, where}]


@app.post("/api/operational/exchange_request")
async def api_op_create_exchange_request(
    payload: ExchangeRequestPayload,
    me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("exchange_request")),
):
    if me.get("role") not in ("owner", "manager", "operationist"):
        raise HTTPException(status_code=403, detail="forbidden")
    storage.reload_sync()
    rate = float(payload.exchange_rate or 0)
    if rate <= 0:
        raise HTTPException(status_code=400, detail="exchange_rate required")
    payout_rate_partner = float(payload.payout_rate_partner or 0) or rate  # fallback на rate
    total_in_rub = float(payload.amount_in or 0)
    partner_pct = float(payload.partner_pct or 0)
    comm_pct = float(payload.commission_usdt or 0)  # % комиссии откупа

    # === НОВАЯ формула (июнь 2026 v3, на основе ответов SIMBA) ===
    # 1) Выплата партнёру USDT = (amount_in / payout_rate_partner) × (1 − partner_pct/100)
    partner_payout_usdt = (
        (total_in_rub / payout_rate_partner) * (1.0 - partner_pct / 100.0)
        if payout_rate_partner > 0 else 0.0
    )
    # 2) Маржа: суммируем выходы только по ЛК БЕЗ юр-юр (чтобы не дублировать)
    out_sum_no_jurjur = 0.0
    total_out_rub = 0.0
    for o in (payload.outs or []):
        amt = float(o.get("amount_out") or 0)
        total_out_rub += amt
        if not (o.get("jur_jur_receivers") or []):
            out_sum_no_jurjur += amt
    margin_gross = (out_sum_no_jurjur / rate) * (1.0 - comm_pct / 100.0) if rate > 0 else 0.0
    # Цены ЛК — каждый ЛК один раз
    lk_prices = float(payload.lk_in_price_usdt or 0)
    for o in (payload.outs or []):
        lk_prices += float(o.get("lk_out_price_usdt") or 0)
        for jj in (o.get("jur_jur_receivers") or []):
            lk_prices += float(jj.get("lk_price_usdt") or 0)
    # Дополнительные модификаторы
    losses_total = sum(float(x.get("amount_rub") or 0) for x in (payload.losses or [])) / (rate or 1.0)
    commissions_total = sum(float(x.get("amount_rub") or 0) for x in (payload.commissions or [])) / (rate or 1.0)
    remains_total = sum(float(x.get("amount_rub") or 0) for x in (payload.remains or [])) / (rate or 1.0)
    margin_usdt = margin_gross - lk_prices - losses_total - commissions_total + remains_total

    # === Маппинг op_status → старый status_after для совместимости ===
    OP_STATUS_MAP = {
        "IN_WORK_RELEASE": "ОТРАБОТАН",  # в работе но можно отпустить → отпускаем
        "DONE":            "ОТРАБОТАН",
        "IN_WORK_HOLD":    "В_РАБОТЕ",   # держим — оплата НЕ идёт
        "BLOCK":           "БЛОК",
    }
    # ЛК которые «отпускаем оплату» — только эти попадают в available на баланс
    RELEASE_OP_STATUSES = {"IN_WORK_RELEASE", "DONE"}

    # Собираем ЛК и их target-статусы + op_status
    involved_lk = []  # [(card_id, status_after, op_status, block_amount, block_note)]
    if payload.lk_card_id_in:
        involved_lk.append((
            payload.lk_card_id_in,
            payload.lk_in_status or "ОТРАБОТАН",
            "DONE", 0.0, "",
        ))
    for o in (payload.outs or []):
        cid = o.get("lk_card_id")
        op_st = (o.get("op_status") or "DONE").upper()
        mapped = OP_STATUS_MAP.get(op_st, o.get("status_after") or "ОТРАБОТАН")
        block_amt = float(o.get("block_amount_rub") or 0)
        block_note = (o.get("block_note") or "").strip()
        if cid:
            involved_lk.append((cid, mapped, op_st, block_amt, block_note))
        for jj in (o.get("jur_jur_receivers") or []):
            jj_cid = jj.get("lk_card_id")
            jj_op = (jj.get("op_status") or "DONE").upper()
            jj_mapped = OP_STATUS_MAP.get(jj_op, jj.get("status_after") or "ОТРАБОТАН")
            if jj_cid:
                involved_lk.append((jj_cid, jj_mapped, jj_op, 0.0, ""))

    req_id = await storage.add_exchange_request(
        bank_in=payload.bank_in,
        fio_in=payload.fio_in,
        lk_card_id_in=payload.lk_card_id_in or "",
        amount_in=payload.amount_in,
        outs=payload.outs,
        partner_pct=partner_pct,
        exchange_rate=rate,
        commission_usdt=comm_pct,
        margin_usdt=margin_usdt,
        total_in_rub=total_in_rub,
        total_out_rub=total_out_rub,
        status="ЗАФИКСИРОВАНА",
        created_by=me.get("username") or str(me.get("tg_id")) or "operationist",
        involved_lk_cards=[cid for cid, *_ in involved_lk],
        # Новые поля v3:
        report_date=payload.report_date or "",
        report_type=payload.report_type or "",
        payout_rate_partner=payout_rate_partner,
        losses=list(payload.losses or []),
        commissions=list(payload.commissions or []),
        remains=list(payload.remains or []),
        partner_payout_usdt=partner_payout_usdt,
    )

    # === Начисление на баланс поставщика (новая схема балансов) ===
    # Распределяем partner_payout_usdt пропорционально цене каждого ЛК для вывода
    # (или равномерно если цены не заданы). Только для ЛК с op_status в RELEASE_OP_STATUSES.
    release_lks = [
        (cid, op_st) for cid, _, op_st, _, _ in involved_lk
        if op_st in RELEASE_OP_STATUSES
    ]
    if release_lks and partner_payout_usdt > 0:
        # Группируем по supplier (= owner_id)
        per_supplier = {}  # {owner_id_or_username: total_share}
        n = len(release_lks)
        share_per_lk = partner_payout_usdt / n
        for cid, _ in release_lks:
            card = storage.get_lk_card(cid) if hasattr(storage, "get_lk_card") else None
            if not card:
                continue
            sup = (card.get("supplier") or "").lstrip("@").strip().lower()
            if not sup:
                continue
            # Ищем owner_id по username (сначала через crm_owners)
            owner_id = ""
            for oid, o in (storage.state.get("crm_owners") or {}).items():
                if (o.get("username") or "").lstrip("@").lower().strip() == sup:
                    owner_id = oid
                    break
            user_key = (
                storage._balance_key_owner(owner_id)
                if owner_id else storage._balance_key_worker(sup)
            )
            per_supplier[user_key] = per_supplier.get(user_key, 0.0) + share_per_lk
        for ukey, amt in per_supplier.items():
            try:
                await storage.accrue_to_balance(
                    ukey, amt,
                    tx_type="lk_payout", ref=req_id,
                    note=f"Exchange {req_id} · {len(release_lks)} ЛК отпущено",
                    status="available",  # отпущено → сразу available
                )
            except Exception as e:
                logger.warning("[balance accrue] %s amt=%.2f failed: %s", ukey, amt, e)

    notified_chats = set()
    for cid, status_after, op_st, block_amt, block_note in involved_lk:
        try:
            update_fields = {"status": status_after}
            # БЛОК — пишем сумму и заметку, ЛК автоматически попадает в раздел БЛОКИ
            if op_st == "BLOCK":
                update_fields["block_amount_rub"] = block_amt
                update_fields["block_note"] = block_note
            await storage.update_lk_card(cid, **update_fields)
        except Exception:
            pass
        # Уведомляем клиента ТОЛЬКО если ЛК стал ОТРАБОТАН (для В_РАБОТЕ — не дёргаем).
        if status_after != "ОТРАБОТАН":
            continue
        try:
            card = storage.get_lk_card(cid) if hasattr(storage, "get_lk_card") else None
            if card:
                wc = int(card.get("work_chat_id") or 0)
                if wc and wc not in notified_chats:
                    method = (card.get("payment_method") or "").upper()
                    if method == "USDT_TRC20":
                        action = "💸 USDT TRC20 будет переведён в ближайшее время"
                    elif method == "GUARANTOR_AFTER_WORK":
                        action = "💼 Пополним сделку в Конте и отпустим вам"
                    elif method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER"):
                        action = "💼 Сделка в Конте будет пополнена"
                    else:
                        action = "💰 Оплата произведена согласно методу"
                    bank = (card.get("bank") or "—").upper()
                    fio = card.get("fio") or "—"
                    notify_text = (
                        f"✅ <b>ЛК ОТРАБОТАН</b>\n\n"
                        f"🏦 <b>{bank}</b> / {fio}\n"
                        f"{action}.\n\n"
                        f"<i>Спасибо за работу!</i>"
                    )
                    await storage.enqueue_dashboard_command(
                        f"__notify_client_otrabotan {wc} {cid}"
                    )
                    notified_chats.add(wc)
        except Exception as e:
            logger.warning("notify ОТРАБОТАН for card=%s failed: %s", cid, e)

    # we_received: что мы получили на руки в USDT (до вычета цены ЛК).
    # Используется для accounting записи. Это margin_gross без вычета комиссии откупа.
    we_received_usdt = (out_sum_no_jurjur / rate) if rate > 0 else 0.0

    try:
        await storage.add_accounting_entry(
            category="kassa",
            amount_usdt=margin_usdt,
            amount_rub=total_in_rub,
            note=f"Exchange request {req_id} (маржа: {margin_usdt:.2f}$, выплата партнёру: {partner_payout_usdt:.2f}$)",
            created_by=me.get("username") or "",
            ref_id=req_id,
        )
    except Exception:
        pass

    # Эмитнуть event для дашборда
    try:
        from event_bus import emit_event
        emit_event("exchange-request-created", {
            "req_id": req_id,
            "margin_usdt": margin_usdt,
            "partner_payout_usdt": partner_payout_usdt,
            "involved_lk_count": len(involved_lk),
        }, character="lk", severity="success")
    except Exception:
        pass

    return {
        "req_id": req_id,
        "margin_usdt": margin_usdt,
        "partner_payout_usdt": partner_payout_usdt,
        "we_received_usdt": we_received_usdt,
        "lk_prices_usdt": lk_prices,
        "losses_usdt": losses_total,
        "commissions_usdt": commissions_total,
        "remains_usdt": remains_total,
        "involved_lk_cards": [t[0] for t in involved_lk],
    }


@app.get("/api/operational/exchange_requests")
async def api_op_list_exchange_requests(
    status: Optional[str] = None, _: None = Depends(_auth),
):
    storage.reload_sync()
    reqs = storage.list_exchange_requests(status=status)
    out = [{**r, "req_id": rid} for rid, r in reqs.items()]
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return {"requests": out, "total": len(out)}


@app.get("/api/operational/search_lk")
async def api_op_search_lk(q: str = "", _: None = Depends(_auth)):
    if not q or len(q) < 2:
        return {"results": []}
    storage.reload_sync()
    qlow = q.lower().strip()
    out = []
    for cid, c in (storage.list_lk_cards() or {}).items():
        if not c: continue
        if (c.get("status") or "В_РАБОТЕ") not in ("В_РАБОТЕ","ОТРАБОТАН","ПОПОЛНИТЬ_И_ОТПУСТИТЬ"): continue
        if qlow in (c.get("fio") or "").lower():
            out.append({
                "card_id": cid,
                "bank": (c.get("bank") or "").upper(),
                "fio": c.get("fio") or "",
                "supplier": (c.get("supplier") or "").lstrip("@"),
                "status": c.get("status") or "",
                "price_usdt": float(c.get("price_usdt") or 0),
            })
            if len(out) >= 20:
                break
    return {"results": out}


def _resolve_dedik_creds_for_card(card_id: str) -> dict:
    """Возвращает {ip, login, password, location} для дедика, привязанного
    к этой lk_card. Ищем через crm_drop_lks по bank+fio через drop'\''ы owner'а."""
    card = storage.get_lk_card(card_id) if hasattr(storage, "get_lk_card") else None
    if not card:
        return {}
    bank = (card.get("bank") or "").upper()
    fio = (card.get("fio") or "").strip().lower()
    supplier = (card.get("supplier") or "").lstrip("@").lower()
    # 1) если в карточке уже сохранён droplk_id — используем напрямую
    droplk_id = card.get("droplk_id") or ""
    drop_lks = storage.state.get("crm_drop_lks") or {}
    drops = storage.state.get("crm_drops") or {}
    if droplk_id and droplk_id in drop_lks:
        lk = drop_lks[droplk_id]
        return {
            "ip": lk.get("ded_ip") or "",
            "login": lk.get("ded_login") or "Administrator",
            "password": lk.get("ded_pass") or "",
            "location": lk.get("ded_location") or "",
        }
    # 2) Иначе ищем по bank + fio (через drop_id → fio в drops)
    for lkid, lk in drop_lks.items():
        if (lk.get("bank") or "").upper() != bank:
            continue
        drop = drops.get(lk.get("drop_id") or "")
        if not drop:
            continue
        d_fio = (drop.get("fio") or "").strip().lower()
        if d_fio and d_fio == fio:
            return {
                "ip": lk.get("ded_ip") or "",
                "login": lk.get("ded_login") or "Administrator",
                "password": lk.get("ded_pass") or "",
                "location": lk.get("ded_location") or "",
            }
    return {}


@app.get("/api/operational/dedik_rdp/{card_id}")
async def api_op_dedik_rdp(card_id: str, me: dict = Depends(_get_me)):
    """Генерирует .rdp файл для нативного RDP-клиента Windows.
    Клиент кликает кнопку «Зайти на дедик» → скачивается .rdp →
    Windows автоматически открывает mstsc.exe с pre-filled креденшелами."""
    if me.get("role") not in ("owner", "manager", "operationist", "system"):
        raise HTTPException(status_code=403, detail="forbidden")
    storage.reload_sync()
    creds = _resolve_dedik_creds_for_card(card_id)
    if not creds.get("ip"):
        raise HTTPException(
            status_code=404,
            detail="дедик IP не найден для этой ЛК — проверь crm_passwords/CRM",
        )
    ip = creds["ip"].strip()
    login = (creds.get("login") or "Administrator").strip()
    # .rdp файл — это plain text с CRLF переносами в правильном формате
    rdp_lines = [
        "full address:s:" + ip,
        "username:s:" + login,
        "screen mode id:i:2",          # full screen
        "use multimon:i:0",
        "desktopwidth:i:1920",
        "desktopheight:i:1080",
        "session bpp:i:32",
        "compression:i:1",
        "keyboardhook:i:2",
        "audiocapturemode:i:0",
        "videoplaybackmode:i:1",
        "connection type:i:7",
        "networkautodetect:i:1",
        "bandwidthautodetect:i:1",
        "displayconnectionbar:i:1",
        "enableworkspacereconnect:i:0",
        "disable wallpaper:i:0",
        "allow font smoothing:i:0",
        "allow desktop composition:i:0",
        "disable full window drag:i:1",
        "disable menu anims:i:1",
        "disable themes:i:0",
        "disable cursor setting:i:0",
        "bitmapcachepersistenable:i:1",
        "audiomode:i:0",
        "redirectprinters:i:0",
        "redirectcomports:i:0",
        "redirectsmartcards:i:1",
        "redirectclipboard:i:1",
        "redirectposdevices:i:0",
        "autoreconnection enabled:i:1",
        "authentication level:i:2",
        "prompt for credentials:i:0",
        "negotiate security layer:i:1",
        "remoteapplicationmode:i:0",
        "alternate shell:s:",
        "shell working directory:s:",
        "gatewayhostname:s:",
        "gatewayusagemethod:i:4",
        "gatewaycredentialssource:i:4",
        "gatewayprofileusagemethod:i:0",
        "promptcredentialonce:i:0",
        "gatewaybrokeringtype:i:0",
        "use redirection server name:i:0",
        "rdgiskdcproxy:i:0",
        "kdcproxyname:s:",
    ]
    body = "\r\n".join(rdp_lines) + "\r\n"
    safe_name = "".join(ch for ch in (card_id + "_" + ip.replace(".", "_")) if ch.isalnum() or ch in "_-") + ".rdp"
    from fastapi.responses import Response
    headers = {
        "Content-Disposition": f"attachment; filename=\"{safe_name}\"",
        "X-Dedik-Login": login,
    }
    # Пароль в RDP-файле НЕ сохраняется (Windows не примет без CryptProtectData).
    # Возвращаем его отдельным header'ом, чтобы UI смог показать "ввести пароль: ..."
    if creds.get("password"):
        # Не пихаем пароль в .rdp — Windows требует local CryptProtectData,
        # которого у нас в Linux нет. UI должен показать пароль рядом для
        # копирования.
        headers["X-Dedik-Password"] = creds["password"]
    headers["Access-Control-Expose-Headers"] = "X-Dedik-Login, X-Dedik-Password"
    return Response(
        content=body,
        media_type="application/x-rdp",
        headers=headers,
    )


@app.get("/api/operational/guacamole_session")
async def api_op_guacamole_session(card_id: str = "", me: dict = Depends(_get_me)):
    """Если в env задан GUACAMOLE_URL — создаёт RDP-сессию через прокси
    (guacamole/proxy.py) и возвращает URL для inline iframe.
    Иначе клиент использует fallback на .rdp файл."""
    if me.get("role") not in ("owner", "manager", "operationist", "system"):
        raise HTTPException(status_code=403, detail="forbidden")

    import os as _os
    base = (_os.getenv("GUACAMOLE_URL") or "").strip().rstrip("/")
    if not base:
        return {"url": None}

    creds = _resolve_dedik_creds_for_card(card_id)
    if not creds.get("ip"):
        return {"url": None, "error": "no_dedik"}

    secret = (_os.getenv("PRIDE_GUAC_SECRET") or "").strip()
    if not secret:
        # Прокси без секрета работать не будет — без auth могут ходить
        # все кто угодно. Возвращаем fallback на .rdp.
        return {
            "url": None,
            "error": "PRIDE_GUAC_SECRET not set in main service",
        }

    # POST к нашему Guacamole-прокси (см. guacamole/proxy.py в репо)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{base}/api/proxy/create_session",
                json={
                    "ip": creds["ip"],
                    "user": creds.get("login") or "Administrator",
                    "password": creds.get("password") or "",
                    "width": 1920,
                    "height": 1080,
                },
                headers={"X-Pride-Secret": secret},
            )
        if r.status_code != 200:
            logger.warning("guac proxy failed: %s %s", r.status_code, r.text[:200])
            return {
                "url": None,
                "error": f"guac proxy {r.status_code}: {r.text[:120]}",
            }
        data = r.json()
        rel = data.get("url") or ""
        if not rel:
            return {"url": None, "error": "guac proxy returned empty url"}
        # rel это "/guacamole/#/client/...&token=..." — добавляем base
        full = base + rel
        return {"url": full, "identifier": data.get("identifier")}
    except Exception as e:
        logger.exception("guac proxy call failed: %s", e)
        return {"url": None, "error": str(e)}


@app.get("/api/operational/dedik_creds/{card_id}")
async def api_op_dedik_creds(card_id: str, me: dict = Depends(_get_me)):
    """Возвращает дед-креды для отображения рядом с .rdp кнопкой (пароль
    нельзя записать в .rdp на стороне сервера — нужен Windows CryptProtectData)."""
    if me.get("role") not in ("owner", "manager", "operationist", "system"):
        raise HTTPException(status_code=403, detail="forbidden")
    storage.reload_sync()
    creds = _resolve_dedik_creds_for_card(card_id)
    if not creds.get("ip"):
        return {"found": False}
    return {
        "found": True,
        "ip": creds.get("ip"),
        "login": creds.get("login"),
        "password": creds.get("password"),
        "location": creds.get("location"),
    }


# ============================================================================
# ACCOUNTING v2 (Бухгалтерия: касса/зарплаты/реклама/симки/поставщики)
# ============================================================================

ACCOUNTING_CATEGORIES = ("kassa", "suppliers", "salaries", "ads", "sims")
ACCOUNTING_CATEGORY_ROLE = {
    "kassa": "manager",
    "suppliers": "manager",
    "salaries": "accounting",
    "ads": "accounting",
    "sims": "manager",
}
ROLE_RANK = {
    "owner": 100, "manager": 80, "accounting": 60,
    "operationist": 40, "system": 30, "guest": 0,
}


def _has_role(me_role: str, required: str) -> bool:
    return ROLE_RANK.get(me_role or "guest", 0) >= ROLE_RANK.get(required, 0)


@app.get("/api/accounting/entries")
async def api_acc_list(
    category: Optional[str] = None, days: int = 30, _: None = Depends(_auth),
):
    storage.reload_sync()
    date_from = time.time() - max(1, days) * 86400
    entries = storage.list_accounting_entries(category=category, date_from=date_from)
    return {"entries": entries, "total": len(entries)}


class AccountingEntryPayload(BaseModel):
    category: str
    amount_usdt: float = 0
    amount_rub: float = 0
    note: str = ""


@app.post("/api/accounting/entry")
async def api_acc_add(
    payload: AccountingEntryPayload,
    me: dict = Depends(_get_me),
    _perm: bool = Depends(require_action("accounting_entry_add")),
):
    cat = (payload.category or "").lower()
    if cat not in ACCOUNTING_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    required = ACCOUNTING_CATEGORY_ROLE.get(cat, "manager")
    if not _has_role(me.get("role") or "", required):
        raise HTTPException(status_code=403, detail="insufficient role")
    entry_id = await storage.add_accounting_entry(
        category=cat,
        amount_usdt=payload.amount_usdt,
        amount_rub=payload.amount_rub,
        note=payload.note or "",
        created_by=me.get("username") or str(me.get("tg_id")) or "",
    )
    return {"entry_id": entry_id}


@app.delete("/api/accounting/entry/{entry_id}")
async def api_acc_delete(entry_id: str, me: dict = Depends(_get_me), _perm: bool = Depends(require_action("accounting_entry_delete"))):
    if not _has_role(me.get("role") or "", "manager"):
        raise HTTPException(status_code=403, detail="forbidden")
    ok = await storage.delete_accounting_entry(entry_id)
    return {"ok": ok}


@app.get("/api/accounting/summary")
async def api_acc_summary(period: str = "month", _: None = Depends(_auth)):
    storage.reload_sync()
    if period == "day":
        date_from = time.time() - 86400
    elif period == "week":
        date_from = time.time() - 7 * 86400
    elif period == "all":
        date_from = None
    else:
        date_from = time.time() - 30 * 86400
    return storage.accounting_summary(date_from=date_from)


@app.get("/api/applications")
async def api_applications(
    days: int = 7,
    _: None = Depends(_auth),
):
    """Заявки V2 за последние N дней (включая сегодня)."""
    storage.reload_sync()
    result = []
    today = datetime.now()
    for i in range(max(1, days)):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        apps = storage.get_applications_v2(d) or []
        for a in apps:
            result.append({
                "date": d,
                "id": a.get("id"),
                "intake": a.get("intake"),
                "outputs": a.get("outputs") or [],
                "course_withdrawal": a.get("course_withdrawal"),
                "course_payout": a.get("course_payout"),
                "partner_pct": a.get("partner_pct"),
                "computed": a.get("computed", {}),
                "ts": a.get("ts"),
            })
    return {"applications": result}


@app.get("/api/chats")
async def api_chats(_: None = Depends(_auth)):
    """Managed-чаты (рабочие беседы клиентов)."""
    storage.reload_sync()
    managed = storage.state.get("managed_chats") or {}
    result = []
    for chat_id, info in managed.items():
        result.append({
            "chat_id": chat_id,
            "client_id": info.get("client_id"),
            "client_name": info.get("client_name"),
            "client_username": info.get("client_username"),
            "payment_method": info.get("payment_method"),
            "usdt_address": info.get("usdt_address"),
            "created_at": info.get("created_at"),
            "welcome_sent": info.get("welcome_sent"),
            "pending_perevyaz": info.get("pending_perevyaz"),
        })
    result.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return {"chats": result, "total": len(result)}


@app.get("/api/deals")
async def api_deals(
    status_filter: Optional[str] = None,
    limit: int = 200,
    _: None = Depends(_auth),
):
    """Сделки storage.deals."""
    storage.reload_sync()
    deals = storage.list_deals() or {}
    result = []
    for did, d in deals.items():
        if status_filter and (d.get("status") or "") != status_filter:
            continue
        result.append({
            "deal_id": did,
            "client_username": d.get("client_username"),
            "fio": d.get("fio"),
            "bank": d.get("bank"),
            "amount": d.get("amount"),
            "fee": d.get("fee"),
            "method": d.get("method"),
            "status": d.get("status"),
            "work_chat_id": d.get("work_chat_id"),
            "created_at": d.get("created_at"),
        })
    result.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return {"deals": result[:limit], "total": len(result)}


@app.get("/api/funnel")
async def api_funnel(days: int = 7, _: None = Depends(_auth)):
    """Воронка конверсии — счётчики по дням."""
    storage.reload_sync()
    rows = storage.get_funnel_range(max(1, min(days, 60)))
    # Суммарные значения за период
    totals: dict = {}
    for r in rows:
        for k, v in r.items():
            if k == "date" or not isinstance(v, (int, float)):
                continue
            totals[k] = totals.get(k, 0) + float(v)
    return {"days": rows, "totals": totals}


@app.get("/api/managers")
async def api_managers(_: None = Depends(_auth)):
    """Стата по менеджерам/работникам — для офис-панели."""
    storage.reload_sync()
    ms = storage.list_manager_stats()
    roles = storage.state.get("worker_roles") or {}
    # Объединяем roles + stats
    all_users = set(ms.keys()) | set(roles.keys())
    out = []
    now_ts = datetime.now().timestamp()
    for uname in all_users:
        s = ms.get(uname) or {}
        r = roles.get(uname) or {}
        last = float(s.get("last_active_ts") or 0)
        idle_sec = (now_ts - last) if last else None
        if idle_sec is None:
            online = "offline"
        elif idle_sec < 300:
            online = "online"
        elif idle_sec < 3600:
            online = "idle"
        else:
            online = "offline"
        # Подсчёт тегов / ответов на теги по этому юзернейму
        tags_received = 0
        replies_to_tags = 0
        try:
            esc_tags = storage.state.get("escalation_tags") or {}
            for _chat_key, by_spec in esc_tags.items():
                e = by_spec.get(uname) or {}
                tags_received += int(e.get("tags_total") or 0)
                replies_to_tags += int(e.get("replies_total") or 0)
        except Exception:
            pass
        # AI escalate-stats — сколько раз AI вызывал именно этого менеджера
        ai_escalated = 0
        try:
            est = storage.state.get("escalate_stats") or {}
            by_spec = est.get("by_specialist") or {}
            ai_escalated = int(by_spec.get(uname) or 0)
        except Exception:
            pass
        # Response rate: насколько часто менеджер отвечает на теги (0..1)
        resp_rate = (
            round(replies_to_tags / tags_received, 2)
            if tags_received > 0 else None
        )
        out.append({
            "username": uname,
            "role": r.get("role") or "—",
            "is_admin": bool(r.get("is_admin")),
            "messages": int(s.get("messages") or 0),
            "chats_touched": int(s.get("chats_touched") or 0),
            "payments_made": int(s.get("payments_made") or 0),
            "lk_completed": int(s.get("lk_completed") or 0),
            "tags_received": tags_received,
            "replies_to_tags": replies_to_tags,
            "response_rate": resp_rate,
            "ai_escalated": ai_escalated,
            "last_active_ts": last,
            "idle_sec": idle_sec,
            "online": online,
        })
    # Сортируем: online → idle → offline; внутри — по messages убыв.
    online_priority = {"online": 0, "idle": 1, "offline": 2}
    out.sort(key=lambda x: (online_priority[x["online"]], -x["messages"]))
    return {"managers": out, "total": len(out)}


# ===== DISCORD-LIKE HUB API =====

class DiscordChannelReq(BaseModel):
    name: str
    type: str = "text"
    category: str = "general"
    topic: str = ""


class DiscordMessageReq(BaseModel):
    channel_id: str
    text: str = ""
    reply_to: Optional[str] = None
    attachments: Optional[list] = None
    mentions: Optional[list] = None


@app.get("/api/discord/channels")
async def api_discord_channels(_: None = Depends(_auth)):
    """Список каналов внутреннего хаба админов."""
    storage.reload_sync()
    channels = storage.list_discord_channels()
    # Авто-создание дефолтных каналов при первом запросе
    if not channels:
        await storage.add_discord_channel(
            "общий", "text", "general", "Общий чат админов",
            created_by="system",
        )
        await storage.add_discord_channel(
            "выплаты", "text", "general", "Координация выплат",
            created_by="system",
        )
        await storage.add_discord_channel(
            "ЛК-перевязки", "text", "general", "Обсуждение перевязок",
            created_by="system",
        )
        await storage.add_discord_channel(
            "переговорка", "voice", "voice", "Голосовая комната для созвонов",
            created_by="system",
        )
        channels = storage.list_discord_channels()
    # Активность звонков теперь живёт в памяти api.py (через WebSocket).
    # Тут заполняем voice_participants чтоб UI сразу показал кто в голосе.
    for ch in channels:
        if ch.get("type") == "voice":
            ch["voice_participants"] = _online_users_in_voice(ch["id"])
        else:
            ch["voice_participants"] = []
    return {"channels": channels}


@app.post("/api/discord/channels")
async def api_discord_create_channel(
    req: DiscordChannelReq, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("discord_channel_create")),
):
    storage.reload_sync()
    cid = await storage.add_discord_channel(
        name=req.name, ch_type=req.type, category=req.category,
        topic=req.topic, created_by="dashboard",
    )
    try:
        event_bus.emit_event(
            "discord-channel-created",
            {"channel_id": cid, "name": req.name, "type": req.type},
        )
    except Exception:
        pass
    return {"ok": True, "channel_id": cid}


@app.delete("/api/discord/channels/{channel_id}")
async def api_discord_delete_channel(channel_id: str, _: None = Depends(_auth), _perm: bool = Depends(require_action("discord_channel_delete"))):
    storage.reload_sync()
    ok = await storage.delete_discord_channel(channel_id)
    if not ok:
        raise HTTPException(status_code=404, detail="channel not found")
    try:
        event_bus.emit_event(
            "discord-channel-deleted", {"channel_id": channel_id},
        )
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/discord/messages")
async def api_discord_messages(
    channel_id: str,
    limit: int = 100,
    before_ts: Optional[float] = None,
    _: None = Depends(_auth),
):
    try:
        storage.reload_sync()
    except Exception as e:
        logger.warning("discord_messages reload_sync failed: %s", e)
    try:
        msgs = storage.list_discord_messages(
            channel_id, limit=max(1, min(limit, 500)), before_ts=before_ts,
        ) or []
    except Exception as e:
        logger.warning("list_discord_messages failed for %s: %s", channel_id, e)
        msgs = []
    # Reactions — каждое падение ловим отдельно
    try:
        all_reacts = storage.get_all_discord_reactions() or {}
    except Exception as e:
        logger.warning("get_all_discord_reactions failed: %s", e)
        all_reacts = {}
    safe_msgs = []
    for m in msgs:
        try:
            if not isinstance(m, dict):
                continue
            m["_reacts"] = all_reacts.get(m.get("id")) or {}
            safe_msgs.append(m)
        except Exception as e:
            logger.debug("discord message skip (bad shape): %s", e)
    return {"messages": safe_msgs, "channel_id": channel_id}


@app.post("/api/discord/messages")
async def api_discord_send_message(
    req: DiscordMessageReq, request: Request, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("discord_message_send")),
):
    storage.reload_sync()
    if not req.channel_id:
        raise HTTPException(status_code=400, detail="channel_id required")
    text = (req.text or "").strip()
    if not text and not req.attachments:
        raise HTTPException(status_code=400, detail="text or attachments required")
    author = _resolve_discord_user(request)
    # Загружаем avatar из TG-info
    author_avatar = ""
    try:
        uid = _try_session_auth(request)
        if uid:
            info = storage.get_tg_user_info(uid) or {}
            author_avatar = info.get("photo_url") or ""
    except Exception:
        pass
    # Парсим mentions из текста если не переданы (@username, @ассистент)
    mentions = req.mentions or []
    if not mentions:
        import re as _re_local
        mentions = list(set(_re_local.findall(r"@([\w_]+)", text)))
    msg = await storage.add_discord_message(
        channel_id=req.channel_id,
        author=author,
        author_avatar=author_avatar,
        text=text,
        attachments=req.attachments or [],
        mentions=mentions,
        reply_to=req.reply_to,
    )
    try:
        event_bus.emit_event(
            "discord-message",
            {
                "channel_id": req.channel_id,
                "message_id": msg["id"],
                "author": msg["author"],
                "short": text[:160],
            },
        )
    except Exception:
        pass
    return {"ok": True, "message": msg}


@app.delete("/api/discord/messages/{message_id}")
async def api_discord_delete_message(
    message_id: str, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("discord_message_delete")),
):
    storage.reload_sync()
    ok = await storage.delete_discord_message(message_id)
    if not ok:
        raise HTTPException(status_code=404, detail="message not found")
    try:
        event_bus.emit_event(
            "discord-message-deleted", {"message_id": message_id},
        )
    except Exception:
        pass
    return {"ok": True}


# Удалены legacy endpoints /api/discord/calls/{id}/{join|leave}.
# Голосовые звонки теперь через WebSocket /ws-discord (см. ниже).


# ===== /api/me — текущий админ для frontend =====

@app.get("/api/me")
async def api_me(request: Request, _: None = Depends(_auth)):
    """Информация о текущем залогиненном админе:
    user_id, username, first_name, photo_url. Используется Discord-хабом
    для аватарок и для voice-сигналинга.

    Если профиль TG ещё не сохранён (юзер логинился до того как мы добавили
    record_tg_user_info), просим перелогиниться — но всё равно отдаём
    fallback чтоб фронт не падал."""
    try:
        uid = _try_session_auth(request)
    except Exception:
        uid = None
    if not uid:
        return {
            "user_id": 0,
            "username": "admin",
            "first_name": "Admin",
            "photo_url": "",
            "auth_mode": "basic",
        }
    try:
        info = storage.get_tg_user_info(uid) or {}
    except Exception:
        info = {}
    # Если TG-профиль пуст — fallback с user_id, но просим перелогиниться
    needs_relogin = not (info.get("username") or info.get("first_name"))
    return {
        "user_id": uid,
        "username": info.get("username") or "",
        "first_name": info.get("first_name") or f"user_{uid}",
        "last_name": info.get("last_name") or "",
        "photo_url": info.get("photo_url") or "",
        "auth_mode": "telegram",
        "needs_relogin": needs_relogin,
    }


# ===== Discord reactions + pins =====

class DiscordReactionReq(BaseModel):
    emoji: str


@app.post("/api/discord/messages/{message_id}/reactions")
async def api_discord_add_reaction(
    message_id: str, req: DiscordReactionReq,
    request: Request, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("discord_reaction_add")),
):
    user = _resolve_discord_user(request)
    reacts = await storage.add_discord_reaction(message_id, req.emoji, user)
    try:
        event_bus.emit_event(
            "discord-reaction",
            {"message_id": message_id, "emoji": req.emoji,
             "user": user, "reactions": reacts},
        )
    except Exception:
        pass
    return {"ok": True, "reactions": reacts}


@app.delete("/api/discord/messages/{message_id}/reactions/{emoji}")
async def api_discord_remove_reaction(
    message_id: str, emoji: str,
    request: Request, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("discord_reaction_remove")),
):
    user = _resolve_discord_user(request)
    reacts = await storage.remove_discord_reaction(message_id, emoji, user)
    try:
        event_bus.emit_event(
            "discord-reaction-removed",
            {"message_id": message_id, "emoji": emoji,
             "user": user, "reactions": reacts},
        )
    except Exception:
        pass
    return {"ok": True, "reactions": reacts}


@app.post("/api/discord/messages/{message_id}/pin")
async def api_discord_pin(
    message_id: str, request: Request, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("discord_pin")),
):
    # Найдём channel_id сообщения
    msgs = storage.state.get("discord_messages") or []
    m = next((x for x in msgs if x.get("id") == message_id), None)
    if not m:
        raise HTTPException(status_code=404, detail="message not found")
    ok = await storage.pin_discord_message(m["channel_id"], message_id)
    return {"ok": ok}


@app.post("/api/discord/messages/{message_id}/unpin")
async def api_discord_unpin(
    message_id: str, request: Request, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("discord_unpin")),
):
    msgs = storage.state.get("discord_messages") or []
    m = next((x for x in msgs if x.get("id") == message_id), None)
    if not m:
        raise HTTPException(status_code=404, detail="message not found")
    ok = await storage.unpin_discord_message(m["channel_id"], message_id)
    return {"ok": ok}


@app.get("/api/discord/channels/{channel_id}/pins")
async def api_discord_pins(channel_id: str, _: None = Depends(_auth)):
    pins_ids = storage.get_pinned_messages(channel_id)
    msgs = storage.state.get("discord_messages") or []
    pinned = [m for m in msgs if m.get("id") in pins_ids]
    return {"pinned": pinned}


def _resolve_discord_user(request: Request) -> str:
    """Резолвит username текущего залогиненного админа."""
    uid = _try_session_auth(request)
    if uid:
        info = storage.get_tg_user_info(uid) or {}
        if info.get("username"):
            return info["username"]
        if info.get("first_name"):
            return info["first_name"]
        return f"user_{uid}"
    return "admin"


# ===== Per-session activity tracking =====
# Каждый браузер дашборда — независимая сессия. Здесь храним недавнюю
# активность для отладки и отображения в dashboard «кто сейчас что делает».
# В памяти api.py (не персистится), макс 50 активностей на пользователя.

_session_activity: dict = {}   # {user: [{ts, action, payload}, ...]}
_session_last_seen: dict = {}  # {user: ts}


def _record_session_activity(
    request: Request, action: str, payload: Optional[dict] = None,
):
    """Запишет активность конкретного админа. Используется для
    отображения «кто что делает» и для отладки кросс-сессионных конфликтов."""
    user = _resolve_discord_user(request)
    entry = {
        "ts": time.time(),
        "action": action,
        "payload": payload or {},
    }
    lst = _session_activity.setdefault(user, [])
    lst.append(entry)
    # Cap 50 на пользователя
    if len(lst) > 50:
        _session_activity[user] = lst[-50:]
    _session_last_seen[user] = entry["ts"]


@app.get("/api/sessions")
async def api_sessions(_: None = Depends(_auth)):
    """Кто сейчас активен в дашборде + что они делают.
    Объединяет:
      - WebSocket-connected (Discord-хаб): сейчас открыта вкладка
      - Recent activity: кто что недавно делал
    """
    online_ws = _online_users_total()
    now = time.time()
    recent = []
    for user, last_ts in _session_last_seen.items():
        idle = now - last_ts
        if idle > 600:  # 10 мин — считается offline
            continue
        activity_list = _session_activity.get(user, [])
        recent.append({
            "user": user,
            "last_seen_ts": last_ts,
            "idle_sec": int(idle),
            "recent_actions": activity_list[-5:],
        })
    recent.sort(key=lambda x: x["idle_sec"])
    return {
        "ws_online": online_ws,
        "recent": recent,
        "total_sessions_tracked": len(_session_activity),
    }


# ===== Discord WebSocket: presence + voice signaling =====
# В памяти api.py (не персистится — теряется при рестарте).
# Структура:
#   _ws_sessions: {session_id: {ws, user_info, voice_channel_id}}
#   _voice_rooms: {channel_id: {session_id: user_info}}

_ws_sessions: dict = {}
_voice_rooms: dict = {}


def _online_users_in_voice(channel_id: str) -> list:
    """Список пользователей сейчас в голосовом канале."""
    room = _voice_rooms.get(channel_id) or {}
    out = []
    for sid, info in room.items():
        out.append({
            "session_id": sid,
            "user_id": info.get("user_id"),
            "username": info.get("username"),
            "first_name": info.get("first_name"),
            "photo_url": info.get("photo_url"),
            "muted": info.get("muted", False),
            "deafened": info.get("deafened", False),
        })
    return out


def _online_users_total() -> list:
    """Кто сейчас подключён к дашборду (имеет открытый WS)."""
    out = []
    seen_users = set()
    for sid, sess in _ws_sessions.items():
        info = sess.get("user_info") or {}
        uname = info.get("username") or info.get("first_name") or sid
        if uname in seen_users:
            continue
        seen_users.add(uname)
        out.append({
            "user_id": info.get("user_id"),
            "username": info.get("username"),
            "first_name": info.get("first_name"),
            "photo_url": info.get("photo_url"),
            "in_voice": sess.get("voice_channel_id"),
        })
    return out


@app.get("/api/discord/online")
async def api_discord_online(_: None = Depends(_auth)):
    """Кто сейчас онлайн (имеет открытый WS) + кто в каких voice-каналах."""
    voice_state = {}
    for ch_id in _voice_rooms.keys():
        voice_state[ch_id] = _online_users_in_voice(ch_id)
    return {
        "online_users": _online_users_total(),
        "voice_channels": voice_state,
    }


async def _ws_broadcast_to_room(
    channel_id: str, message: dict, exclude_session: Optional[str] = None,
):
    """Послать message всем в voice-канале кроме exclude_session."""
    room = _voice_rooms.get(channel_id) or {}
    for sid in list(room.keys()):
        if sid == exclude_session:
            continue
        sess = _ws_sessions.get(sid)
        if not sess or not sess.get("ws"):
            continue
        try:
            await sess["ws"].send_json(message)
        except Exception as e:
            logger.debug("ws broadcast to %s failed: %s", sid, e)


async def _ws_send_to_session(session_id: str, message: dict) -> bool:
    sess = _ws_sessions.get(session_id)
    if not sess or not sess.get("ws"):
        return False
    try:
        await sess["ws"].send_json(message)
        return True
    except Exception:
        return False


@app.websocket("/ws-discord")
async def discord_ws(ws: WebSocket):
    """WebSocket для Discord-хаба: presence + WebRTC signaling.

    Протокол сообщений (JSON):
      Клиент → Сервер:
        { type: "hello" } — после подключения, сервер ответит { type: "ready", session_id, peers }
        { type: "join-voice", channel_id }
        { type: "leave-voice" }
        { type: "signal", target, payload } — WebRTC offer/answer/ICE
        { type: "mute", muted: bool }
        { type: "deaf", deafened: bool }
        { type: "typing", channel_id }

      Сервер → Клиент:
        { type: "ready", session_id, me }
        { type: "peer-joined", session_id, user } — другой юзер зашёл в твой voice
        { type: "peer-left", session_id }
        { type: "signal", from, payload }
        { type: "voice-state", channel_id, participants }
        { type: "typing", channel_id, user }
        { type: "presence", online }
    """
    # Auth — пытаемся через session cookie. Если нет — даём гостевую
    # admin-сессию (Basic auth уже отработал на HTTP-уровне; для WS-апгрейда
    # cookie может отсутствовать).
    user_info = {}
    uid = None
    try:
        cookie_val = ws.cookies.get(SESSION_COOKIE)
        if cookie_val:
            uid = _verify_session(cookie_val)
        if uid:
            try:
                info = storage.get_tg_user_info(uid) or {}
            except Exception:
                info = {}
            user_info = {
                "user_id": uid,
                "username": info.get("username") or "",
                "first_name": info.get("first_name") or f"user_{uid}",
                "photo_url": info.get("photo_url") or "",
            }
    except Exception as e:
        logger.warning("ws auth check failed: %s", e)
    if not user_info:
        # Basic-auth или session отсутствует — anonymous admin-сессия
        user_info = {
            "user_id": 0, "username": "admin",
            "first_name": "Admin", "photo_url": "",
        }
    # accept ВСЕГДА — отказать заранее не можем (Basic auth требует Authorization
    # header, который браузер не отправляет на WS-upgrade автоматом)
    try:
        await ws.accept()
    except Exception as e:
        logger.warning("ws accept failed: %s", e)
        return
    import uuid as _uuid
    session_id = _uuid.uuid4().hex[:12]
    _ws_sessions[session_id] = {
        "ws": ws,
        "user_info": user_info,
        "voice_channel_id": None,
        "connected_at": time.time(),
    }
    # Активность для tracking
    try:
        _session_activity.setdefault(
            user_info.get("username") or user_info.get("first_name") or "admin", [],
        ).append({
            "ts": time.time(),
            "action": "ws-connect",
            "payload": {"session_id": session_id},
        })
        _session_last_seen[user_info.get("username") or user_info.get("first_name") or "admin"] = time.time()
    except Exception:
        pass
    # Сообщаем клиенту его session_id + кто он
    try:
        await ws.send_json({
            "type": "ready",
            "session_id": session_id,
            "me": user_info,
        })
    except Exception:
        pass
    # Эмитим presence-update всем
    try:
        event_bus.emit_event(
            "discord-presence-update",
            {"online": _online_users_total()},
        )
    except Exception:
        pass
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")
            if t == "join-voice":
                ch_id = data.get("channel_id") or ""
                if not ch_id:
                    continue
                # Если был в другом канале — выйти
                prev = _ws_sessions[session_id].get("voice_channel_id")
                if prev and prev != ch_id:
                    await _leave_voice(session_id)
                _ws_sessions[session_id]["voice_channel_id"] = ch_id
                room = _voice_rooms.setdefault(ch_id, {})
                room[session_id] = user_info
                # Сообщаем всем остальным в этом канале о новом пире
                await _ws_broadcast_to_room(
                    ch_id,
                    {"type": "peer-joined", "session_id": session_id, "user": user_info},
                    exclude_session=session_id,
                )
                # Шлём новому пиру список существующих пиров (чтобы он сделал offer)
                existing = [
                    {"session_id": sid, "user": info}
                    for sid, info in room.items() if sid != session_id
                ]
                await ws.send_json({
                    "type": "voice-state",
                    "channel_id": ch_id,
                    "participants": _online_users_in_voice(ch_id),
                    "existing_peers": existing,
                })
                try:
                    event_bus.emit_event(
                        "discord-voice-state",
                        {"channel_id": ch_id,
                         "participants": _online_users_in_voice(ch_id)},
                    )
                except Exception:
                    pass
            elif t == "leave-voice":
                await _leave_voice(session_id)
            elif t == "signal":
                target = data.get("target")
                payload = data.get("payload")
                if target:
                    await _ws_send_to_session(target, {
                        "type": "signal",
                        "from": session_id,
                        "payload": payload,
                    })
            elif t == "mute":
                muted = bool(data.get("muted"))
                ch_id = _ws_sessions[session_id].get("voice_channel_id")
                if ch_id:
                    room = _voice_rooms.get(ch_id) or {}
                    if session_id in room:
                        room[session_id]["muted"] = muted
                        await _ws_broadcast_to_room(
                            ch_id,
                            {"type": "voice-state",
                             "channel_id": ch_id,
                             "participants": _online_users_in_voice(ch_id)},
                        )
            elif t == "deaf":
                deafened = bool(data.get("deafened"))
                ch_id = _ws_sessions[session_id].get("voice_channel_id")
                if ch_id:
                    room = _voice_rooms.get(ch_id) or {}
                    if session_id in room:
                        room[session_id]["deafened"] = deafened
                        await _ws_broadcast_to_room(
                            ch_id,
                            {"type": "voice-state",
                             "channel_id": ch_id,
                             "participants": _online_users_in_voice(ch_id)},
                        )
            elif t == "typing":
                ch_id = data.get("channel_id") or ""
                if ch_id:
                    try:
                        event_bus.emit_event(
                            "discord-typing",
                            {"channel_id": ch_id,
                             "user": user_info.get("username") or user_info.get("first_name")},
                        )
                    except Exception:
                        pass
            elif t == "ping":
                try:
                    await ws.send_json({"type": "pong"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("ws-discord loop error: %s", e)
    finally:
        await _leave_voice(session_id)
        _ws_sessions.pop(session_id, None)
        try:
            event_bus.emit_event(
                "discord-presence-update",
                {"online": _online_users_total()},
            )
        except Exception:
            pass


async def _leave_voice(session_id: str):
    """Удалить сессию из voice-канала и оповестить остальных."""
    sess = _ws_sessions.get(session_id)
    if not sess:
        return
    ch_id = sess.get("voice_channel_id")
    if not ch_id:
        return
    sess["voice_channel_id"] = None
    room = _voice_rooms.get(ch_id) or {}
    if session_id in room:
        del room[session_id]
    if not room:
        _voice_rooms.pop(ch_id, None)
    await _ws_broadcast_to_room(
        ch_id,
        {"type": "peer-left", "session_id": session_id},
    )
    try:
        event_bus.emit_event(
            "discord-voice-state",
            {"channel_id": ch_id,
             "participants": _online_users_in_voice(ch_id)},
        )
    except Exception:
        pass


# === Payouts queues (USDT / Guarantor) ===

@app.get("/api/payouts")
async def api_payouts(_: None = Depends(_auth)):
    """Все 3 очереди выплат: release, fund_release, usdt."""
    storage.reload_sync()
    return {
        "release": storage.list_payouts("release"),
        "fund_release": storage.list_payouts("fund_release"),
        "usdt": storage.list_payouts("usdt"),
    }


@app.post("/api/payouts/usdt_paid")
async def api_payouts_usdt_paid(req: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("payout_usdt_paid"))):
    """Менеджер ввёл TronScan хеш для USDT-выплаты.
    body: {card_id, tx_hash}"""
    data = await req.json()
    card_id = (data.get("card_id") or "").lower().lstrip("#")
    tx_hash = (data.get("tx_hash") or "").strip()
    if not card_id or not tx_hash or len(tx_hash) < 16:
        raise HTTPException(400, "card_id and tx_hash (>=16 chars) required")
    try:
        await storage.enqueue_dashboard_command(
            f"выплачено #{card_id} {tx_hash}",
            source="dashboard-usdt-paid",
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "card_id": card_id, "tx_hash": tx_hash}


@app.post("/api/payouts/deal_funded")
async def api_payouts_deal_funded(req: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("payout_deal_funded"))):
    """Менеджер: сделка пополнена. body: {deal_id, amount}"""
    data = await req.json()
    deal_id = (str(data.get("deal_id") or "")).lstrip("#").strip()
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "amount must be number")
    if not deal_id or amount <= 0:
        raise HTTPException(400, "deal_id and positive amount required")
    try:
        await storage.enqueue_dashboard_command(
            f"сделка #{deal_id} пополнена {amount}",
            source="dashboard-deal-funded",
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "deal_id": deal_id, "amount": amount}


@app.post("/api/payouts/released")
async def api_payouts_released(req: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("payout_released"))):
    """Менеджер отпустил гарант-сделку. body: {card_id} или {deal_id}"""
    data = await req.json()
    card_id = (data.get("card_id") or "").lower().lstrip("#")
    deal_id = (str(data.get("deal_id") or "")).lstrip("#").strip()
    key = card_id or deal_id
    if not key:
        raise HTTPException(400, "card_id or deal_id required")
    try:
        await storage.enqueue_dashboard_command(
            f"отпущено #{key}",
            source="dashboard-released",
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    # === Auto-accounting: +kassa (приход от клиента когда сделка отпущена) ===
    # Цена ЛК извлекается из card если возможно, иначе amount=0 (запись с пометкой)
    try:
        if card_id:
            card = storage.get_lk_card(card_id)
            if card:
                price = float(card.get("price_usdt") or 0)
                if price > 0:
                    await storage.add_accounting_entry(
                        category="kassa",
                        amount_usdt=price,
                        note=f"auto: release #{card_id} ({card.get('bank', '?')} {card.get('fio', '?')})",
                        created_by="auto:payout_released",
                    )
                    try:
                        await storage.add_notification(
                            type="success",
                            text=f"💰 Авто-запись в кассу: +{price:.2f}$ от ЛК #{card_id}",
                            dedup_key=f"auto_kassa:{card_id}",
                        )
                    except Exception:
                        pass
                # === Auto-accrue worker compensation ===
                # Кто отработал ЛК (supplier на карточке)? + у него есть compensation rules?
                supplier = (card.get("supplier") or "").lstrip("@").lower()
                if supplier and price > 0:
                    comp = storage.get_worker_compensation(supplier)
                    if comp:
                        worker_due = 0.0
                        if comp.get("rate_per_lk_usdt"):
                            worker_due += float(comp["rate_per_lk_usdt"])
                        if comp.get("pct_of_lk_price"):
                            worker_due += price * float(comp["pct_of_lk_price"]) / 100
                        if worker_due > 0:
                            await storage.accrue_to_worker(
                                supplier, worker_due,
                                reason=f"LK #{card_id} release (price={price})",
                            )
                            try:
                                await storage.add_notification(
                                    type="info",
                                    text=f"👷 @{supplier}: +{worker_due:.2f}$ начислено (LK #{card_id}). "
                                         f"Pending: {storage.get_worker_pending(supplier):.2f}$",
                                    dedup_key=f"accrue:{supplier}:{card_id}",
                                )
                            except Exception:
                                pass
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("auto-acc kassa failed: %s", _e)
    return {"ok": True, "key": key}


@app.post("/api/payouts/set_deal_id")
async def api_payouts_set_deal_id(req: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("payout_set_deal_id"))):
    """Менеджер ввёл номер сделки от клиента в fund_release очередь.
    Также синхронизирует deal_id на саму карточку ЛК (чтобы анкета в TG-группе
    тоже отразила номер сделки).
    body: {payout_id, deal_id}"""
    data = await req.json()
    payout_id = int(data.get("payout_id") or 0)
    deal_id = (str(data.get("deal_id") or "")).lstrip("#").strip()
    if not payout_id or not deal_id:
        raise HTTPException(400, "payout_id and deal_id required")
    ok = await storage.update_payout("fund_release", payout_id, deal_id=deal_id)
    if not ok:
        raise HTTPException(404, "payout not found")
    # Найдём card_id из этой записи и обновим саму карточку
    try:
        arr = (storage._payouts_state().get("fund_release") or [])
        item = next((i for i in arr if int(i.get("id") or 0) == payout_id), None)
        if item and item.get("card_id"):
            await storage.update_lk_card(item["card_id"], deal_id=deal_id)
            # Запросим userbot обновить анкету в TG-группе ЛК
            await storage.enqueue_dashboard_command(
                f"__sync_lk_card {item['card_id']}",
                source="dashboard-set-deal-id",
            )
    except Exception as e:
        logger.warning("set_deal_id: card update failed: %s", e)
    return {"ok": True, "payout_id": payout_id, "deal_id": deal_id}


# === CRM (Партнёры — Дропы — ЛК) ===

@app.get("/api/crm/owners")
async def api_crm_owners(_: None = Depends(_auth)):
    """Список партнёров CRM со статистикой."""
    storage.reload_sync()
    owners = storage.list_crm_owners() if hasattr(storage, "list_crm_owners") else {}
    drops = storage.list_crm_drops() if hasattr(storage, "list_crm_drops") else {}
    lks = storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}
    # Группировки
    drops_by_owner: Dict[str, list] = {}
    for d in (drops or {}).values():
        drops_by_owner.setdefault(d.get("owner_id") or "", []).append(d)
    lks_by_drop: Dict[str, list] = {}
    for l in (lks or {}).values():
        lks_by_drop.setdefault(l.get("drop_id") or "", []).append(l)

    result = []
    for oid, o in (owners or {}).items():
        d_list = drops_by_owner.get(oid, [])
        d_done = [d for d in d_list if d.get("status") == "done"]
        d_pending = [d for d in d_list if d.get("status") in ("pending", "draft")]
        d_accepted = [d for d in d_list if d.get("status") == "accepted"]
        d_brak = [d for d in d_list if d.get("status") == "brak"]
        # Сумма по принятым/закрытым
        total_price = sum(int(d.get("price_usdt") or 0) for d in d_done)
        avg_price = (total_price / len(d_done)) if d_done else 0
        # Последняя активность
        last_ts = max(
            (d.get("created_at") or d.get("accept_ts") or 0 for d in d_list),
            default=0,
        )
        # LK счёт
        lks_total = sum(len(lks_by_drop.get(d.get("drop_id"), [])) for d in d_list)
        result.append({
            "owner_id": oid,
            "tg_user_id": o.get("tg_user_id"),
            "username": o.get("username"),
            "name": o.get("name"),
            "work_chat_id": o.get("work_chat_id"),
            "banned_until": o.get("banned_until") or 0,
            "warnings": o.get("warnings") or 0,
            "drops_total": len(d_list),
            "drops_pending": len(d_pending),
            "drops_accepted": len(d_accepted),
            "drops_done": len(d_done),
            "drops_brak": len(d_brak),
            "lks_total": lks_total,
            "total_price_done": total_price,
            "avg_price_done": round(avg_price, 2),
            "last_activity": last_ts,
        })
    result.sort(key=lambda x: x["last_activity"] or 0, reverse=True)
    return {"owners": result, "total": len(result)}


@app.get("/api/crm/drops")
async def api_crm_drops(
    owner_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 300,
    _: None = Depends(_auth),
):
    """Список дропов (клиентов) CRM."""
    storage.reload_sync()
    if not hasattr(storage, "list_crm_drops"):
        return {"drops": [], "total": 0}
    drops = storage.list_crm_drops(owner_id=owner_id) or {}
    result = []
    for did, d in drops.items():
        if status_filter and d.get("status") != status_filter:
            continue
        owner = (storage.get_crm_owner(d.get("owner_id", "")) or {}) \
            if hasattr(storage, "get_crm_owner") else {}
        lk_count = 0
        try:
            lk_count = len(storage.list_crm_drop_lks(drop_id=did) or {})
        except Exception:
            pass
        result.append({
            "drop_id": did,
            "owner_id": d.get("owner_id"),
            "owner_username": owner.get("username"),
            "fio": d.get("fio"),
            "about": (d.get("about") or "")[:200],
            "status": d.get("status"),
            "price_usdt": d.get("price_usdt") or 0,
            "scan_count": len(d.get("scan_file_ids") or []),
            "lk_count": lk_count,
            "lk_card_ids": d.get("lk_card_ids") or [],
            "social": d.get("social"),
            "residence": d.get("residence"),
            "other_banks": d.get("other_banks"),
            "created_at": d.get("created_at") or 0,
            "accept_ts": d.get("accept_ts") or 0,
            "done_ts": d.get("done_ts") or 0,
        })
    result.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    return {"drops": result[:limit], "total": len(result)}


@app.get("/api/crm/drop_lks")
async def api_crm_drop_lks(
    drop_id: Optional[str] = None,
    bank: Optional[str] = None,
    limit: int = 500,
    _: None = Depends(_auth),
):
    """Список ЛК банков (CRM)."""
    storage.reload_sync()
    if not hasattr(storage, "list_crm_drop_lks"):
        return {"lks": [], "total": 0}
    lks = storage.list_crm_drop_lks(drop_id=drop_id) or {}
    result = []
    for lid, l in lks.items():
        if bank and bank.lower() not in (l.get("bank") or "").lower():
            continue
        result.append({
            "droplk_id": lid,
            "drop_id": l.get("drop_id"),
            "bank": l.get("bank"),
            "value": l.get("value"),
            "status": l.get("status"),
            "deal": l.get("deal"),
            "new_login": l.get("new_login"),
            "new_mail": l.get("new_mail"),
            "new_number": l.get("new_number"),
            "code_word": l.get("code_word"),
            "ded_location": l.get("ded_location"),
            "ded_ip": l.get("ded_ip"),
            "ded_login": l.get("ded_login"),
            "sms_history": l.get("sms_history") or [],
            "created_at": l.get("created_at") or 0,
        })
    result.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    return {"lks": result[:limit], "total": len(result)}


@app.get("/api/crm/export.csv")
async def api_crm_export_csv(_: None = Depends(_auth)):
    """CSV экспорт: партнёры + дропы + ЛК (один плоский файл)."""
    import csv as _csv
    import io as _io
    from fastapi.responses import StreamingResponse
    storage.reload_sync()
    owners = storage.list_crm_owners() if hasattr(storage, "list_crm_owners") else {}
    drops = storage.list_crm_drops() if hasattr(storage, "list_crm_drops") else {}
    lks_all = storage.list_crm_drop_lks() if hasattr(storage, "list_crm_drop_lks") else {}
    buf = _io.StringIO()
    w = _csv.writer(buf, delimiter=";")
    w.writerow([
        "owner_username", "owner_name", "drop_id", "fio", "drop_status",
        "drop_price", "lk_bank", "lk_value", "lk_status", "lk_deal",
        "new_login", "ded_location", "ded_ip", "created_at",
    ])
    # Группируем ЛК по drop
    lks_by_drop: Dict[str, list] = {}
    for l in (lks_all or {}).values():
        lks_by_drop.setdefault(l.get("drop_id") or "", []).append(l)
    for did, d in (drops or {}).items():
        owner = (owners or {}).get(d.get("owner_id") or "") or {}
        d_lks = lks_by_drop.get(did, [])
        if not d_lks:
            w.writerow([
                owner.get("username") or "", owner.get("name") or "",
                did, d.get("fio") or "", d.get("status") or "",
                d.get("price_usdt") or 0,
                "", "", "", "", "", "", "", d.get("created_at") or "",
            ])
        for l in d_lks:
            w.writerow([
                owner.get("username") or "", owner.get("name") or "",
                did, d.get("fio") or "", d.get("status") or "",
                d.get("price_usdt") or 0,
                l.get("bank") or "", l.get("value") or "",
                l.get("status") or "", l.get("deal") or "",
                l.get("new_login") or "", l.get("ded_location") or "",
                l.get("ded_ip") or "", l.get("created_at") or "",
            ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=crm_export.csv"},
    )


@app.get("/api/crm/owners/{owner_id}")
async def api_crm_owner_detail(owner_id: str, _: None = Depends(_auth)):
    """Полная карточка партнёра: профиль + все дропы + ЛК + computed stats."""
    storage.reload_sync()
    if not hasattr(storage, "get_crm_owner"):
        raise HTTPException(404, "CRM not available")
    o = storage.get_crm_owner(owner_id)
    if not o:
        raise HTTPException(404, "Owner not found")
    all_drops = storage.list_crm_drops(owner_id=owner_id) or {}
    all_lks = storage.list_crm_drop_lks() or {}
    lks_by_drop: Dict[str, list] = {}
    for l in all_lks.values():
        lks_by_drop.setdefault(l.get("drop_id") or "", []).append(l)

    drops_full = []
    # Время-до-готового по закрытым дропам
    done_durations = []
    for did, d in all_drops.items():
        drop_lks = lks_by_drop.get(did, [])
        drops_full.append({
            "drop_id": did,
            "fio": d.get("fio"),
            "about": d.get("about") or "",
            "status": d.get("status"),
            "price_usdt": d.get("price_usdt") or 0,
            "lk_count": len(drop_lks),
            "lks": [
                {
                    "droplk_id": l.get("droplk_id"),
                    "bank": l.get("bank"),
                    "status": l.get("status"),
                    "deal": l.get("deal"),
                    "value": l.get("value"),
                } for l in drop_lks
            ],
            "social": d.get("social"),
            "residence": d.get("residence"),
            "other_banks": d.get("other_banks"),
            "scan_count": len(d.get("scan_file_ids") or []),
            "created_at": d.get("created_at") or 0,
            "accept_ts": d.get("accept_ts") or 0,
            "done_ts": d.get("done_ts") or 0,
            "last_remind_ts": d.get("last_remind_ts") or 0,
        })
        # Длительность для done
        if d.get("status") == "done" and d.get("accept_ts") and d.get("done_ts"):
            done_durations.append(int(d["done_ts"] - d["accept_ts"]))

    drops_full.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    # Stats
    by_status = {}
    for d in drops_full:
        s = d["status"] or "?"
        by_status[s] = by_status.get(s, 0) + 1
    total_revenue = sum(d["price_usdt"] for d in drops_full if d["status"] == "done")
    avg_revenue = (total_revenue / by_status.get("done", 1)) if by_status.get("done") else 0
    avg_time_to_done_h = (sum(done_durations) / len(done_durations) / 3600) if done_durations else 0
    completion_rate = (by_status.get("done", 0) / max(1, len(drops_full))) * 100

    return {
        "owner": {
            "owner_id": owner_id,
            "tg_user_id": o.get("tg_user_id"),
            "username": o.get("username"),
            "name": o.get("name"),
            "work_chat_id": o.get("work_chat_id"),
            "joined_at": o.get("joined_at") or 0,
            "last_active_ts": o.get("last_active_ts") or 0,
            "banned_until": o.get("banned_until") or 0,
            "warnings": o.get("warnings") or 0,
            "rating": o.get("rating") or 0,
        },
        "drops": drops_full,
        "stats": {
            "by_status": by_status,
            "total_drops": len(drops_full),
            "total_revenue_usdt": total_revenue,
            "avg_revenue_usdt": round(avg_revenue, 2),
            "avg_time_to_done_hours": round(avg_time_to_done_h, 1),
            "completion_rate_pct": round(completion_rate, 1),
        },
    }


@app.post("/api/crm/owners/{owner_id}/ban")
async def api_crm_owner_ban(
    owner_id: str,
    days: int = 7,
    _: None = Depends(_auth),
    _perm: bool = Depends(require_action("crm_ban")),
):
    """Бан партнёра до now+days."""
    storage.reload_sync()
    if not hasattr(storage, "get_crm_owner"):
        raise HTTPException(404, "CRM not available")
    owner = storage.get_crm_owner(owner_id)
    if not owner:
        raise HTTPException(404, "Owner not found")
    until_ts = time.time() + max(1, int(days)) * 86400
    if hasattr(storage, "update_crm_owner"):
        await storage.update_crm_owner(owner_id, banned_until=until_ts)
    else:
        # fallback — прямо в state
        storage.state.setdefault("crm_owners", {})
        if owner_id in storage.state["crm_owners"]:
            storage.state["crm_owners"][owner_id]["banned_until"] = until_ts
            await storage._save_unlocked()
    return {"ok": True, "owner_id": owner_id, "banned_until": until_ts}


@app.post("/api/crm/owners/{owner_id}/unban")
async def api_crm_owner_unban(owner_id: str, _: None = Depends(_auth), _perm: bool = Depends(require_action("crm_unban"))):
    """Снять бан."""
    storage.reload_sync()
    if not hasattr(storage, "get_crm_owner"):
        raise HTTPException(404, "CRM not available")
    if hasattr(storage, "update_crm_owner"):
        await storage.update_crm_owner(owner_id, banned_until=0)
    else:
        storage.state.setdefault("crm_owners", {})
        if owner_id in storage.state["crm_owners"]:
            storage.state["crm_owners"][owner_id]["banned_until"] = 0
            await storage._save_unlocked()
    return {"ok": True, "owner_id": owner_id}


@app.post("/api/crm/owners/{owner_id}/warn")
async def api_crm_owner_warn(owner_id: str, _: None = Depends(_auth), _perm: bool = Depends(require_action("crm_warn"))):
    """+1 предупреждение."""
    storage.reload_sync()
    if not hasattr(storage, "get_crm_owner"):
        raise HTTPException(404, "CRM not available")
    owner = storage.get_crm_owner(owner_id)
    if not owner:
        raise HTTPException(404, "Owner not found")
    warns = int(owner.get("warnings") or 0) + 1
    if hasattr(storage, "update_crm_owner"):
        await storage.update_crm_owner(owner_id, warnings=warns)
    else:
        storage.state.setdefault("crm_owners", {})
        if owner_id in storage.state["crm_owners"]:
            storage.state["crm_owners"][owner_id]["warnings"] = warns
            await storage._save_unlocked()
    return {"ok": True, "owner_id": owner_id, "warnings": warns}


# === Sparklines (per-day series for any metric) ===

@app.get("/api/sparkline")
async def api_sparkline(
    metric: str,
    days: int = 7,
    _: None = Depends(_auth),
):
    """Возвращает временной ряд по дням для метрики.
    Поддерживаемые метрики:
      funnel.starts, funnel.chats_created, funnel.rs_handed, ...
      margin           — маржа V2 по дням
      ai_replies       — приближённо (только сегодняшний total)
    """
    storage.reload_sync()
    today = datetime.now()
    out = []

    if metric.startswith("funnel."):
        key = metric.split(".", 1)[1]
        rows = storage.get_funnel_range(max(1, min(days, 60)))
        for r in reversed(rows):
            out.append({"date": r["date"], "value": float(r.get(key, 0) or 0)})
    elif metric == "margin":
        for i in range(max(1, min(days, 60)) - 1, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            apps = storage.get_applications_v2(d) or []
            day_margin = sum(
                float(a.get("computed", {}).get("margin_usdt", 0) or 0)
                for a in apps
            )
            out.append({"date": d, "value": day_margin})
    elif metric == "lk_created":
        # Аппроксимация: считаем карточки по created_at дню
        from collections import defaultdict
        by_day = defaultdict(int)
        cards = storage.list_lk_cards() or {}
        for c in cards.values():
            ts = c.get("created_at") or 0
            if ts:
                d = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                by_day[d] += 1
        for i in range(max(1, min(days, 60)) - 1, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"date": d, "value": float(by_day.get(d, 0))})
    else:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric}")

    return {"metric": metric, "points": out}


# === Anomaly detection (простая эвристика) ===

@app.get("/api/anomalies")
async def api_anomalies(_: None = Depends(_auth)):
    """Простой anomaly-детектор. Возвращает список аномалий с severity."""
    storage.reload_sync()
    anomalies = []
    stats = storage.state.get("ai_stats", {}) or {}
    err_total = int(stats.get("errors_total", 0) or 0)
    rep_total = int(stats.get("replies_total", 0) or 0)
    if rep_total > 0 and err_total / max(1, rep_total + err_total) > 0.2:
        anomalies.append({
            "code": "ai_error_rate",
            "severity": "warning",
            "message": f"AI error rate {err_total}/{rep_total + err_total} > 20%",
        })
    # Скоро будет: 1) AI silent suppressed бьёт > 10 в час → warning
    cards = storage.list_lk_cards() or {}
    blocks = sum(1 for c in cards.values() if (c.get("status") or "").upper() == "БЛОК")
    if blocks >= 5:
        anomalies.append({
            "code": "lk_blocks",
            "severity": "warning",
            "message": f"{blocks} карточек в БЛОКЕ — требует внимания",
        })
    return {"anomalies": anomalies}


# === Control endpoints (POST — изменения состояния) ===

class ChatSilentReq(BaseModel):
    chat_id: int
    minutes: int = 30


class AIToggleReq(BaseModel):
    enabled: bool


class LKStatusReq(BaseModel):
    new_status: str


class LKUpdateReq(BaseModel):
    """Редактируемые поля карточки ЛК через дашборд."""
    bank: Optional[str] = None
    fio: Optional[str] = None
    price_usdt: Optional[float] = None
    payment_method: Optional[str] = None
    deal_id: Optional[str] = None
    usdt_address: Optional[str] = None
    supplier: Optional[str] = None
    client_username: Optional[str] = None
    work_chat_id: Optional[int] = None
    block_amount_rub: Optional[float] = None
    block_note: Optional[str] = None
    brak_reason: Optional[str] = None


@app.post("/api/control/ai_toggle")
async def control_ai_toggle(req: AIToggleReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("ai_toggle"))):
    """Включить/выключить AI глобально."""
    storage.reload_sync()
    try:
        await storage.set_ai_enabled(bool(req.enabled))
    except Exception as e:
        logger.warning("ai_toggle save failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    try:
        event_bus.emit_event(
            "dashboard-ai-toggle",
            {"enabled": bool(req.enabled)},
            severity="warning",
        )
    except Exception:
        pass
    return {"ok": True, "ai_enabled": bool(req.enabled)}


@app.post("/api/control/lk/{card_id}/status")
async def control_lk_status(
    card_id: str, req: LKStatusReq, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("lk_status_change")),
):
    """Сменить статус карточки ЛК."""
    storage.reload_sync()
    card_id = card_id.lower().lstrip("#")
    allowed = {
        "В_РАБОТЕ", "ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
        "ОЖИДАЕТ_ПОПОЛНЕНИЯ",  # для GUARANTOR_BEFORE: создана сделка, ждём наше пополнение
        "ЗАВЕРШЁН", "ЗАВЕРШЕН", "БРАК", "БЛОК",
        "БЛОК_БЕЗ_ОТРАБОТКИ",
    }
    new_status = (req.new_status or "").strip().upper()
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"new_status must be one of {sorted(allowed)}",
        )

    # ВАЖНО: payment_method НИКОГДА не меняется при смене статуса.
    # Метод оплаты фиксируется один раз клиентом (через AI-tool set_payment_method)
    # и далее НЕИЗМЕНЕН до конца жизни карточки.
    # ПОПОЛНИТЬ_И_ОТПУСТИТЬ — ПОЛНОЦЕННЫЙ статус (не алиас). Цикл для
    # GUARANTOR_AFTER_WORK: В_РАБОТЕ → ОТРАБОТАН → ПОПОЛНИТЬ_И_ОТПУСТИТЬ → ЗАВЕРШЁН.
    ok = await storage.set_lk_card_status(card_id, new_status, by="dashboard")
    if not ok:
        raise HTTPException(status_code=404, detail="card not found")

    # При выставлении статуса ПОПОЛНИТЬ_И_ОТПУСТИТЬ — карточка должна быть
    # в очереди fund_release. Сразу добавляем (storage.add_payout дедуплицирует).
    if new_status == "ПОПОЛНИТЬ_И_ОТПУСТИТЬ":
        try:
            card = (storage.list_lk_cards() or {}).get(card_id) or {}
            if card:
                await storage.add_payout("fund_release", {
                    "card_id": card_id,
                    "bank": card.get("bank") or "",
                    "fio": card.get("fio") or "",
                    "supplier": card.get("supplier") or "",
                    "work_chat_id": card.get("work_chat_id") or 0,
                    "amount_usdt": float(card.get("price_usdt") or 0),
                    "deal_id": card.get("deal_id") or "",
                })
        except Exception as e:
            logger.warning("auto-enqueue fund_release on status change failed: %s", e)

    try:
        event_bus.emit_event(
            "lk-status-changed",
            {"card_id": card_id, "new_status": new_status, "by": "dashboard"},
        )
    except Exception:
        pass
    # Полная синхронизация: userbot должен обновить анкету в Группе 1 ЛК.
    # Если статус БЛОК_БЕЗ_ОТРАБОТКИ — также запустит side-effects
    # (отмена сделки + уведомление клиента в work_chat + reply работнику).
    try:
        await storage.enqueue_dashboard_command(
            f"__sync_lk_card {card_id}",
            source="dashboard-status-change",
        )
    except Exception:
        pass
    if new_status == "БЛОК_БЕЗ_ОТРАБОТКИ":
        try:
            await storage.enqueue_dashboard_command(
                f"__handle_block_no_work {card_id}",
                source="dashboard-block-no-work",
            )
        except Exception:
            pass
    elif new_status in ("БЛОК", "БРАК", "ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
                        "ОЖИДАЕТ_ПОПОЛНЕНИЯ", "В_РАБОТЕ",
                        "ЗАВЕРШЁН", "ЗАВЕРШЕН"):
        # Простое уведомление клиенту — отдельной командой через юзербот
        try:
            await storage.enqueue_dashboard_command(
                f"__notify_status {card_id} {new_status}",
                source="dashboard-status-change",
            )
        except Exception:
            pass
    return {"ok": True, "card_id": card_id, "new_status": new_status}


@app.get("/api/lk_card/{card_id}")
async def api_lk_card_detail(card_id: str, _: None = Depends(_auth)):
    """Полная информация по карточке ЛК — все поля + история статусов."""
    storage.reload_sync()
    card_id = card_id.lower().lstrip("#")
    c = storage.get_lk_card(card_id) or {}
    if not c:
        raise HTTPException(status_code=404, detail="card not found")
    history = list(c.get("history") or [])
    # Доп. контекст: связанная сделка
    deal = None
    did = (c.get("deal_id") or "").strip().lstrip("#")
    if did:
        d = storage.get_deal(did)
        if d:
            deal = {
                "deal_id": did,
                "status": d.get("status"),
                "amount": d.get("amount"),
                "fee": d.get("fee"),
                "method": d.get("method"),
                "fio": d.get("fio"),
                "bank": d.get("bank"),
                "client_username": d.get("client_username"),
            }
    return {
        "card_id": card_id,
        "supplier": c.get("supplier"),
        "bank": c.get("bank"),
        "fio": c.get("fio"),
        "price_usdt": c.get("price_usdt"),
        "payment_method": c.get("payment_method"),
        "status": c.get("status"),
        "deal_id": c.get("deal_id"),
        "usdt_address": c.get("usdt_address"),
        "client_username": c.get("client_username"),
        "work_chat_id": c.get("work_chat_id"),
        "block_amount_rub": c.get("block_amount_rub"),
        "block_note": c.get("block_note"),
        "brak_reason": c.get("brak_reason"),
        "lk_group_msg_id": c.get("lk_group_msg_id"),
        "post_action_reply_msg_id": c.get("post_action_reply_msg_id"),
        "last_application_id": c.get("last_application_id"),
        "created_at": c.get("created_at"),
        "created_by": (history[0].get("by") if history else None),
        "history": history,
        "deal": deal,
    }


@app.post("/api/control/lk/{card_id}/update")
async def control_lk_update(
    card_id: str, req: LKUpdateReq, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("lk_update")),
):
    """Обновить редактируемые поля карточки ЛК (банк, ФИО, цена,
    метод, deal_id, usdt_address, supplier, …).

    После обновления:
    - emit события `lk-card-updated` для real-time подписчиков
    - enqueue `__sync_lk_card lkNNN` чтобы userbot обновил анкету в Группе 1
    """
    storage.reload_sync()
    card_id = card_id.lower().lstrip("#")
    if not storage.get_lk_card(card_id):
        raise HTTPException(status_code=404, detail="card not found")
    updates: dict = {}
    payload = req.model_dump(exclude_unset=True) if hasattr(req, "model_dump") \
        else req.dict(exclude_unset=True)
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, str):
            v_clean = v.strip()
            if k == "deal_id":
                v_clean = v_clean.lstrip("#")
            if k == "supplier":
                v_clean = v_clean.lstrip("@")
            if k == "client_username":
                v_clean = v_clean.lstrip("@")
            if k == "payment_method":
                v_clean = v_clean.upper()
            updates[k] = v_clean
        else:
            updates[k] = v
    if not updates:
        return {"ok": True, "card_id": card_id, "updated": []}
    ok = await storage.update_lk_card(card_id, **updates)
    if not ok:
        raise HTTPException(status_code=500, detail="update failed")
    try:
        event_bus.emit_event(
            "lk-card-updated",
            {
                "card_id": card_id,
                "fields": list(updates.keys()),
                "by": "dashboard",
            },
        )
    except Exception:
        pass
    # Sync: userbot обновит анкету в Группе 1 ЛК
    try:
        await storage.enqueue_dashboard_command(
            f"__sync_lk_card {card_id}",
            source="dashboard-card-update",
        )
    except Exception:
        pass
    return {"ok": True, "card_id": card_id, "updated": list(updates.keys())}


@app.post("/api/control/lk/{card_id}/delete")
async def control_lk_delete(card_id: str, _: None = Depends(_auth), _perm: bool = Depends(require_action("lk_delete"))):
    """Удалить карточку ЛК."""
    storage.reload_sync()
    card_id = card_id.lower().lstrip("#")
    ok = await storage.delete_lk_card(card_id)
    if not ok:
        raise HTTPException(status_code=404, detail="card not found")
    try:
        event_bus.emit_event(
            "lk-deleted",
            {"card_id": card_id, "by": "dashboard"},
            severity="warning",
        )
    except Exception:
        pass
    return {"ok": True, "card_id": card_id}


class CommandReq(BaseModel):
    text: str


class LeoAskReq(BaseModel):
    text: str
    auto_execute: bool = True


@app.post("/api/leo/realtime_session")
async def leo_realtime_session(request: Request, _: None = Depends(_auth), _perm: bool = Depends(require_action("leo_realtime_session"))):
    """Выдаёт ephemeral client token от OpenAI Realtime API.
    Каждый админ получает СВОЮ независимую сессию — не конфликтуют между собой.

    Требует env OPENAI_API_KEY. Стоимость ~$0.06/мин разговора.
    """
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY не задан в env Railway. Без него Realtime не работает.",
        )
    # Регистрируем активность (best-effort, не должно ломать endpoint)
    try:
        user = _resolve_discord_user(request)
        _record_session_activity(request, "leo-voice-start", {"user": user})
    except Exception as e:
        logger.warning("leo session activity record failed: %s", e)
    # Готовим инструкции с актуальным снимком состояния + knowledge graph.
    # Тяжёлые операции — в thread pool чтобы не блокировать event loop.
    state_snap, knowledge = {}, ""
    try:
        import leo as leo_mod
        try:
            import asyncio as _aio
            loop = _aio.get_running_loop()
            state_snap = await loop.run_in_executor(
                None, leo_mod._build_state_snapshot_cached,
            )
        except Exception as e:
            logger.warning("leo state snapshot failed: %s", e)
            state_snap = {}
        try:
            loop = _aio.get_running_loop()
            knowledge = await loop.run_in_executor(
                None, leo_mod._load_knowledge_summary, 8000,
            )
        except Exception as e:
            logger.warning("leo knowledge load failed: %s", e)
            knowledge = ""
    except Exception as e:
        logger.warning("leo realtime prep totally failed: %s", e)
        state_snap, knowledge = {}, ""

    instructions = (
        "Ты — LEO, голосовой AI-ассистент PRIDE (компания по выкупу российских "
        "расчётных счетов). Тебя зовёт админ через дашборд J.A.R.V.I.S.\n\n"
        "ПРАВИЛА РАЗГОВОРА:\n"
        "- Говори естественно, как живой собеседник. Коротко, по делу.\n"
        "- НЕ зачитывай эмодзи, маркдаун, символы. Только живая речь.\n"
        "- Если просят сделать действие (рассылку, очистку, аудит) — "
        "вызывай функцию execute_command.\n"
        "- Если юзер диктует ВАЖНЫЙ ФАКТ / ПРАВИЛО / ЗАДАЧУ / ИДЕЮ / ПОПРАВКУ "
        "которое должно сохраниться навсегда — ОБЯЗАТЕЛЬНО вызывай функцию "
        "save_to_knowledge_graph. Заметка попадёт в knowledge graph и AI-ассистенты "
        "клиентов сразу её увидят.\n"
        "- Триггеры записи в граф: 'запиши', 'запомни', 'добавь в базу', 'отметь что', "
        "'задача', 'идея', 'правило', 'поправка', а также когда юзер просто "
        "констатирует факт который ассистент должен знать.\n"
        "- После save_to_knowledge_graph коротко подтверди голосом ('записал' / "
        "'добавил в правила' / 'сохранил задачу').\n"
        "- Если спрашивают цифры — используй snapshot ниже.\n"
        "- Если спрашивают правила / прайс / процессы — используй knowledge.\n"
        "- Не выдумывай. Если не знаешь — скажи «не знаю».\n\n"
        f"=== СНИМОК СИСТЕМЫ ===\n{json.dumps(state_snap, ensure_ascii=False)}\n\n"
        f"=== KNOWLEDGE GRAPH (правила, прайс, FAQ) ===\n{knowledge}\n"
    )

    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as cli:
            r = await cli.post(
                "https://api.openai.com/v1/realtime/sessions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17"),
                    "voice": os.getenv("LEO_VOICE", "ash"),  # ash/ballad/coral/sage/verse/alloy/echo/shimmer
                    "instructions": instructions,
                    "modalities": ["audio", "text"],
                    "input_audio_transcription": {"model": "whisper-1"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "silence_duration_ms": 600,
                    },
                    "tools": [
                        {
                            "type": "function",
                            "name": "execute_command",
                            "description": (
                                "Выполнить команду PRIDE через юзербот. Используй когда "
                                "юзер просит сделать что-то: рассылку, аудит, очистку, "
                                "смену статуса, поиск. Примеры команд: 'очисти маржу', "
                                "'аудит', 'рассылка работчатам: текст', '/sync_lk', "
                                "'/operator @timonskupc1', '/find_card Альфа Зоткин'."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "cmd": {
                                        "type": "string",
                                        "description": "Текст команды как для command console",
                                    },
                                },
                                "required": ["cmd"],
                            },
                        },
                        {
                            "type": "function",
                            "name": "search_knowledge_notes",
                            "description": (
                                "Поиск по сохранённым заметкам / правилам / задачам "
                                "в graph PRIDE. Используй когда юзер спрашивает "
                                "'что мы говорили про Альфу', 'какие задачи у нас', "
                                "'была идея про промокод?'. Возвращает до 10 совпадений."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Что искать — ключевое слово/фраза",
                                    },
                                },
                                "required": ["query"],
                            },
                        },
                        {
                            "type": "function",
                            "name": "save_to_knowledge_graph",
                            "description": (
                                "Сохранить факт / правило / задачу в граф знаний PRIDE. "
                                "Используй когда юзер диктует что-то важное что должно "
                                "запомниться навсегда (новое правило, цена, факт о клиенте, "
                                "идея, поправка). Заметка пишется в storage И автоматически "
                                "коммитится в knowledge/leo_brain.md на GitHub — AI-ассистенты "
                                "клиентов сразу видят новые знания.\n\n"
                                "Примеры триггеров (юзер говорит):\n"
                                "  'Запиши: Точка временно не работает' → category=rule\n"
                                "  'Запомни что Иванов наш клиент с прошлого года' → category=fact\n"
                                "  'Задача: перезвонить Тимону завтра' → category=task\n"
                                "  'Идея: добавить промокод для VIP' → category=idea\n"
                                "  'Поправка: Локо теперь стоит 300, не 250' → category=correction\n"
                                "После вызова обязательно скажи юзеру голосом что записал."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "text": {
                                        "type": "string",
                                        "description": "Сам факт/правило/задача — чистым текстом, без 'я записал...'",
                                    },
                                    "category": {
                                        "type": "string",
                                        "enum": ["fact", "rule", "task", "idea",
                                                 "correction", "client", "deal", "pricing"],
                                        "description": "Тип заметки",
                                    },
                                    "priority": {
                                        "type": "string",
                                        "enum": ["normal", "high", "urgent"],
                                        "description": "Срочность — normal по умолчанию",
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Опц. теги, без #. Например ['альфа', 'клиент-иванов']",
                                    },
                                },
                                "required": ["text", "category"],
                            },
                        },
                    ],
                },
            )
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=r.status_code,
                    detail=f"OpenAI Realtime API: {r.text[:300]}",
                )
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class LeoNoteReq(BaseModel):
    text: str
    category: str = "fact"   # fact / rule / task / idea / correction / client / deal / pricing
    priority: str = "normal"  # normal / high / urgent
    tags: list = []
    sync_to_knowledge: bool = True  # сразу writeback в knowledge/*.md


@app.post("/api/leo/note")
async def leo_save_note(req: LeoNoteReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("leo_note_create"))):
    """Принимает заметку от LEO (через голос или текстом из консоли),
    сохраняет в storage.leo_notes + при необходимости коммитит в knowledge graph
    (knowledge/leo_brain.md) через memory.commit_to_knowledge.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    note = await storage.add_leo_note(
        text=text, category=req.category, priority=req.priority,
        source="voice", tags=req.tags,
    )
    knowledge_url = None
    if req.sync_to_knowledge:
        try:
            import memory
            # Куда писать — зависит от категории
            file_map = {
                "pricing": "leo_brain.md",
                "rule":    "leo_brain.md",
                "fact":    "leo_brain.md",
                "task":    "leo_brain.md",
                "idea":    "leo_brain.md",
                "correction": "leo_brain.md",
                "client":  "leo_brain.md",
                "deal":    "leo_brain.md",
            }
            target_file = file_map.get(req.category, "leo_brain.md")
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%Y-%m-%d %H:%M")
            tags_str = " ".join(f"#{t}" for t in (req.tags or []))
            block = (
                f"## [{req.category.upper()}] {ts}"
                + (f" · {req.priority}" if req.priority != "normal" else "")
                + (f"\n{tags_str}" if tags_str else "")
                + f"\n\n{text}\n"
            )
            commit_msg = f"leo-brain: {req.category} — {text[:50]}"
            knowledge_url = await memory.commit_to_knowledge(
                target_file, block, commit_msg, overwrite=False,
            )
            if knowledge_url:
                await storage.mark_leo_note_synced(note["id"], knowledge_url)
        except Exception as e:
            logger.warning("leo_note knowledge sync failed: %s", e)
    try:
        event_bus.emit_event(
            "leo-note", {
                "id": note.get("id"),
                "category": req.category,
                "text": text[:120],
                "synced": bool(knowledge_url),
                "url": knowledge_url or "",
            }, character="leo", severity="success",
        )
    except Exception:
        pass
    return {
        "ok": True, "note": note,
        "knowledge_url": knowledge_url, "synced": bool(knowledge_url),
    }


@app.get("/api/leo/notes")
async def leo_list_notes(
    limit: int = 100, category: Optional[str] = None,
    _: None = Depends(_auth),
):
    storage.reload_sync()
    return {"notes": storage.list_leo_notes(limit=limit, category=category)}


@app.delete("/api/leo/note/{note_id}")
async def leo_delete_note(note_id: int, _: None = Depends(_auth), _perm: bool = Depends(require_action("leo_note_delete"))):
    ok = await storage.delete_leo_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="note not found")
    return {"ok": True}


class LeoNoteSearchReq(BaseModel):
    query: str
    limit: int = 20


@app.post("/api/leo/notes/search")
async def leo_search_notes(req: LeoNoteSearchReq, _: None = Depends(_auth)):
    """Поиск по заметкам — по тексту, тегам, категории."""
    storage.reload_sync()
    q = (req.query or "").lower().strip()
    if not q:
        return {"notes": []}
    notes = storage.list_leo_notes(limit=1000)
    found = []
    for n in notes:
        haystack = " ".join([
            n.get("text") or "", n.get("category") or "",
            " ".join(n.get("tags") or []),
        ]).lower()
        if all(part in haystack for part in q.split()):
            found.append(n)
    return {"notes": found[:max(1, min(req.limit, 100))]}


class MoveNoteReq(BaseModel):
    file: str   # pricing.md / faq.md / policy.md / about.md / deals.md / style.md


@app.post("/api/leo/note/{note_id}/move")
async def leo_move_note(note_id: int, req: MoveNoteReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("leo_note_move"))):
    """Переносит заметку в конкретный knowledge-файл (rules.md / pricing.md и т.п.).
    Из leo_brain.md заметка НЕ удаляется — только добавляется ссылка."""
    storage.reload_sync()
    target = (req.file or "").strip().lower()
    if not target.endswith(".md"):
        target += ".md"
    allowed = {
        "pricing.md", "faq.md", "policy.md", "about.md",
        "deals.md", "style.md", "lk_cards.md", "accounting.md",
        "rules.md", "leo_brain.md",
    }
    if target not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"target file must be one of {sorted(allowed)}",
        )
    # Найдём заметку
    note = None
    for n in storage.list_leo_notes(limit=1000):
        if int(n.get("id", 0)) == int(note_id):
            note = n; break
    if not note:
        raise HTTPException(status_code=404, detail="note not found")

    try:
        import memory
        from datetime import datetime as _dt
        ts = _dt.fromtimestamp(note.get("ts", time.time())).strftime("%Y-%m-%d %H:%M")
        cat = (note.get("category") or "fact").upper()
        tags = " ".join(f"#{t}" for t in (note.get("tags") or []))
        block = (
            f"## [{cat} · LEO] {ts}\n"
            + (tags + "\n\n" if tags else "")
            + (note.get("text") or "")
            + "\n"
        )
        msg = f"leo-move: {cat} → {target} — {(note.get('text') or '')[:50]}"
        url = await memory.commit_to_knowledge(target, block, msg, overwrite=False)
        if not url:
            raise HTTPException(status_code=500, detail="GitHub commit failed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    try:
        event_bus.emit_event(
            "leo-note-moved",
            {"id": note_id, "file": target, "url": url},
            character="leo", severity="success",
        )
    except Exception:
        pass
    return {"ok": True, "url": url, "file": target}


@app.post("/api/leo/notes/archive_old")
async def leo_archive_old(days: int = 30, _: None = Depends(_auth), _perm: bool = Depends(require_action("leo_notes_archive"))):
    """Архивирует заметки старше N дней: переносит в knowledge/leo_archive.md
    и удаляет из storage. Возвращает количество перенесённых."""
    storage.reload_sync()
    cutoff = time.time() - days * 86400
    notes = storage.list_leo_notes(limit=10000)
    old = [n for n in notes if float(n.get("ts", 0)) < cutoff]
    if not old:
        return {"archived": 0, "url": None}
    import memory
    from datetime import datetime as _dt
    lines = [f"# Архив заметок LEO (старше {days} дней)\n"]
    for n in sorted(old, key=lambda x: x.get("ts", 0)):
        ts = _dt.fromtimestamp(n.get("ts", 0)).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"## [{(n.get('category') or '').upper()}] {ts}\n"
            f"{n.get('text') or ''}\n"
        )
    block = "\n".join(lines)
    url = await memory.commit_to_knowledge(
        "leo_archive.md", block,
        f"leo-archive: {len(old)} заметок старше {days} дней",
        overwrite=False,
    )
    if url:
        # Удаляем из storage
        for n in old:
            await storage.delete_leo_note(int(n.get("id", 0)))
    return {"archived": len(old), "url": url}


# ============== OUTREACH (рассылочный отдел) ==============

class OutreachAuthStartReq(BaseModel):
    phone: str


class OutreachAuthConfirmReq(BaseModel):
    phone: str
    code: str
    password: str = ""  # для 2FA


@app.post("/api/outreach/bots/auth/start")
async def outreach_auth_start(req: OutreachAuthStartReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_bot_auth_start"))):
    """Шаг 1: запросить SMS-код для нового юзербота."""
    import outreach
    res = await outreach.manager.start_auth(req.phone)
    return res


@app.post("/api/outreach/bots/auth/confirm")
async def outreach_auth_confirm(req: OutreachAuthConfirmReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_bot_auth_confirm"))):
    """Шаг 2: подтвердить SMS-код (+ password если включена 2FA)."""
    import outreach
    res = await outreach.manager.confirm_code(req.phone, req.code, req.password)
    return res


@app.get("/api/outreach/bots")
async def outreach_list_bots(_: None = Depends(_auth)):
    storage.reload_sync()
    bots = storage.list_outreach_bots()
    # Не отдаём session_string наружу
    safe = [{k: v for k, v in b.items() if k != "session_string"} for b in bots]
    return {"bots": safe}


@app.delete("/api/outreach/bots/{bot_id}")
async def outreach_delete_bot(bot_id: int, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_bot_delete"))):
    import outreach
    await outreach.manager.disconnect_bot(bot_id)
    ok = await storage.delete_outreach_bot(bot_id)
    if not ok:
        raise HTTPException(status_code=404, detail="bot not found")
    return {"ok": True}


class CampaignReq(BaseModel):
    name: str
    text: str
    targets: list  # список chat_ids (числа или @username каналов)
    manager_username: str
    rate_per_hour: int = 20
    jitter_min_sec: int = 90
    jitter_max_sec: int = 240
    active_hours_from: int = 9
    active_hours_to: int = 21


class CampaignPatchReq(BaseModel):
    name: Optional[str] = None
    text: Optional[str] = None
    targets: Optional[list] = None
    manager_username: Optional[str] = None
    rate_per_hour: Optional[int] = None
    jitter_min_sec: Optional[int] = None
    jitter_max_sec: Optional[int] = None
    active_hours_from: Optional[int] = None
    active_hours_to: Optional[int] = None


@app.get("/api/outreach/campaigns")
async def outreach_list_campaigns(_: None = Depends(_auth)):
    storage.reload_sync()
    return {"campaigns": storage.list_outreach_campaigns()}


@app.post("/api/outreach/campaigns")
async def outreach_create_campaign(req: CampaignReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_campaign_create"))):
    entry = await storage.add_outreach_campaign(**req.dict())
    return {"ok": True, "campaign": entry}


@app.patch("/api/outreach/campaigns/{campaign_id}")
async def outreach_patch_campaign(
    campaign_id: int, req: CampaignPatchReq, _: None = Depends(_auth),
    _perm: bool = Depends(require_action("outreach_campaign_update")),
):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    ok = await storage.update_outreach_campaign(campaign_id, **fields)
    if not ok:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"ok": True}


@app.delete("/api/outreach/campaigns/{campaign_id}")
async def outreach_delete_campaign(campaign_id: int, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_campaign_delete"))):
    import outreach
    await outreach.manager.stop_campaign(campaign_id)
    ok = await storage.delete_outreach_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"ok": True}


@app.post("/api/outreach/campaigns/{campaign_id}/start")
async def outreach_start(campaign_id: int, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_campaign_start"))):
    import outreach
    ok = await outreach.manager.start_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=400, detail="cannot start")
    return {"ok": True}


@app.post("/api/outreach/campaigns/{campaign_id}/pause")
async def outreach_pause(campaign_id: int, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_campaign_pause"))):
    import outreach
    await outreach.manager.pause_campaign(campaign_id)
    return {"ok": True}


@app.post("/api/outreach/campaigns/{campaign_id}/stop")
async def outreach_stop(campaign_id: int, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_campaign_stop"))):
    import outreach
    await outreach.manager.stop_campaign(campaign_id)
    return {"ok": True}


@app.get("/api/outreach/messages")
async def outreach_messages(
    campaign_id: Optional[int] = None, limit: int = 200,
    _: None = Depends(_auth),
):
    storage.reload_sync()
    return {"messages": storage.list_outreach_messages(campaign_id, limit)}


@app.get("/api/outreach/responses")
async def outreach_responses(
    handled: Optional[bool] = None,
    intent: Optional[str] = None,
    limit: int = 200,
    _: None = Depends(_auth),
):
    storage.reload_sync()
    return {"responses": storage.list_outreach_responses(handled, intent, limit)}


@app.post("/api/outreach/responses/{resp_id}/handle")
async def outreach_handle_response(resp_id: int, _: None = Depends(_auth), _perm: bool = Depends(require_action("outreach_response_handle"))):
    """Пометить ответ как обработанный вручную."""
    ok = await storage.mark_outreach_response(resp_id, handled=True)
    if not ok:
        raise HTTPException(status_code=404, detail="response not found")
    return {"ok": True}


@app.post("/api/leo/voice_command")
async def leo_voice_command(req: CommandReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("leo_voice_command"))):
    """Принимает команду от голосового Льва (через OpenAI tool-call) —
    ставит её в очередь userbot."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty cmd")
    entry = await storage.enqueue_dashboard_command(text, source="leo-voice")
    try:
        event_bus.emit_event(
            "leo-voice-cmd",
            {"cmd": text[:160], "id": entry.get("id")},
            character="leo", severity="info",
        )
    except Exception:
        pass
    return {"ok": True, "command": entry}


@app.post("/api/leo/ask")
async def api_leo_ask(req: LeoAskReq, _: None = Depends(_auth), _perm: bool = Depends(require_action("leo_ask"))):
    """LEO — умный AI агент. Принимает свободный текст, отвечает + при
    необходимости автоматически ставит команды в очередь userbot для
    выполнения."""
    try:
        import leo
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"leo not available: {e}")
    res = await leo.ask(req.text)
    executed = []
    if req.auto_execute and res.get("actions"):
        for act in res["actions"]:
            cmd_text = leo.tool_to_command_text(act["tool"], act.get("args", {}))
            if cmd_text:
                entry = await storage.enqueue_dashboard_command(cmd_text, source="leo")
                executed.append({"tool": act["tool"], "cmd": cmd_text, "id": entry.get("id")})
    try:
        event_bus.emit_event(
            "leo-ask",
            {"text": req.text[:120], "reply": res.get("reply", "")[:200],
             "actions": len(res.get("actions") or [])},
            character="leo",
            severity="info",
        )
    except Exception:
        pass
    return {
        "reply": res.get("reply", ""),
        "actions": res.get("actions") or [],
        "executed": executed,
        "usage": res.get("usage") or {},
    }


@app.post("/api/commands")
async def api_command_enqueue(
    req: CommandReq, request: Request, _: None = Depends(_auth),
):
    """Очередь команд для userbot — дашборд кидает текстовую команду,
    userbot её подберёт и выполнит."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    user = _resolve_discord_user(request)
    entry = await storage.enqueue_dashboard_command(
        text, source=f"dashboard:{user}",
    )
    _record_session_activity(request, "command", {"text": text[:120], "cmd_id": entry.get("id")})
    try:
        event_bus.emit_event(
            "dashboard-command-enqueued",
            payload={"id": entry.get("id"), "text": text[:120]},
            character="dashboard",
        )
    except Exception:
        pass
    return {"ok": True, "id": entry.get("id")}


@app.get("/api/commands")
async def api_command_list(limit: int = 30, _: None = Depends(_auth)):
    storage.reload_sync()
    items = list(storage.state.get("dashboard_commands") or [])
    items.sort(key=lambda x: -(x.get("ts") or 0))
    return {"commands": items[:limit]}


@app.get("/api/control/info")
async def control_info(_: None = Depends(_auth)):
    storage.reload_sync()
    return {
        "ai_enabled": storage.is_ai_enabled(),
        "writeback_enabled": storage.is_writeback_enabled(),
        "lk_group_id": storage.get_lk_group_id(),
        "accounting_group_id": storage.get_accounting_group_id(),
    }


@app.websocket("/ws")
async def websocket_events(ws: WebSocket):
    authed = False
    cookie_val = ws.cookies.get(SESSION_COOKIE)
    if cookie_val:
        uid = _verify_session(cookie_val)
        if uid is not None and (not TG_ADMINS or uid in TG_ADMINS):
            authed = True
    if not authed:
        user = ws.query_params.get("user", "")
        pwd = ws.query_params.get("pass", "")
        if DASHBOARD_PASS and user and pwd:
            if (
                secrets.compare_digest(user, DASHBOARD_USER)
                and secrets.compare_digest(pwd, DASHBOARD_PASS)
            ):
                authed = True
    if not authed:
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    stop_event = asyncio.Event()
    async def broadcast_loop():
        try:
            async for event in event_bus.subscribe(replay_last=20):
                if stop_event.is_set():
                    break
                try:
                    await ws.send_json({"kind": "event", **event})
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
    broadcast_task = asyncio.create_task(broadcast_loop())
    try:
        while True:
            msg = await ws.receive_json()
            cmd = msg.get("cmd", "")
            if cmd == "ping":
                await ws.send_json({"kind": "pong", "ts": datetime.now().isoformat()})
            elif cmd == "state":
                storage.reload_sync()
                await ws.send_json({
                    "kind": "state", "ai_enabled": storage.is_ai_enabled(),
                    "subscribers": event_bus.subscriber_count(),
                })
            else:
                await ws.send_json({"kind": "ack", "cmd": cmd})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("ws error: %s", e)
    finally:
        stop_event.set()
        broadcast_task.cancel()


@app.get("/api/events/stream")
async def api_events_stream(request: Request, _: None = Depends(_auth)):
    async def generator():
        try:
            async for event in event_bus.subscribe(replay_last=80):
                if await request.is_disconnected():
                    break
                payload = json.dumps(event, default=str, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("SSE stream error: %s", e)
    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =====================================================================
# NOTIFICATIONS — алерты для JARVIS + TG личка owner-у
# =====================================================================

@app.get("/api/notifications/poll")
async def api_notifications_poll(
    request: Request,
    since: float = 0,
    unread_only: bool = True,
    limit: int = 50,
    me: dict = Depends(_get_me),
):
    """Polling endpoint для bell-иконки в JARVIS. Возвращает unread нотификации."""
    role = me.get("role") or ""
    user_id = int(me.get("tg_user_id") or 0) or None
    notifs = storage.list_notifications(
        role=role, user_id=user_id,
        since_ts=float(since or 0), unread_only=bool(unread_only),
        limit=int(limit),
    )
    return {"ok": True, "notifications": notifs, "count": len(notifs)}


@app.post("/api/notifications/{notif_id}/read")
async def api_notification_mark_read(notif_id: int, me: dict = Depends(_get_me)):
    ok = await storage.mark_notification_read(int(notif_id))
    return {"ok": ok, "id": notif_id}


@app.post("/api/notifications/read_all")
async def api_notifications_mark_all_read(me: dict = Depends(_get_me)):
    role = me.get("role") or ""
    user_id = int(me.get("tg_user_id") or 0) or None
    count = await storage.mark_all_notifications_read(role=role, user_id=user_id)
    return {"ok": True, "marked_read": count}

# =====================================================================
# AD CAMPAIGNS — рекламные кампании, выплаты менеджерам рекламодателей
# =====================================================================

@app.get("/api/ads/campaigns")
async def api_ads_list(status: str = None, me: dict = Depends(_get_me)):
    """Список кампаний (опц фильтр по статусу)."""
    return {"ok": True, "campaigns": storage.list_ad_campaigns(status=status)}


@app.post("/api/ads/campaigns/create")
async def api_ads_create(request: Request, me: dict = Depends(_get_me)):
    """Создать кампанию. Body: {manager_username, amount_usdt, platform?, note?, date_start?, date_end?, usdt_address?}"""
    if (me.get("role") or "") not in ("owner", "accounting"):
        raise HTTPException(403, "owner/accounting only")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    entry = await storage.add_ad_campaign(
        manager_username=body.get("manager_username", ""),
        amount_usdt=float(body.get("amount_usdt") or 0),
        usdt_address=body.get("usdt_address", ""),
        platform=body.get("platform", ""),
        note=body.get("note", ""),
        date_start=body.get("date_start", ""),
        date_end=body.get("date_end", ""),
    )
    # Notify owner — нужно подтверждение
    try:
        await storage.add_notification(
            type="warning",
            text=f"📢 Новая реклама #{entry['id']}: {entry['amount_usdt']}$ @{entry['manager_username']} ({entry.get('platform', '?')})",
            action_url="accounting",
        )
    except Exception:
        pass
    return {"ok": True, "campaign": entry}


@app.post("/api/ads/campaigns/{cid}/set_address")
async def api_ads_set_address(cid: int, request: Request, me: dict = Depends(_get_me)):
    """Установить USDT адрес для кампании."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    address = (body.get("usdt_address") or "").strip()
    try:
        from tron_payouts import validate_tron_address
        if not validate_tron_address(address):
            raise HTTPException(400, "Невалидный Tron-адрес")
    except ImportError:
        pass  # tron_payouts ещё не установлен в env
    ok = await storage.update_ad_campaign(cid, usdt_address=address, status="awaiting_approval")
    if not ok:
        raise HTTPException(404, "campaign not found")
    # === AUTO-PAY если сумма в лимите + auto_pay_ads_enabled ===
    safety = storage.get_payout_safety()
    camp = storage.get_ad_campaign(cid)
    if (safety.get("auto_pay_enabled_global", True) and
        safety.get("auto_pay_ads_enabled", True) and
        float(camp.get("amount_usdt", 0)) <= float(safety.get("max_per_tx_usdt") or 500)):
        # Авто-вызываем approve_and_pay (но без owner check т.к. это автомат)
        try:
            from tron_payouts import is_configured, send_usdt_to
            if is_configured():
                daily_used = storage.get_daily_outbound_total()
                if daily_used + float(camp["amount_usdt"]) <= float(safety.get("max_daily_usdt") or 2000):
                    result = await send_usdt_to(
                        to_address=address,
                        amount_usdt=float(camp["amount_usdt"]),
                        reason=f"ad #{cid} auto-pay {camp.get('platform', '')}",
                        wait_confirmation=False,
                    )
                    if result.get("ok"):
                        tx_hash = result.get("tx_hash", "")
                        await storage.update_ad_campaign(cid, status="paid", tx_hash=tx_hash, approved_by="auto")
                        try:
                            await storage.add_accounting_entry(
                                category="ads", amount_usdt=float(camp["amount_usdt"]),
                                note=f"auto-ad #{cid} @{camp['manager_username']} tx:{tx_hash[:16]}",
                                created_by="auto:set_address",
                            )
                            await storage.add_notification(
                                type="success",
                                text=f"📢💸 Реклама #{cid} АВТО-оплачена: {camp['amount_usdt']}$ → @{camp['manager_username']}",
                            )
                        except Exception:
                            pass
                        return {"ok": True, "auto_paid": True, "tx_hash": tx_hash}
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("ads auto-pay failed: %s", e)
    return {"ok": True, "auto_paid": False}


@app.post("/api/ads/campaigns/{cid}/approve_and_pay")
async def api_ads_approve_pay(cid: int, me: dict = Depends(_get_me)):
    """Approve + autopay через tron_payouts. Owner-only."""
    if (me.get("role") or "") != "owner":
        raise HTTPException(403, "owner only")
    camp = storage.get_ad_campaign(cid)
    if not camp:
        raise HTTPException(404, "campaign not found")
    if camp.get("status") == "paid":
        return {"ok": True, "already_paid": True, "tx_hash": camp.get("tx_hash")}
    if not camp.get("usdt_address"):
        raise HTTPException(400, "Нет USDT адреса — сначала /set_address")
    # Approve
    await storage.update_ad_campaign(cid, status="approved", approved_by=me.get("username") or "")
    # Safety checks
    safety = storage.get_payout_safety()
    if not safety.get("auto_pay_enabled_global", True):
        return {"ok": False, "error": "auto-pay disabled (kill-switch)"}
    if not safety.get("auto_pay_ads_enabled", True):
        return {"ok": False, "error": "auto-pay-ads disabled"}
    if float(camp["amount_usdt"]) > float(safety.get("max_per_tx_usdt") or 500):
        return {"ok": False, "error": f"amount > max_per_tx ({safety.get('max_per_tx_usdt')} USDT) — выплати руками"}
    daily_used = storage.get_daily_outbound_total()
    if daily_used + float(camp["amount_usdt"]) > float(safety.get("max_daily_usdt") or 2000):
        return {"ok": False, "error": f"daily limit exceeded ({daily_used:.2f}+ )"}
    # Pay
    try:
        from tron_payouts import is_configured, send_usdt_to
        if not is_configured():
            await storage.update_ad_campaign(cid, status="awaiting_payment_manual")
            return {"ok": False, "error": "TRON не сконфигурирован — выплати вручную"}
        result = await send_usdt_to(
            to_address=camp["usdt_address"],
            amount_usdt=float(camp["amount_usdt"]),
            reason=f"ad campaign #{cid} {camp.get('platform', '')}",
            wait_confirmation=True,
        )
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "send failed")}
        tx_hash = result.get("tx_hash", "")
        await storage.update_ad_campaign(cid, status="paid", tx_hash=tx_hash)
        # Auto-acc запись (ads)
        try:
            await storage.add_accounting_entry(
                category="ads",
                amount_usdt=float(camp["amount_usdt"]),
                note=f"ad #{cid} @{camp['manager_username']} {camp.get('platform', '')} tx:{tx_hash[:16]}...",
                created_by=f"auto:{me.get('username') or 'owner'}",
            )
        except Exception:
            pass
        try:
            await storage.add_notification(
                type="success",
                text=f"📢💸 Реклама #{cid} оплачена: {camp['amount_usdt']}$ → @{camp['manager_username']}",
            )
        except Exception:
            pass
        return {"ok": True, "tx_hash": tx_hash, "confirmed": result.get("confirmed")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

# =====================================================================
# AUTO-PAY: compensation rules + ручной триггер для payout-runner
# =====================================================================

@app.get("/api/payouts/compensation")
async def api_comp_list(me: dict = Depends(_get_me)):
    """Список всех правил компенсации работников + их pending."""
    if (me.get("role") or "") not in ("owner", "accounting"):
        raise HTTPException(403, "owner/accounting only")
    comps = storage.list_worker_compensations()
    out = []
    for username, rules in comps.items():
        out.append({
            "username": username, "rules": rules,
            "pending_usdt": storage.get_worker_pending(username),
            "usdt_address": storage.get_worker_usdt_address(username) or "",
        })
    return {"ok": True, "workers": out}


@app.post("/api/payouts/compensation/{username}/set")
async def api_comp_set(username: str, request: Request, me: dict = Depends(_get_me)):
    """Body: {rate_per_lk_usdt?, monthly_base_usdt?, pct_of_lk_price?, min_payout_amount?, auto_pay_enabled?}"""
    if (me.get("role") or "") != "owner":
        raise HTTPException(403, "owner only")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    rules = await storage.set_worker_compensation(username, **body)
    return {"ok": True, "username": username.lstrip("@").lower(), "rules": rules}


@app.get("/api/payouts/safety")
async def api_safety_get(me: dict = Depends(_get_me)):
    if (me.get("role") or "") != "owner":
        raise HTTPException(403, "owner only")
    return {"ok": True, "safety": storage.get_payout_safety(),
            "daily_outbound": storage.get_daily_outbound_total()}


@app.post("/api/payouts/safety/set")
async def api_safety_set(request: Request, me: dict = Depends(_get_me)):
    if (me.get("role") or "") != "owner":
        raise HTTPException(403, "owner only")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    new = await storage.set_payout_safety(**body)
    return {"ok": True, "safety": new}


@app.post("/api/payouts/run_salaries")
async def api_run_salaries(me: dict = Depends(_get_me)):
    """Ручной запуск авто-выплаты зарплат прямо сейчас (для тестирования).
    Обычно scheduler в reminder loop сам это делает каждые N часов."""
    if (me.get("role") or "") != "owner":
        raise HTTPException(403, "owner only")
    try:
        from auto_payouts_runner import run_salary_payouts
        result = await run_salary_payouts(reason="manual trigger by owner")
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(500, f"runner failed: {e}")

@app.post("/api/outkup/orders/{order_id}/auto_pay")
async def api_outkup_autopay(
    order_id: str, request: Request,
    me: dict = Depends(_get_me),
):
    """Авто-отправка USDT клиенту после approval откупщика.
    Body: {client_usdt_address: str}
    Safety: max_per_tx / max_daily / kill-switch.
    После успешной отправки → outkup completed + acc запись."""
    if (me.get("role") or "") not in ("owner", "outkup_specialist"):
        raise HTTPException(403, "owner/outkup_specialist only")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    address = (body.get("client_usdt_address") or "").strip()
    if not address:
        raise HTTPException(400, "client_usdt_address required")
    orders = storage.list_outkup_orders() or {}
    o = orders.get(order_id) or {}
    if not o:
        raise HTTPException(404, "order not found")
    rub = float(o.get("amount_rub") or 0)
    rate = float(o.get("rate_rub_per_usdt") or 100)
    if rub <= 0 or rate <= 0:
        raise HTTPException(400, "invalid order amount/rate")
    usdt_amount = rub / rate

    # Safety
    safety = storage.get_payout_safety()
    if not safety.get("auto_pay_enabled_global", True) or not safety.get("auto_pay_outkup_enabled", True):
        raise HTTPException(403, "auto-pay outkup disabled")
    if usdt_amount > float(safety.get("max_per_tx_usdt") or 500):
        raise HTTPException(400, f"amount {usdt_amount:.2f}$ > max_per_tx")
    daily_used = storage.get_daily_outbound_total()
    if daily_used + usdt_amount > float(safety.get("max_daily_usdt") or 2000):
        raise HTTPException(400, "daily limit exceeded")

    try:
        from tron_payouts import is_configured, send_usdt_to, validate_tron_address
        if not is_configured():
            raise HTTPException(500, "TRON not configured")
        if not validate_tron_address(address):
            raise HTTPException(400, f"Invalid Tron address: {address}")
        result = await send_usdt_to(
            to_address=address,
            amount_usdt=usdt_amount,
            reason=f"outkup #{order_id} ({rub} ₽ @ {rate} rate)",
            wait_confirmation=False,
        )
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "send failed")}
        tx_hash = result.get("tx_hash", "")
        # Завершить заказ
        await storage.complete_outkup_order(order_id, txid=tx_hash, by=me.get("username") or "auto")
        # Acc запись (через auto-hook в api_outkup_complete который я добавлял ранее НЕ сработает т.к. это другой путь)
        try:
            await storage.add_accounting_entry(
                category="kassa",
                amount_usdt=usdt_amount,
                amount_rub=rub,
                note=f"outkup #{order_id} auto-pay (rate {rate}, tx {tx_hash[:16]}...)",
                created_by=f"auto:outkup_autopay",
            )
        except Exception:
            pass
        try:
            await storage.add_notification(
                type="success",
                text=f"💱✅ Откуп #{order_id} АВТО-выплачен: {usdt_amount:.2f}$ → {address[:8]}...",
            )
        except Exception:
            pass
        return {"ok": True, "tx_hash": tx_hash, "amount_usdt": usdt_amount}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

# =====================================================================
# TRON wallet info — для финансового UI
# =====================================================================

@app.get("/api/tron/balance")
async def api_tron_balance(me: dict = Depends(_get_me)):
    if (me.get("role") or "") != "owner":
        raise HTTPException(403, "owner only")
    try:
        from tron_payouts import is_configured, get_hot_wallet_balance, get_hot_wallet_address
        if not is_configured():
            return {"ok": False, "error": "TRON not configured"}
        bal = await get_hot_wallet_balance()
        return {"ok": True, "address": get_hot_wallet_address(), "balance": bal}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/tron/outbound")
async def api_tron_outbound(limit: int = 50, me: dict = Depends(_get_me)):
    if (me.get("role") or "") not in ("owner", "accounting"):
        raise HTTPException(403, "owner/accounting only")
    log = storage.list_tron_outbound(limit=int(limit))
    return {"ok": True, "log": log}


# =====================================================================
# OUTKUP QUOTE — расчёт USDT для клиента по RUB
# =====================================================================

@app.get("/api/outkup/quote")
async def api_outkup_quote(amount_rub: float, me: dict = Depends(_get_me)):
    """Возвращает: usdt_to_client (что отправим клиенту), our_margin_usdt (наша маржа)."""
    q = storage.get_outkup_quote(float(amount_rub))
    return {"ok": True, "quote": q}

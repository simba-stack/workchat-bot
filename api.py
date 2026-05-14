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
from typing import Optional

from fastapi import (
    FastAPI, Request, Depends, HTTPException, WebSocket, WebSocketDisconnect,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import event_bus
import config
from storage import storage

logger = logging.getLogger(__name__)

app = FastAPI(title="PRIDE Dashboard", docs_url=None, redoc_url=None)
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
DASHBOARD_DEFAULT = (os.getenv("DASHBOARD_DEFAULT", "jarvis") or "").lower()


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


# === Static & health ===

@app.get("/healthz")
async def healthz():
    """Public healthcheck — без авторизации (нужно для Railway)."""
    return {"status": "ok", "subscribers": event_bus.subscriber_count()}


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


@app.get("/", response_class=HTMLResponse)
async def root(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    # В strict-режиме редиректим на /login если не авторизован
    if _is_strict_telegram() and _try_session_auth(request) is None:
        return RedirectResponse(url="/login", status_code=302)
    _check_auth(request, credentials)
    return HTMLResponse(_load_html(DASHBOARD_DEFAULT))


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
async def api_discord_delete_channel(channel_id: str, _: None = Depends(_auth)):
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
async def control_ai_toggle(req: AIToggleReq, _: None = Depends(_auth)):
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
):
    """Сменить статус карточки ЛК."""
    storage.reload_sync()
    card_id = card_id.lower().lstrip("#")
    allowed = {
        "В_РАБОТЕ", "ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
        "ЗАВЕРШЁН", "ЗАВЕРШЕН", "БРАК", "БЛОК",
        "БЛОК_БЕЗ_ОТРАБОТКИ",
    }
    new_status = (req.new_status or "").strip().upper()
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"new_status must be one of {sorted(allowed)}",
        )
    ok = await storage.set_lk_card_status(card_id, new_status, by="dashboard")
    if not ok:
        raise HTTPException(status_code=404, detail="card not found")
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
async def control_lk_delete(card_id: str, _: None = Depends(_auth)):
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
async def leo_realtime_session(request: Request, _: None = Depends(_auth)):
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
async def leo_save_note(req: LeoNoteReq, _: None = Depends(_auth)):
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
async def leo_delete_note(note_id: int, _: None = Depends(_auth)):
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
async def leo_move_note(note_id: int, req: MoveNoteReq, _: None = Depends(_auth)):
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
async def leo_archive_old(days: int = 30, _: None = Depends(_auth)):
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
async def outreach_auth_start(req: OutreachAuthStartReq, _: None = Depends(_auth)):
    """Шаг 1: запросить SMS-код для нового юзербота."""
    import outreach
    res = await outreach.manager.start_auth(req.phone)
    return res


@app.post("/api/outreach/bots/auth/confirm")
async def outreach_auth_confirm(req: OutreachAuthConfirmReq, _: None = Depends(_auth)):
    """Шаг 2: подтвердить SMS-код (+ password если 2FA)."""
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
async def outreach_delete_bot(bot_id: int, _: None = Depends(_auth)):
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
async def outreach_create_campaign(req: CampaignReq, _: None = Depends(_auth)):
    entry = await storage.add_outreach_campaign(**req.dict())
    return {"ok": True, "campaign": entry}


@app.patch("/api/outreach/campaigns/{campaign_id}")
async def outreach_patch_campaign(
    campaign_id: int, req: CampaignPatchReq, _: None = Depends(_auth),
):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    ok = await storage.update_outreach_campaign(campaign_id, **fields)
    if not ok:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"ok": True}


@app.delete("/api/outreach/campaigns/{campaign_id}")
async def outreach_delete_campaign(campaign_id: int, _: None = Depends(_auth)):
    import outreach
    await outreach.manager.stop_campaign(campaign_id)
    ok = await storage.delete_outreach_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"ok": True}


@app.post("/api/outreach/campaigns/{campaign_id}/start")
async def outreach_start(campaign_id: int, _: None = Depends(_auth)):
    import outreach
    ok = await outreach.manager.start_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=400, detail="cannot start")
    return {"ok": True}


@app.post("/api/outreach/campaigns/{campaign_id}/pause")
async def outreach_pause(campaign_id: int, _: None = Depends(_auth)):
    import outreach
    await outreach.manager.pause_campaign(campaign_id)
    return {"ok": True}


@app.post("/api/outreach/campaigns/{campaign_id}/stop")
async def outreach_stop(campaign_id: int, _: None = Depends(_auth)):
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
async def outreach_handle_response(resp_id: int, _: None = Depends(_auth)):
    """Пометить ответ как обработанный вручную."""
    ok = await storage.mark_outreach_response(resp_id, handled=True)
    if not ok:
        raise HTTPException(status_code=404, detail="response not found")
    return {"ok": True}


@app.post("/api/leo/voice_command")
async def leo_voice_command(req: CommandReq, _: None = Depends(_auth)):
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
async def api_leo_ask(req: LeoAskReq, _: None = Depends(_auth)):
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
    """Очередь команд для userbot. Дашборд отправляет сюда команды
    free-form, userbot опрашивает каждые 5 сек и выполняет.

    Источник команды (source) включает username админа, чтобы можно было
    отличить кто что отправил в общую очередь (важно для tracking)."""
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
            "dashboard-command",
            {"text": text[:200], "cmd_id": entry.get("id"), "by": user},
            severity="info",
        )
    except Exception:
        pass
    return {"ok": True, "command": entry}


@app.get("/api/commands")
async def api_command_list(limit: int = 30, _: None = Depends(_auth)):
    """Список последних команд (для отображения истории в дашборде)."""
    storage.reload_sync()
    return {"commands": storage.list_dashboard_commands(limit=max(1, min(limit, 200)))}


# === Push endpoint for external (AI) integrations ===

API_PUSH_TOKEN = os.getenv("API_PUSH_TOKEN", "")


class PushEvent(BaseModel):
    type: str
    payload: dict = {}
    character: Optional[str] = None
    severity: Optional[str] = "info"


@app.post("/api/push")
async def api_push_event(req: PushEvent, request: Request):
    """Внешний канал — позволяет AI/скриптам пихать события в event_bus
    без затрат на Anthropic. Аутентификация через Bearer token (env
    API_PUSH_TOKEN). Если token не задан — endpoint отключён."""
    if not API_PUSH_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="push disabled — set API_PUSH_TOKEN env",
        )
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    token = auth_header[7:].strip()
    if not secrets.compare_digest(token, API_PUSH_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        event_bus.emit_event(
            req.type, req.payload or {},
            character=req.character or "",
            severity=req.severity or "info",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.post("/api/control/sync_lk_request")
async def control_sync_lk_request(_: None = Depends(_auth)):
    """Просит userbot.py запустить sync_lk_cards. Делаем через event_bus —
    userbot подписывается и реагирует.
    NOTE: реализация требует чтобы userbot слушал event 'request-sync-lk'.
    Альтернатива — запустить вручную команду '/sync_lk' в Группе 1 ЛК.
    """
    try:
        event_bus.emit_event(
            "request-sync-lk",
            {"by": "dashboard", "limit": 1000},
            severity="info",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "ok": True,
        "hint": (
            "Запрос отправлен. Если userbot не подцепит — выполни в Группе 1 "
            "ЛК команду '/sync_lk' или '/sync_lk 1000'."
        ),
    }


@app.get("/api/control/info")
async def control_info(_: None = Depends(_auth)):
    """Текущее состояние управления — чтоб дашборд знал в каком режиме."""
    storage.reload_sync()
    return {
        "ai_enabled": storage.is_ai_enabled(),
        "writeback_enabled": storage.is_writeback_enabled(),
        "lk_group_id": storage.get_lk_group_id(),
        "accounting_group_id": storage.get_accounting_group_id(),
    }


# === WebSocket bidirectional ===

@app.websocket("/ws")
async def websocket_events(ws: WebSocket):
    """WebSocket для двусторонней связи.
    Принимает: ping/команды от дашборда.
    Шлёт: события из event_bus в реальном времени.
    Auth — два варианта:
      1) Cookie `jarvis_session` (после Telegram OAuth) — предпочтительно
      2) ?user=...&pass=... в query string (legacy Basic)
    """
    authed = False
    # 1) Try session cookie
    cookie_val = ws.cookies.get(SESSION_COOKIE)
    if cookie_val:
        uid = _verify_session(cookie_val)
        if uid is not None and (not TG_ADMINS or uid in TG_ADMINS):
            authed = True
    # 2) Try query-string basic
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
                    "kind": "state",
                    "ai_enabled": storage.is_ai_enabled(),
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


# === SSE event stream ===

@app.get("/api/events/stream")
async def api_events_stream(
    request: Request,
    _: None = Depends(_auth),
):
    """Server-Sent Events стрим — push событий из event_bus."""
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
            # nginx/Cloudflare: запретить буферизацию
            "X-Accel-Buffering": "no",
        },
    )

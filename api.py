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
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, StreamingResponse

import event_bus
from storage import storage

logger = logging.getLogger(__name__)

app = FastAPI(title="PRIDE Dashboard", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

_HTML_PATH = Path(__file__).parent / "dashboard" / "index.html"


def _load_html() -> str:
    """Читает HTML дашборда с диска (всегда свежий — удобно для горячих правок)."""
    try:
        return _HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "<!DOCTYPE html><html><head><title>Dashboard</title></head>"
            "<body><h1>Dashboard файл не найден</h1>"
            "<p>Ожидаемый путь: <code>dashboard/index.html</code></p></body></html>"
        )


def _check_auth(credentials: Optional[HTTPBasicCredentials]) -> None:
    """Basic auth check. 503 если пароль не задан в env."""
    if not DASHBOARD_PASS:
        raise HTTPException(
            status_code=503,
            detail=(
                "Dashboard auth not configured. Set DASHBOARD_USER и "
                "DASHBOARD_PASS в Railway → Variables."
            ),
        )
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    pass_ok = secrets.compare_digest(credentials.password, DASHBOARD_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _auth(credentials: HTTPBasicCredentials = Depends(security)):
    _check_auth(credentials)


# === Static & health ===

@app.get("/healthz")
async def healthz():
    """Public healthcheck — без авторизации (нужно для Railway)."""
    return {"status": "ok", "subscribers": event_bus.subscriber_count()}


@app.get("/", response_class=HTMLResponse)
async def root(_: None = Depends(_auth)):
    return HTMLResponse(_load_html())


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

    return {
        "stats": {
            "lk_cards_total": len(cards),
            "lk_cards_by_status": cards_by_status,
            "lk_cards_by_method": cards_by_method,
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

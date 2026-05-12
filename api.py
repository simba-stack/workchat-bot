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

from fastapi import (
    FastAPI, Request, Depends, HTTPException, WebSocket, WebSocketDisconnect,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

import event_bus
from storage import storage

logger = logging.getLogger(__name__)

app = FastAPI(title="PRIDE Dashboard", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

_HTML_PATH = Path(__file__).parent / "dashboard" / "index.html"
_JARVIS_PATH = Path(__file__).parent / "dashboard" / "jarvis.html"
DASHBOARD_DEFAULT = (os.getenv("DASHBOARD_DEFAULT", "jarvis") or "").lower()


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
    return HTMLResponse(_load_html(DASHBOARD_DEFAULT))


@app.get("/classic", response_class=HTMLResponse)
async def classic_dashboard(_: None = Depends(_auth)):
    """Классический дашборд (старый дизайн)."""
    return HTMLResponse(_load_html("index"))


@app.get("/jarvis", response_class=HTMLResponse)
async def jarvis_dashboard(_: None = Depends(_auth)):
    """J.A.R.V.I.S. дашборд — 3 колонки + анимированный офис."""
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
    blocks_count = sum(1 for c in cards.values() if (c.get("status") or "").upper() == "БЛОК")
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
        out.append({
            "username": uname,
            "role": r.get("role") or "—",
            "is_admin": bool(r.get("is_admin")),
            "messages": int(s.get("messages") or 0),
            "chats_touched": int(s.get("chats_touched") or 0),
            "payments_made": int(s.get("payments_made") or 0),
            "lk_completed": int(s.get("lk_completed") or 0),
            "last_active_ts": last,
            "idle_sec": idle_sec,
            "online": online,
        })
    # Сортируем: online → idle → offline; внутри — по messages убыв.
    online_priority = {"online": 0, "idle": 1, "offline": 2}
    out.sort(key=lambda x: (online_priority[x["online"]], -x["messages"]))
    return {"managers": out, "total": len(out)}


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
    return {"ok": True, "card_id": card_id, "new_status": new_status}


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
    Auth: ?user=...&pass=... в query string (Basic auth не работает в WS browser API).
    """
    user = ws.query_params.get("user", "")
    pwd = ws.query_params.get("pass", "")
    if not DASHBOARD_PASS:
        await ws.close(code=4503, reason="auth not configured")
        return
    if not (
        secrets.compare_digest(user, DASHBOARD_USER)
        and secrets.compare_digest(pwd, DASHBOARD_PASS)
    ):
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

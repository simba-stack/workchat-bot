"""WebSocket endpoint /ws/p2p (ТЗ Том 18 §5).

Auth: query ?init_data=... (Telegram WebApp).
Client → Server:
  {"action":"subscribe","channel":"trade:abc"}
  {"action":"unsubscribe","channel":"trade:abc"}
  {"action":"pong"}
Server → Client:
  {"event":"connected","user_id":42}
  {"event":"subscribed","channel":"trade:abc"}
  {"event":"error","reason":"..."}
  {"event":"ping"}
  {"event":"<EventType>","channel":"...","payload":{...},"ts":"..."}
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from api.auth import verify_init_data
from core.config import settings
from core.db import AsyncSessionLocal
from core.models import User
from p2p.enums import TradeStatus
from p2p.models import P2PTrade
from p2p.ws.manager import manager

logger = logging.getLogger("p2p.ws.router")
router = APIRouter()


PING_INTERVAL = 30.0
PONG_TIMEOUT = 60.0
_DISPUTE_STATES = {TradeStatus.DISPUTE_OPENED.value, TradeStatus.ARBITRATION.value}


async def _auth_from_init_data(init_data: str) -> User | None:
    """Проверка initData + поиск User по tg_id. Возвращает User или None."""
    try:
        tg_user = verify_init_data(init_data, settings.bot_token)
    except Exception as e:
        logger.info("[ws] auth failed: %s", e)
        return None
    tg_id = int(tg_user.get("id") or 0)
    if not tg_id:
        return None
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(User).where(User.tg_id == tg_id))
        return r.scalar_one_or_none()


async def _can_subscribe(user: User, channel: str) -> tuple[bool, str | None]:
    """Проверка прав на канал."""
    if not channel:
        return False, "invalid channel"
    if channel != "admin" and ":" not in channel:
        return False, "invalid channel format"

    if channel == "admin":
        if user.tg_id in settings.admin_ids:
            return True, None
        return False, "admin only"

    try:
        kind, ident = channel.split(":", 1)
    except ValueError:
        return False, "invalid channel format"

    if kind == "user":
        # Только свой канал
        try:
            uid = int(ident)
        except ValueError:
            return False, "invalid user id"
        if uid != user.id:
            return False, "not your user channel"
        return True, None

    if kind == "trade":
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(P2PTrade).where(P2PTrade.id == ident))
            trade = r.scalar_one_or_none()
        if not trade:
            return False, "trade not found"
        if user.id in (trade.buyer_id, trade.seller_id):
            return True, None
        if user.tg_id in settings.admin_ids and trade.status in _DISPUTE_STATES:
            return True, None
        return False, "not a trade participant"

    if kind == "advertisement":
        # Объявления — публично читаемые (TODO: ограничить если ad приватное)
        return True, None

    return False, "unknown channel kind"


async def _send(ws: WebSocket, msg: dict) -> bool:
    try:
        await ws.send_json(msg)
        return True
    except Exception as e:
        logger.debug("[ws] send failed: %s", e)
        return False


@router.websocket("/ws/p2p")
async def ws_p2p(websocket: WebSocket, init_data: str = Query("", alias="init_data")):
    # 1) accept
    await websocket.accept()

    # 2) auth
    if not init_data:
        await _send(websocket, {"event": "error", "reason": "init_data required"})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    user = await _auth_from_init_data(init_data)
    if not user:
        await _send(websocket, {"event": "error", "reason": "auth failed"})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    manager.attach(websocket, user.id)
    await _send(websocket, {
        "event": "connected",
        "user_id": user.id,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    last_pong_at = asyncio.get_event_loop().time()
    heartbeat_task: asyncio.Task | None = None

    async def heartbeat_loop() -> None:
        nonlocal last_pong_at
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                # Проверка таймаута pong
                idle = asyncio.get_event_loop().time() - last_pong_at
                if idle > PONG_TIMEOUT:
                    logger.info("[ws] user=%s pong timeout (%.1fs) — closing", user.id, idle)
                    try:
                        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                    except Exception:
                        pass
                    return
                if not await _send(websocket, {"event": "ping"}):
                    return
        except asyncio.CancelledError:
            return

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                await _send(websocket, {"event": "error", "reason": "invalid json"})
                continue
            if not isinstance(data, dict):
                await _send(websocket, {"event": "error", "reason": "expected object"})
                continue

            action = data.get("action")

            if action == "pong":
                last_pong_at = asyncio.get_event_loop().time()
                continue

            if action == "subscribe":
                channel = (data.get("channel") or "").strip()
                ok, reason = await _can_subscribe(user, channel)
                if not ok:
                    await _send(websocket, {
                        "event": "error",
                        "reason": reason or "forbidden",
                        "channel": channel,
                    })
                    continue
                manager.subscribe(websocket, channel)
                await _send(websocket, {"event": "subscribed", "channel": channel})
                continue

            if action == "unsubscribe":
                channel = (data.get("channel") or "").strip()
                manager.unsubscribe(websocket, channel)
                await _send(websocket, {"event": "unsubscribed", "channel": channel})
                continue

            await _send(websocket, {"event": "error", "reason": f"unknown action: {action}"})

    except WebSocketDisconnect:
        logger.info("[ws] user=%s disconnected", user.id)
    except Exception as e:
        logger.warning("[ws] user=%s loop error: %s", user.id, e)
    finally:
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
        manager.detach(websocket)

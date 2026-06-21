"""WebSocket ConnectionManager — channel → subscribers routing (ТЗ Том 18 §5)."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from core.db import AsyncSessionLocal
from p2p.models import P2PTrade
from sqlalchemy import select

logger = logging.getLogger("p2p.ws")


class ConnectionManager:
    """Хранит channel → set[WebSocket]. Single-threaded asyncio, локов не нужно."""

    def __init__(self) -> None:
        # channel -> set of websockets
        self._channels: dict[str, set[WebSocket]] = {}
        # websocket -> set of channels (для быстрого cleanup при disconnect)
        self._socket_channels: dict[WebSocket, set[str]] = {}
        # websocket -> user_id (для авторизации/логов)
        self._socket_user: dict[WebSocket, int] = {}

    # ── lifecycle ──────────────────────────────────────────────────────
    def attach(self, ws: WebSocket, user_id: int) -> None:
        self._socket_channels.setdefault(ws, set())
        self._socket_user[ws] = user_id

    def detach(self, ws: WebSocket) -> None:
        channels = self._socket_channels.pop(ws, set())
        for ch in channels:
            subs = self._channels.get(ch)
            if subs:
                subs.discard(ws)
                if not subs:
                    self._channels.pop(ch, None)
        self._socket_user.pop(ws, None)

    # ── subscribe ──────────────────────────────────────────────────────
    def subscribe(self, ws: WebSocket, channel: str) -> None:
        self._channels.setdefault(channel, set()).add(ws)
        self._socket_channels.setdefault(ws, set()).add(channel)

    def unsubscribe(self, ws: WebSocket, channel: str) -> None:
        subs = self._channels.get(channel)
        if subs:
            subs.discard(ws)
            if not subs:
                self._channels.pop(channel, None)
        ch_set = self._socket_channels.get(ws)
        if ch_set:
            ch_set.discard(channel)

    def user_of(self, ws: WebSocket) -> int | None:
        return self._socket_user.get(ws)

    def stats(self) -> dict[str, Any]:
        return {
            "channels": len(self._channels),
            "sockets": len(self._socket_user),
            "subs_total": sum(len(s) for s in self._channels.values()),
        }

    # ── publish ────────────────────────────────────────────────────────
    async def publish(self, channel: str, message: dict[str, Any]) -> int:
        """Отправить сообщение всем подписчикам канала. Возвращает количество доставленных."""
        subs = list(self._channels.get(channel, set()))
        if not subs:
            return 0
        # Аннотация канала
        msg = {**message, "channel": channel}
        delivered = 0
        dead: list[WebSocket] = []
        for ws in subs:
            try:
                await ws.send_json(msg)
                delivered += 1
            except Exception as e:
                logger.debug("[ws] send failed (will detach): %s", e)
                dead.append(ws)
        for ws in dead:
            self.detach(ws)
        return delivered


# Глобальный singleton — используется и из FastAPI router и из outbox publisher
manager = ConnectionManager()


# ── outbox → ws ──────────────────────────────────────────────────────────
async def publish_to_channels(
    event_type: str,
    payload: dict[str, Any],
    aggregate_type: str | None,
    aggregate_id: str | None,
) -> None:
    """Hook для outbox_publisher.register_ws_publisher().

    Маппинг event → channels:
      - aggregate_type=trade   → channel "trade:{id}" + user-каналы buyer/seller
      - aggregate_type=advertisement → channel "advertisement:{id}"
      - всегда → channel "admin"
    """
    if not aggregate_type or not aggregate_id:
        # Глобальное событие — только admin
        msg = _build_envelope(event_type, payload)
        await manager.publish("admin", msg)
        return

    msg = _build_envelope(event_type, payload)
    channels: list[str] = []

    if aggregate_type == "trade":
        channels.append(f"trade:{aggregate_id}")
        # участникам в их user-каналы (если есть в payload)
        for k in ("buyer_id", "seller_id"):
            uid = payload.get(k)
            if uid:
                channels.append(f"user:{int(uid)}")
        # TODO #7: merchant:{seller_id} — для подписки на свои события мерчанта
        seller_id = payload.get("seller_id")
        if seller_id:
            channels.append(f"merchant:{int(seller_id)}")
        # fallback: вытащить participants из БД если их нет в payload
        if not any(payload.get(k) for k in ("buyer_id", "seller_id")):
            try:
                async with AsyncSessionLocal() as db:
                    r = await db.execute(select(P2PTrade).where(P2PTrade.id == aggregate_id))
                    t = r.scalar_one_or_none()
                    if t:
                        channels.append(f"user:{t.buyer_id}")
                        channels.append(f"user:{t.seller_id}")
                        channels.append(f"merchant:{t.seller_id}")
            except Exception as e:
                logger.debug("[ws] fallback participant lookup failed: %s", e)
    elif aggregate_type == "advertisement":
        channels.append(f"advertisement:{aggregate_id}")
        owner = payload.get("owner_id") or payload.get("user_id")
        if owner:
            channels.append(f"user:{int(owner)}")
            # TODO #7: merchant channel
            channels.append(f"merchant:{int(owner)}")
    elif aggregate_type == "user":
        channels.append(f"user:{aggregate_id}")
    elif aggregate_type == "dispute":
        # Диспут — рассылаем по trade-каналу и user-каналам
        trade_id = payload.get("trade_id")
        if trade_id:
            channels.append(f"trade:{trade_id}")
        for k in ("buyer_id", "seller_id", "opened_by_id"):
            uid = payload.get(k)
            if uid:
                channels.append(f"user:{int(uid)}")

    # admin всегда получает копию
    channels.append("admin")

    # Дедуп
    for ch in dict.fromkeys(channels):
        try:
            await manager.publish(ch, msg)
        except Exception as e:
            logger.warning("[ws] publish to %s failed: %s", ch, e)


def _build_envelope(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": event_type,
        "payload": payload or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }

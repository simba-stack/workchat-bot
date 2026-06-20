"""P2P WebSocket Protocol (ТЗ Том 18 §5).

Real-time канал для фронта: subscribe/publish, heartbeat, channel routing.

Каналы:
  - trade:{trade_id}     — события сделки (только участники + арбитры при споре)
  - user:{user_id}       — личные события юзера (только сам юзер)
  - advertisement:{ad_id} — события объявления (publicly readable: TBD)
  - admin               — глобальный канал для админов
"""
from p2p.ws.manager import (
    ConnectionManager,
    manager,
    publish_to_channels,
)

__all__ = ["ConnectionManager", "manager", "publish_to_channels"]

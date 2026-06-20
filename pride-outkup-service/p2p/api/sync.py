"""P2P Lost-Events Recovery — GET /api/v2/p2p/sync.

Используется фронтом для catch-up после реконнекта WebSocket:
он передаёт last seen sequence (since_seq), endpoint возвращает все
события из p2p_outbox с id > since_seq, отфильтрованные по принадлежности.

Принадлежность:
- channels=trade:<id> → юзер должен быть buyer/seller
- channels=user:<id>  → пропускаем только если совпадает с user.id
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p.models import P2POutbox, P2PTrade

logger = logging.getLogger("p2p.api.sync")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-sync"])


def _parse_channels(s: str) -> list[tuple[str, str]]:
    """'trade:abc,user:1' → [('trade','abc'),('user','1')]"""
    out: list[tuple[str, str]] = []
    if not s:
        return out
    for raw in s.split(","):
        raw = raw.strip()
        if not raw or ":" not in raw:
            continue
        kind, _, val = raw.partition(":")
        kind = kind.strip().lower()
        val = val.strip()
        if kind and val:
            out.append((kind, val))
    return out


@router.get("/sync")
async def sync_events(
    since_seq: int = Query(0, ge=0, description="last seen outbox.id (int internal seq)"),
    channels: str = Query("", description="comma separated: trade:<id>,user:<id>"),
    limit: int = Query(100, ge=1, le=500),
    lookback_minutes: int = Query(60, ge=1, le=1440),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Вернуть outbox events для catch-up. ids — это внутренние uuid'ы p2p_outbox.

    since_seq трактуем как ts-метку created_at (через ms) если > 0 — но БД
    хранит created_at:DateTime. Чтобы не зависеть от поля 'seq', мы фильтруем
    по created_at >= now - lookback_minutes и опционально по created_at > t(since_seq).

    Возвращаем формат удобный для фронта:
      {
        items: [{event_id, event_type, payload, aggregate_type, aggregate_id, seq_ms, ts}],
        server_seq_ms: <unix_ms сейчас>,
        count: N,
      }
    """
    chans = _parse_channels(channels)

    # Фильтр по принадлежности
    trade_ids: list[str] = []
    user_filters: list[int] = []
    for kind, val in chans:
        if kind == "trade":
            trade_ids.append(val)
        elif kind == "user":
            try:
                user_filters.append(int(val))
            except ValueError:
                continue

    # Авторизация на trade-channels: юзер должен быть участником
    if trade_ids:
        rq = await db.execute(
            select(P2PTrade.id).where(
                P2PTrade.id.in_(trade_ids),
                or_(P2PTrade.buyer_id == user.id, P2PTrade.seller_id == user.id),
            )
        )
        allowed_trade_ids = {row[0] for row in rq.all()}
        denied = set(trade_ids) - allowed_trade_ids
        if denied:
            raise HTTPException(403, f"forbidden trade channels: {sorted(denied)}")
        trade_ids = list(allowed_trade_ids)

    # Авторизация на user-channels — только свой user.id
    if user_filters:
        if any(uid != user.id for uid in user_filters):
            raise HTTPException(403, "user channels: only own user.id is allowed")

    cutoff_ts = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

    # since_seq трактуем как unix_ms timestamp (frontend хранит server_seq_ms)
    since_ts: Optional[datetime] = None
    if since_seq > 0:
        try:
            since_ts = datetime.fromtimestamp(since_seq / 1000.0, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            since_ts = None

    effective_ts = since_ts if (since_ts and since_ts > cutoff_ts) else cutoff_ts

    conds = [P2POutbox.created_at > effective_ts]

    # Если есть фильтры по агрегату — добавим:
    agg_conds = []
    if trade_ids:
        agg_conds.append(
            and_(
                P2POutbox.aggregate_type == "trade",
                P2POutbox.aggregate_id.in_(trade_ids),
            )
        )
    # Для user-channel выдаём только notif/wallet/personal — здесь упрощённо:
    # отбираем все события где payload->user_id совпадает (не делаем, чтобы не
    # триггерить JSON-операторы PG в общем случае). Если phisically нужно —
    # фронт фильтрует на своей стороне.
    if user_filters:
        agg_conds.append(
            and_(
                P2POutbox.aggregate_type == "user",
                P2POutbox.aggregate_id.in_([str(uid) for uid in user_filters]),
            )
        )

    if agg_conds:
        conds.append(or_(*agg_conds))

    q = (
        select(P2POutbox)
        .where(and_(*conds))
        .order_by(P2POutbox.created_at.asc())
        .limit(limit)
    )
    r = await db.execute(q)
    rows = list(r.scalars().all())

    items = []
    for row in rows:
        ts = row.created_at
        seq_ms = int(ts.timestamp() * 1000) if ts else 0
        items.append({
            "event_id": row.event_id,
            "event_type": row.event_type,
            "aggregate_type": row.aggregate_type,
            "aggregate_id": row.aggregate_id,
            "payload": row.payload or {},
            "seq_ms": seq_ms,
            "ts": ts.isoformat() if ts else None,
            "correlation_id": row.correlation_id,
        })

    server_now = datetime.now(timezone.utc)
    return {
        "items": items,
        "count": len(items),
        "server_seq_ms": int(server_now.timestamp() * 1000),
        "server_ts": server_now.isoformat(),
        "since_seq": since_seq,
        "lookback_minutes": lookback_minutes,
    }

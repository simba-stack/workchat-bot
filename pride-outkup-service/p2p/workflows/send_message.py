"""Workflow: send_message — отправить сообщение в чат сделки (ТЗ Том 13).

- Проверяет участие (buyer/seller/arbitrator при DISPUTE/ARBITRATION).
- Лимит длины через policies.CHAT_MAX_MESSAGE_LENGTH.
- sequence_number — atomic через advisory_lock("chat:{trade_id}") + MAX+1.
- emit EventType.CHAT_MESSAGE_SENT.
"""
from __future__ import annotations
import logging

from fastapi import HTTPException
from sqlalchemy import select, func

from p2p import audit, locks, outbox, policies
from p2p.enums import EventType, MessageType, TradeStatus
from p2p.models import P2PMessage, P2PTrade
from p2p.orchestrator import WorkflowContext
from core.config import settings

logger = logging.getLogger("p2p.wf.send_message")


_ALLOWED_TYPES = {MessageType.TEXT.value, MessageType.IMAGE.value, MessageType.SYSTEM.value}
_DISPUTE_STATES = {TradeStatus.DISPUTE_OPENED.value, TradeStatus.ARBITRATION.value}


async def handle(ctx: WorkflowContext) -> dict:
    p = ctx.input_payload
    db = ctx.db

    trade_id = p.get("trade_id")
    text_body = (p.get("text") or p.get("body_text") or "").strip()
    message_type = (p.get("message_type") or MessageType.TEXT.value).upper()
    attachment_id = p.get("attachment_id")

    if not trade_id:
        raise HTTPException(422, "trade_id required")
    if message_type not in _ALLOWED_TYPES:
        raise HTTPException(422, f"message_type must be one of {sorted(_ALLOWED_TYPES)}")
    if message_type == MessageType.TEXT.value and not text_body:
        raise HTTPException(422, "text required for TEXT message")

    # Длина
    max_len = await policies.get_int(db, "CHAT_MAX_MESSAGE_LENGTH")
    if len(text_body) > max_len:
        raise HTTPException(422, f"text too long (max {max_len})")

    # Найти trade
    r = await db.execute(select(P2PTrade).where(P2PTrade.id == trade_id))
    trade = r.scalar_one_or_none()
    if not trade:
        raise HTTPException(404, "trade not found")

    # Авторизация
    is_buyer = trade.buyer_id == ctx.user_id
    is_seller = trade.seller_id == ctx.user_id
    is_arbiter = False
    # Арбитр = юзер чей tg_id есть в settings.admin_ids (полноценный RBAC ещё в TODO #20)
    from core.models import User
    ur = await db.execute(select(User).where(User.id == ctx.user_id))
    u = ur.scalar_one_or_none()
    if u and u.tg_id in settings.admin_ids:
        is_arbiter = True

    if not (is_buyer or is_seller):
        if not (is_arbiter and trade.status in _DISPUTE_STATES):
            raise HTTPException(403, "not a trade participant")

    # SYSTEM сообщения может слать только арбитр (или внутренний код через ctx.actor_role)
    if message_type == MessageType.SYSTEM.value and not is_arbiter and ctx.actor_role != "SYSTEM":
        raise HTTPException(403, "SYSTEM message requires arbiter/system role")

    # sequence_number — atomic
    await locks.advisory_lock(db, f"chat:{trade_id}")
    rseq = await db.execute(
        select(func.coalesce(func.max(P2PMessage.sequence_number), 0))
        .where(P2PMessage.trade_id == trade_id)
    )
    next_seq = int(rseq.scalar() or 0) + 1

    msg = P2PMessage(
        trade_id=trade_id,
        sender_id=ctx.user_id,
        sequence_number=next_seq,
        message_type=message_type,
        text=text_body or None,
        attachment_id=attachment_id,
        is_system=(message_type == MessageType.SYSTEM.value),
        status="SENT",
    )
    db.add(msg)
    await db.flush()

    await audit.log(
        db,
        action="chat.message_sent",
        entity_type="trade_message",
        entity_id=msg.id,
        actor_id=ctx.user_id,
        actor_role=ctx.actor_role,
        new_state={
            "trade_id": trade_id,
            "sequence_number": next_seq,
            "message_type": message_type,
            "length": len(text_body),
        },
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
        source=ctx.source,
    )

    await outbox.emit(
        db,
        event_type=EventType.CHAT_MESSAGE_SENT.value,
        payload={
            "message_id": msg.id,
            "trade_id": trade_id,
            "sender_id": ctx.user_id,
            "sequence_number": next_seq,
            "message_type": message_type,
            "buyer_id": trade.buyer_id,
            "seller_id": trade.seller_id,
        },
        aggregate_type="trade",
        aggregate_id=trade_id,
        correlation_id=ctx.correlation_id,
        workflow_id=ctx.workflow_id,
    )

    return {
        "ok": True,
        "message_id": msg.id,
        "trade_id": trade_id,
        "sequence_number": next_seq,
        "message_type": message_type,
    }

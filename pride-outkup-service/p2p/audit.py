"""Audit Log helpers (ТЗ Том 18 §14).

Пишется ВНУТРИ той же транзакции что и business data.
Если audit не записался — Commit запрещён (raise → Rollback).

IMMUTABLE: UPDATE/DELETE на этих записях запрещены на уровне приложения.
"""
from __future__ import annotations
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from p2p.models import P2PAuditLog

logger = logging.getLogger("p2p.audit")


async def log(
    db: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_id: int | None = None,
    actor_role: str | None = None,
    previous_state: dict | None = None,
    new_state: dict | None = None,
    correlation_id: str | None = None,
    workflow_id: str | None = None,
    source: str | None = None,
    ip_address: str | None = None,
    device_id: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Создать audit record. Возвращает ID."""
    rec = P2PAuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        actor_role=actor_role,
        previous_state=previous_state,
        new_state=new_state,
        correlation_id=correlation_id,
        workflow_id=workflow_id,
        source=source,
        ip_address=ip_address,
        device_id=device_id,
        user_agent=user_agent,
    )
    db.add(rec)
    await db.flush()
    return rec.id

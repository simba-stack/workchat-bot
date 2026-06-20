"""P2P Commands API (POST endpoints) — CQRS write-side.

Все mutate операции идут через run_workflow() в Orchestrator.
"""
from __future__ import annotations
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.db import get_db
from core.models import User
from p2p.api._deps import get_idempotency_key, get_actor_role
from p2p.orchestrator import run_workflow
from p2p.workflows import (
    create_advertisement, create_trade, mark_paid, confirm_payment, cancel_trade,
    open_dispute, resolve_dispute,
)

logger = logging.getLogger("p2p.api.commands")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-commands"])


def _source(req: Request) -> str:
    ua = req.headers.get("user-agent", "")[:50]
    return f"miniapp:{ua}"


# ============= ADVERTISEMENTS =============

@router.post("/advertisements")
async def cmd_create_advertisement(
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    return await run_workflow(
        db,
        workflow_type="create_advertisement",
        user_id=user.id,
        input_payload=payload,
        handler=create_advertisement.handle,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req),
        endpoint="POST /p2p/advertisements",
    )


# ============= TRADES =============

@router.post("/trades")
async def cmd_create_trade(
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    return await run_workflow(
        db,
        workflow_type="create_trade",
        user_id=user.id,
        input_payload=payload,
        handler=create_trade.handle,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req),
        endpoint="POST /p2p/trades",
    )


@router.post("/trades/{trade_id}/mark-paid")
async def cmd_mark_paid(
    trade_id: str,
    payload: dict[str, Any] | None = None,
    req: Request = None,  # type: ignore
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "trade_id": trade_id}
    return await run_workflow(
        db,
        workflow_type="mark_paid",
        user_id=user.id,
        input_payload=input_p,
        handler=mark_paid.handle,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req) if req else None,
        endpoint=f"POST /p2p/trades/{trade_id}/mark-paid",
    )


@router.post("/trades/{trade_id}/confirm-payment")
async def cmd_confirm_payment(
    trade_id: str,
    payload: dict[str, Any] | None = None,
    req: Request = None,  # type: ignore
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "trade_id": trade_id}
    return await run_workflow(
        db,
        workflow_type="confirm_payment",
        user_id=user.id,
        input_payload=input_p,
        handler=confirm_payment.handle,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req) if req else None,
        endpoint=f"POST /p2p/trades/{trade_id}/confirm-payment",
    )


@router.post("/trades/{trade_id}/cancel")
async def cmd_cancel_trade(
    trade_id: str,
    payload: dict[str, Any] | None = None,
    req: Request = None,  # type: ignore
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "trade_id": trade_id}
    return await run_workflow(
        db,
        workflow_type="cancel_trade",
        user_id=user.id,
        input_payload=input_p,
        handler=cancel_trade.handle,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req) if req else None,
        endpoint=f"POST /p2p/trades/{trade_id}/cancel",
    )


@router.post("/trades/{trade_id}/dispute")
async def cmd_open_dispute(
    trade_id: str,
    payload: dict[str, Any] | None = None,
    req: Request = None,  # type: ignore
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    input_p = {**(payload or {}), "trade_id": trade_id}
    return await run_workflow(
        db,
        workflow_type="open_dispute",
        user_id=user.id,
        input_payload=input_p,
        handler=open_dispute.handle,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req) if req else None,
        endpoint=f"POST /p2p/trades/{trade_id}/dispute",
    )

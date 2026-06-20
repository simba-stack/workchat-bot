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
    create_advertisement, update_advertisement, pause_advertisement, resume_advertisement,
    archive_advertisement, delete_advertisement,
    create_trade, mark_paid, confirm_payment, cancel_trade,
    open_dispute, resolve_dispute,
    trade_extras,
)

logger = logging.getLogger("p2p.api.commands")
router = APIRouter(prefix="/api/v2/p2p", tags=["p2p-commands"])


def _source(req: Request | None) -> str | None:
    if not req:
        return None
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
        source=_source(req),
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
        source=_source(req),
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
        source=_source(req),
        endpoint=f"POST /p2p/trades/{trade_id}/cancel",
    )


@router.post("/trades/{trade_id}/extend-deadline")
async def cmd_extend_deadline(
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
        workflow_type="extend_deadline",
        user_id=user.id,
        input_payload=input_p,
        handler=trade_extras.extend_deadline,
        idempotency_key=idempotency_key,
        actor_role=get_actor_role(user),
        source=_source(req),
        endpoint=f"POST /p2p/trades/{trade_id}/extend-deadline",
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
        source=_source(req),
        endpoint=f"POST /p2p/trades/{trade_id}/dispute",
    )


# ============= ADVERTISEMENT MANAGEMENT =============

@router.patch("/advertisements/{ad_id}")
async def cmd_update_advertisement(
    ad_id: str,
    payload: dict[str, Any],
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    inp = {"advertisement_id": ad_id, "changes": payload}
    return await run_workflow(
        db, workflow_type="update_advertisement", user_id=user.id,
        input_payload=inp, handler=update_advertisement.handle,
        idempotency_key=idempotency_key, actor_role=get_actor_role(user),
        source=_source(req), endpoint=f"PATCH /p2p/advertisements/{ad_id}",
    )


@router.post("/advertisements/{ad_id}/pause")
async def cmd_pause_advertisement(
    ad_id: str,
    payload: dict[str, Any] | None = None,
    req: Request = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    inp = {**(payload or {}), "advertisement_id": ad_id}
    return await run_workflow(
        db, workflow_type="pause_advertisement", user_id=user.id,
        input_payload=inp, handler=pause_advertisement.handle,
        idempotency_key=idempotency_key, actor_role=get_actor_role(user),
        source=_source(req),
        endpoint=f"POST /p2p/advertisements/{ad_id}/pause",
    )


@router.post("/advertisements/{ad_id}/resume")
async def cmd_resume_advertisement(
    ad_id: str,
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    inp = {"advertisement_id": ad_id}
    return await run_workflow(
        db, workflow_type="resume_advertisement", user_id=user.id,
        input_payload=inp, handler=resume_advertisement.handle,
        idempotency_key=idempotency_key, actor_role=get_actor_role(user),
        source=_source(req), endpoint=f"POST /p2p/advertisements/{ad_id}/resume",
    )


@router.post("/advertisements/{ad_id}/archive")
async def cmd_archive_advertisement(
    ad_id: str,
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    inp = {"advertisement_id": ad_id}
    return await run_workflow(
        db, workflow_type="archive_advertisement", user_id=user.id,
        input_payload=inp, handler=archive_advertisement.handle,
        idempotency_key=idempotency_key, actor_role=get_actor_role(user),
        source=_source(req), endpoint=f"POST /p2p/advertisements/{ad_id}/archive",
    )


@router.delete("/advertisements/{ad_id}")
async def cmd_delete_advertisement(
    ad_id: str,
    req: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Depends(get_idempotency_key),
):
    inp = {"advertisement_id": ad_id}
    return await run_workflow(
        db, workflow_type="delete_advertisement", user_id=user.id,
        input_payload=inp, handler=delete_advertisement.handle,
        idempotency_key=idempotency_key, actor_role=get_actor_role(user),
        source=_source(req), endpoint=f"DELETE /p2p/advertisements/{ad_id}",
    )

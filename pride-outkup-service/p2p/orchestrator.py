"""Transaction Orchestrator (ТЗ Том 16).

Единая точка выполнения всех бизнес-процессов.
Создаёт WorkflowExecution, выполняет шаги через try/except, гарантирует Rollback.

Все workflow вызываются через run_workflow() — никаких прямых вызовов engines.
"""
from __future__ import annotations
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Awaitable, Callable, Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from p2p import audit, idempotency, locks
from p2p.enums import WorkflowStatus
from p2p.models import P2PWorkflowExecution

logger = logging.getLogger("p2p.orchestrator")


class WorkflowContext:
    """Передаётся в workflow handler. Содержит общие поля для трассировки."""

    def __init__(
        self,
        *,
        db: AsyncSession,
        workflow_type: str,
        workflow_id: str,
        correlation_id: str,
        idempotency_key: str,
        user_id: int | None,
        input_payload: dict,
        actor_role: str | None = None,
        source: str | None = None,
    ) -> None:
        self.db = db
        self.workflow_type = workflow_type
        self.workflow_id = workflow_id
        self.correlation_id = correlation_id
        self.idempotency_key = idempotency_key
        self.user_id = user_id
        self.input_payload = input_payload
        self.actor_role = actor_role
        self.source = source
        self.current_step: str | None = None

    def step(self, name: str) -> None:
        """Отметить текущий шаг (для observability/recovery)."""
        self.current_step = name
        logger.debug("[wf] %s/%s step=%s", self.workflow_type, self.workflow_id, name)


async def run_workflow(
    db: AsyncSession,
    *,
    workflow_type: str,
    user_id: int | None,
    input_payload: dict,
    handler: Callable[[WorkflowContext], Awaitable[dict]],
    idempotency_key: str | None = None,
    actor_role: str | None = None,
    source: str | None = None,
    endpoint: str | None = None,
) -> dict:
    """Главный entry point для любого workflow.

    Гарантирует:
    - Idempotency (если передан ключ)
    - Audit пишется в той же транзакции
    - Rollback на любой ошибке
    - WorkflowExecution row для observability

    Возвращает результат handler'а (dict) — он будет отдан клиенту.
    Если handler выбрасывает HTTPException — он пробрасывается, transaction rolled back.
    """
    correlation_id = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    effective_key = idempotency_key or workflow_id

    # 1) Idempotency check (если ключ передан клиентом)
    if idempotency_key and endpoint:
        hit = await idempotency.check(db, user_id=user_id, endpoint=endpoint, key=idempotency_key)
        if hit is not None:
            logger.info("[wf] idempotency HIT type=%s key=%s — returning cached",
                        workflow_type, idempotency_key)
            return hit.body

    # 2) Создаём WorkflowExecution
    wf = P2PWorkflowExecution(
        workflow_type=workflow_type,
        version=1,
        user_id=user_id,
        correlation_id=correlation_id,
        idempotency_key=effective_key,
        status=WorkflowStatus.RUNNING.value,
        input_payload=input_payload,
        started_at=datetime.now(timezone.utc),
    )
    db.add(wf)
    await db.flush()
    workflow_id = wf.id

    ctx = WorkflowContext(
        db=db,
        workflow_type=workflow_type,
        workflow_id=workflow_id,
        correlation_id=correlation_id,
        idempotency_key=effective_key,
        user_id=user_id,
        input_payload=input_payload,
        actor_role=actor_role,
        source=source,
    )

    started = time.time()
    try:
        result = await handler(ctx)
        if not isinstance(result, dict):
            raise HTTPException(500, "workflow handler must return dict")

        wf.status = WorkflowStatus.COMPLETED.value
        wf.output_payload = result
        wf.finished_at = datetime.now(timezone.utc)
        wf.current_step = ctx.current_step
        await db.flush()

        # Audit
        await audit.log(
            db,
            action=f"workflow.{workflow_type}.completed",
            entity_type="workflow",
            entity_id=workflow_id,
            actor_id=user_id,
            actor_role=actor_role,
            new_state={"output": result, "duration_ms": int((time.time() - started) * 1000)},
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            source=source,
        )

        # Idempotency save (после success)
        if idempotency_key and endpoint:
            await idempotency.save(
                db, user_id=user_id, endpoint=endpoint, key=idempotency_key,
                status=200, body=result, workflow_id=workflow_id,
            )

        return result

    except HTTPException as e:
        wf.status = WorkflowStatus.FAILED.value
        wf.error_code = str(e.status_code)
        wf.error_message = str(e.detail)
        wf.finished_at = datetime.now(timezone.utc)
        wf.current_step = ctx.current_step
        # Audit failed
        try:
            await audit.log(
                db,
                action=f"workflow.{workflow_type}.failed",
                entity_type="workflow",
                entity_id=workflow_id,
                actor_id=user_id,
                actor_role=actor_role,
                new_state={"error_code": str(e.status_code), "error_message": str(e.detail)},
                correlation_id=correlation_id,
                workflow_id=workflow_id,
                source=source,
            )
        except Exception:
            pass
        # Пробрасываем — FastAPI dependency сделает rollback
        raise

    except Exception as e:
        wf.status = WorkflowStatus.FAILED.value
        wf.error_code = type(e).__name__
        wf.error_message = str(e)[:1000]
        wf.finished_at = datetime.now(timezone.utc)
        wf.current_step = ctx.current_step
        logger.exception("[wf] %s/%s FAILED: %s", workflow_type, workflow_id, e)
        raise HTTPException(500, f"workflow {workflow_type} failed: {type(e).__name__}")

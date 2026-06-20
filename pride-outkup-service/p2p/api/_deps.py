"""Общие зависимости для p2p endpoint'ов."""
from __future__ import annotations
from fastapi import Header, HTTPException

from api.auth import get_current_user  # reuse
from p2p.enums import P2PUserRole


def get_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str | None:
    return idempotency_key


def get_actor_role(user) -> str:
    """Маппинг User → P2PUserRole через rbac.resolve_role()."""
    # Lazy import чтобы избежать циклов с rbac → api.auth.
    from p2p import rbac
    return rbac.resolve_role(user)

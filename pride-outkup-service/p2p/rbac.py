"""RBAC — Role-Based Access Control (ТЗ Том 9).

Роли определяются динамически по user:
  1. tg_id в settings.ADMIN_TG_IDS → ADMIN
  2. tg_id в settings.ARBITRATOR_TG_IDS → ARBITRATOR
  3. tg_id в settings.SUPPORT_TG_IDS → SUPPORT
  4. completed_trades > 50 → MERCHANT
  5. иначе → USER

Использование:
    # В endpoint:
    @router.get("/admin/...")
    async def admin_only(
        user = Depends(require_role(P2PUserRole.ADMIN.value)),
    ): ...

    # Программно:
    role = rbac.resolve_role(user)
"""
from __future__ import annotations
import logging
from typing import Any, Iterable

from fastapi import Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from core.config import settings
from core.db import get_db
from p2p.enums import P2PUserRole

logger = logging.getLogger("p2p.rbac")


# ═══════════════════════════════════════════════════════════════════════
# Helpers: считаем tg_id сеты из settings
# ═══════════════════════════════════════════════════════════════════════

def _parse_tg_ids(raw: Any) -> set[int]:
    """Распарсить '123,456,789' или list → set[int]. Безопасно к мусору."""
    if raw is None:
        return set()
    out: set[int] = set()
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw).split(",")
    for x in items:
        try:
            s = str(x).strip()
            if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                out.add(int(s))
        except Exception:
            pass
    return out


def _admin_tg_ids() -> set[int]:
    # Сначала пробуем готовое свойство, иначе сырое значение
    try:
        ids = getattr(settings, "admin_ids", None)
        if ids:
            return set(int(x) for x in ids)
    except Exception:
        pass
    raw = (
        getattr(settings, "ADMIN_TG_IDS", None)
        or getattr(settings, "admin_tg_ids", None)
        or ""
    )
    return _parse_tg_ids(raw)


def _arbitrator_tg_ids() -> set[int]:
    raw = (
        getattr(settings, "ARBITRATOR_TG_IDS", None)
        or getattr(settings, "arbitrator_tg_ids", None)
        or ""
    )
    return _parse_tg_ids(raw)


def _support_tg_ids() -> set[int]:
    raw = (
        getattr(settings, "SUPPORT_TG_IDS", None)
        or getattr(settings, "support_tg_ids", None)
        or ""
    )
    return _parse_tg_ids(raw)


def _super_admin_tg_ids() -> set[int]:
    raw = (
        getattr(settings, "SUPER_ADMIN_TG_IDS", None)
        or getattr(settings, "super_admin_tg_ids", None)
        or ""
    )
    return _parse_tg_ids(raw)


# ═══════════════════════════════════════════════════════════════════════
# Merchant threshold — берём из settings или дефолт
# ═══════════════════════════════════════════════════════════════════════
MERCHANT_MIN_COMPLETED_TRADES = 50


def _merchant_threshold() -> int:
    try:
        v = getattr(settings, "MERCHANT_MIN_COMPLETED_TRADES", None)
        if v is not None:
            return int(v)
    except Exception:
        pass
    return MERCHANT_MIN_COMPLETED_TRADES


# ═══════════════════════════════════════════════════════════════════════
# Core API
# ═══════════════════════════════════════════════════════════════════════

def resolve_role(user: Any) -> str:
    """Определить роль пользователя.

    Цепочка приоритетов:
        SUPER_ADMIN > ADMIN > ARBITRATOR > SUPPORT > MERCHANT > USER

    Параметр `user` — это core.models.User (или None → GUEST).
    Проверка MERCHANT идёт по user.completed_deals (если поле есть) — без БД-запроса.
    """
    if not user:
        return P2PUserRole.GUEST.value

    tg_id = getattr(user, "tg_id", None)
    if tg_id is not None:
        try:
            tg_id = int(tg_id)
        except Exception:
            tg_id = None

    if tg_id is not None:
        if tg_id in _super_admin_tg_ids():
            return P2PUserRole.SUPER_ADMIN.value
        if tg_id in _admin_tg_ids():
            return P2PUserRole.ADMIN.value
        if tg_id in _arbitrator_tg_ids():
            return P2PUserRole.ARBITRATOR.value
        if tg_id in _support_tg_ids():
            return P2PUserRole.SUPPORT.value

    # MERCHANT по completed_deals (поле есть в core.models.User)
    completed = getattr(user, "completed_deals", None) or 0
    try:
        if int(completed) > _merchant_threshold():
            return P2PUserRole.MERCHANT.value
    except Exception:
        pass

    return P2PUserRole.USER.value


async def resolve_role_async(db: AsyncSession, user: Any) -> str:
    """Расширенная версия: для MERCHANT-проверки делает запрос к p2p_trades
    (на случай если completed_deals не обновлено).
    """
    base = resolve_role(user)
    if base != P2PUserRole.USER.value:
        return base
    # Дополнительная проверка через p2p_trades
    try:
        from p2p.enums import TradeStatus
        from p2p.models import P2PTrade
        from sqlalchemy import or_
        r = await db.execute(
            select(func.count(P2PTrade.id)).where(
                P2PTrade.status == TradeStatus.COMPLETED.value,
                or_(P2PTrade.buyer_id == user.id, P2PTrade.seller_id == user.id),
            )
        )
        cnt = int(r.scalar() or 0)
        if cnt > _merchant_threshold():
            return P2PUserRole.MERCHANT.value
    except Exception as e:
        logger.debug("[rbac] async merchant check failed: %s", e)
    return base


def is_admin(user: Any) -> bool:
    return resolve_role(user) in (P2PUserRole.ADMIN.value, P2PUserRole.SUPER_ADMIN.value)


def is_arbitrator(user: Any) -> bool:
    """ARBITRATOR + ADMIN/SUPER_ADMIN тоже могут арбитражить."""
    return resolve_role(user) in (
        P2PUserRole.ARBITRATOR.value,
        P2PUserRole.ADMIN.value,
        P2PUserRole.SUPER_ADMIN.value,
    )


def is_support(user: Any) -> bool:
    return resolve_role(user) in (
        P2PUserRole.SUPPORT.value,
        P2PUserRole.ADMIN.value,
        P2PUserRole.SUPER_ADMIN.value,
    )


# ═══════════════════════════════════════════════════════════════════════
# FastAPI dependency
# ═══════════════════════════════════════════════════════════════════════

def require_role(*allowed_roles: str):
    """FastAPI dependency-factory: пропустить только если роль user'а
    входит в `allowed_roles`. SUPER_ADMIN всегда проходит.

    Использование:
        @router.post("/x")
        async def x(user = Depends(require_role(
            P2PUserRole.ADMIN.value, P2PUserRole.ARBITRATOR.value
        ))): ...
    """
    allowed: set[str] = set(allowed_roles) | {P2PUserRole.SUPER_ADMIN.value}

    async def _dep(
        user=Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        role = resolve_role(user)
        if role not in allowed:
            # async-проверка для merchant
            role = await resolve_role_async(db, user)
        if role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"role required: {sorted(allowed_roles)} (your role: {role})",
            )
        return user

    return _dep


def require_admin():
    """Shortcut для require_role(ADMIN, SUPER_ADMIN)."""
    return require_role(P2PUserRole.ADMIN.value)


def require_arbitrator():
    """ARBITRATOR + ADMIN."""
    return require_role(P2PUserRole.ARBITRATOR.value, P2PUserRole.ADMIN.value)


def require_support():
    return require_role(
        P2PUserRole.SUPPORT.value,
        P2PUserRole.ARBITRATOR.value,
        P2PUserRole.ADMIN.value,
    )

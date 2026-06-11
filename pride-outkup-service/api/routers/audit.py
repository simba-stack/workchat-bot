"""Audit/Feature-flag admin endpoints.

JARVIS вкладка «Аудит функций» использует это API:
  GET    /api/admin/audit/features                — список всех функций
  PATCH  /api/admin/audit/features/{key}          — toggle / config / описание
  POST   /api/admin/audit/features/{key}/test     — запустить self-test
  POST   /api/admin/audit/features/run_all_tests  — массовый прогон
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_admin
from core.db import get_db
from core.models import FeatureFlag, User
from core.services import feature_flags

router = APIRouter()


def _to_dict(f: FeatureFlag) -> dict:
    return {
        "key": f.key,
        "label": f.label,
        "category": f.category,
        "enabled": bool(f.enabled),
        "config": f.config or {},
        "description": f.description,
        "last_check_at": f.last_check_at.isoformat() if f.last_check_at else None,
        "last_check_status": f.last_check_status,
        "last_check_note": f.last_check_note,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
        "updated_by": f.updated_by,
    }


@router.get("/features")
async def list_features(
    category: str | None = None,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(FeatureFlag).order_by(FeatureFlag.category, FeatureFlag.key)
    if category:
        q = q.where(FeatureFlag.category == category)
    rows = (await db.execute(q)).scalars().all()
    # Группировка по категориям для UI
    grouped: dict[str, list] = {}
    for f in rows:
        grouped.setdefault(f.category, []).append(_to_dict(f))
    return {
        "ok": True,
        "items": [_to_dict(f) for f in rows],
        "by_category": grouped,
        "categories": sorted(grouped.keys()),
        "total": len(rows),
        "enabled": sum(1 for f in rows if f.enabled),
        "disabled": sum(1 for f in rows if not f.enabled),
    }


@router.patch("/features/{key}")
async def update_feature(
    key: str,
    payload: dict,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    f = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))).scalar_one_or_none()
    if not f:
        raise HTTPException(404, f"feature '{key}' не найдена")
    changed = []
    if "enabled" in payload:
        f.enabled = bool(payload["enabled"])
        changed.append(f"enabled={f.enabled}")
    if "config" in payload:
        if not isinstance(payload["config"], (dict, type(None))):
            raise HTTPException(400, "config must be object or null")
        f.config = payload["config"]
        changed.append("config")
    if "description" in payload:
        f.description = (payload["description"] or "")[:2000]
        changed.append("description")
    if not changed:
        raise HTTPException(400, "nothing to update")
    f.updated_at = datetime.now(timezone.utc)
    f.updated_by = me.username or str(me.tg_id)
    await db.commit()
    await feature_flags.invalidate_cache()
    return {"ok": True, "feature": _to_dict(f), "changed": changed}


@router.post("/features/{key}/test")
async def test_feature(
    key: str,
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    f = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))).scalar_one_or_none()
    if not f:
        raise HTTPException(404, f"feature '{key}' не найдена")
    ok, note = await feature_flags.run_self_test(key)
    await feature_flags.save_check_result(key, ok, note)
    # Перечитать
    f = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))).scalar_one_or_none()
    return {"ok": ok, "note": note, "feature": _to_dict(f)}


@router.post("/features/run_all_tests")
async def run_all_tests(
    me: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Прогон всех self-tests. Возвращает сводный отчёт."""
    rows = (await db.execute(select(FeatureFlag))).scalars().all()
    report: list[dict] = []
    ok_count, fail_count = 0, 0
    for f in rows:
        ok, note = await feature_flags.run_self_test(f.key)
        await feature_flags.save_check_result(f.key, ok, note)
        report.append({"key": f.key, "label": f.label, "ok": ok, "note": note})
        if ok:
            ok_count += 1
        else:
            fail_count += 1
    return {
        "ok": True,
        "total": len(report),
        "ok_count": ok_count,
        "fail_count": fail_count,
        "report": report,
    }


@router.post("/features/resync_registry")
async def resync_registry(
    me: User = Depends(require_admin),
):
    """Перечитать REGISTRY в БД (после деплоя с новыми функциями)."""
    await feature_flags.bootstrap_registry()
    await feature_flags.invalidate_cache()
    return {"ok": True}

"""Worker: каждые 30 сек обновлять Prometheus Gauge-метрики из БД."""
from __future__ import annotations
import asyncio
import logging

from core.db import AsyncSessionLocal
from p2p.observability import metrics as p2p_metrics
from p2p.workers import reconciliation as recon

logger = logging.getLogger("p2p.observability.worker")

REFRESH_SEC = 30


async def run() -> None:
    logger.info("[obs-worker] started, refresh every %ds", REFRESH_SEC)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                summary = await p2p_metrics.refresh_gauges(db)
            logger.debug("[obs-worker] refresh ok: %s", summary)
        except Exception as e:
            logger.exception("[obs-worker] refresh failed: %s", e)

        # Дополнительно — отразить количество recon-ошибок (best-effort).
        try:
            res = await recon.run_once()
            p2p_metrics.set_recon_errors(int(res.get("count") or 0))
        except Exception:
            pass

        await asyncio.sleep(REFRESH_SEC)

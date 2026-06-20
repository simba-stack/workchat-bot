"""Structured JSON logging для P2P (ТЗ Том 18 §17).

Конфигурирует root logger так чтобы все `logger.info(...)` выходили в stdout
в формате:
    {"ts": "...", "level": "INFO", "logger": "p2p.x", "message": "...",
     "correlation_id": "...", "workflow_id": "...", "user_id": ..., ...}

extra={"correlation_id": ..., "workflow_id": ..., "user_id": ...} попадают в JSON.

Не ломает legacy logger.info(msg, arg1) — argv формат сохраняется.
"""
from __future__ import annotations
import logging
import os
import sys

_CONFIGURED = False


def _build_handler() -> logging.Handler:
    """Создать handler с JSON formatter (если pythonjsonlogger доступен)."""
    handler = logging.StreamHandler(stream=sys.stdout)
    try:
        from pythonjsonlogger import jsonlogger  # type: ignore
        fmt = jsonlogger.JsonFormatter(
            fmt=(
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(correlation_id)s %(workflow_id)s %(user_id)s"
            ),
            rename_fields={
                "asctime": "ts",
                "levelname": "level",
                "name": "logger",
            },
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    except Exception:
        # Fallback: plain text с key=val
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    handler.setFormatter(fmt)
    return handler


def setup_structured_logging(level: str | int | None = None) -> None:
    """Заменить root handler на JSON.

    Вызывается ОДНОКРАТНО из api/main.py до создания app.
    Безопасно повторного вызова (idempotent).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = level or os.environ.get("LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    # Снимаем все старые handler'ы (uvicorn ставит свои)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = _build_handler()
    root.addHandler(handler)
    root.setLevel(level)

    # Уровень noise-loggers
    for noisy in ("sqlalchemy.engine", "asyncio", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger("p2p.observability").info(
        "structured logging configured",
        extra={"correlation_id": None, "workflow_id": None, "user_id": None},
    )

"""Entry point — Alembic migrate -> bot + API в одном процессе.

Бот и сервер запускаются изолированно: если бот падает, сервер живёт
(и наоборот). Это критично для Railway healthcheck — /health должен
отвечать всегда, даже если у бота проблемы с TG-токеном или роутером.
"""
import asyncio
import logging
import os
import sys
import subprocess

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def run_migrations():
    """Запускает alembic upgrade head перед стартом сервиса."""
    logger.info("Running alembic migrations...")
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            logger.info("Migrations done")
            if result.stdout.strip():
                logger.info(result.stdout)
        else:
            logger.error("Migrations FAILED stdout=%s stderr=%s",
                         result.stdout, result.stderr)
    except Exception as e:
        logger.error("Could not run migrations: %s", e)


async def _safe_bot():
    """Запуск бота с защитой от падений — не валит main loop."""
    try:
        from bot.main import run_bot
        await run_bot()
    except Exception as e:
        logger.exception("Bot crashed: %s — продолжаем без бота", e)
        # Бесконечный sleep чтобы не выходить из asyncio.gather
        while True:
            await asyncio.sleep(3600)


async def _safe_server():
    """Запуск uvicorn-сервера. Если он упадёт — это критично."""
    try:
        from api.main import app
        port = int(os.environ.get("PORT", "8000"))
        logger.info("uvicorn starting on 0.0.0.0:%d", port)
        config = uvicorn.Config(
            app, host="0.0.0.0", port=port, log_level="info",
            access_log=False, lifespan="on",
        )
        server = uvicorn.Server(config)
        await server.serve()
    except Exception as e:
        logger.exception("Server crashed: %s", e)
        raise


async def main():
    logger.info("PRIDE P2P starting...")
    logger.info("PORT=%s DATABASE_URL=%s",
                os.environ.get("PORT", "8000"),
                "(set)" if os.environ.get("DATABASE_URL") else "(MISSING!)")

    run_migrations()

    try:
        from core.config import settings
        logger.info("Mini-App: %s%s", settings.miniapp_url, settings.miniapp_path)
        logger.info("Bot: @%s", settings.bot_username)
    except Exception as e:
        logger.warning("settings load: %s", e)

    # Сервер и бот изолированы. Server.serve() обязателен (без него
    # healthcheck умрёт). Бот в gather, но даже если он упадёт —
    # _safe_bot держит loop живым через sleep.
    await asyncio.gather(
        _safe_server(),
        _safe_bot(),
        return_exceptions=False,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")

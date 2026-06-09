"""Entry point — Alembic migrate → bot + API в одном процессе."""
import asyncio
import logging
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
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("Migrations done")
            if result.stdout.strip():
                logger.info(result.stdout)
        else:
            logger.error("Migrations FAILED: %s", result.stderr)
    except Exception as e:
        logger.error("Could not run migrations: %s", e)


async def main():
    run_migrations()

    from api.main import app
    from bot.main import run_bot
    from core.config import settings

    logger.info("PRIDE P2P starting...")
    logger.info("Mini-App: %s%s", settings.miniapp_url, settings.miniapp_path)
    logger.info("Bot: @%s", settings.bot_username)

    import os
    port = int(os.environ.get("PORT", "8000"))

    config = uvicorn.Config(
        app, host="0.0.0.0", port=port, log_level="info",
        access_log=False, lifespan="on",
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        run_bot(),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())

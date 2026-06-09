"""Entry point — поднимает aiogram bot + FastAPI uvicorn в одном процессе.

Railway даёт один контейнер — там должны жить и бот и backend Mini-App.
"""
import asyncio
import logging
import sys

import uvicorn

from api.main import app
from bot.main import run_bot
from core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("PRIDE P2P starting...")
    logger.info("Mini-App URL: %s%s", settings.miniapp_url, settings.miniapp_path)
    logger.info("Bot: @%s", settings.bot_username)

    config = uvicorn.Config(
        app, host="0.0.0.0", port=8000, log_level="info",
        access_log=False, lifespan="on",
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        run_bot(),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())

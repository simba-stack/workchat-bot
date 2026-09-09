"""
calc/main.py — entry point для calc-бота (@PrideStatistic_bot).

Запуск локально:
  CALC_BOT_TOKEN=xxx CALC_OWNER_IDS=123,456 python -m calc.main

Railway service:
  Start command: python -m calc.main
  ENV:
    CALC_BOT_TOKEN=8650100450:...
    CALC_OWNER_IDS=467916027   (tg_id SIMBA)
    CALC_DATA_DIR=/data-calc   (volume mount)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from calc.handlers import router
from calc.storage import storage

# TZ MSK
os.environ.setdefault("TZ", "Europe/Moscow")
try:
    import time as _time
    _time.tzset()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("calc.main")


async def _bootstrap_owners():
    ids_raw = os.getenv("CALC_OWNER_IDS", "").strip()
    if not ids_raw:
        return
    for x in ids_raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            await storage.add_owner(int(x))
        except ValueError:
            logger.warning("Bad CALC_OWNER_IDS item: %s", x)


async def main():
    token = os.getenv("CALC_BOT_TOKEN")
    if not token:
        raise RuntimeError("CALC_BOT_TOKEN not set")

    storage.load()
    await _bootstrap_owners()
    logger.info(
        "[calc] owners: %s, admin_chat: %s, chats: %d",
        storage.state.get("owner_ids"),
        storage.state.get("admin_chat_id"),
        len(storage.state.get("client_chats") or {}),
    )

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # skip pending updates
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("[calc] starting polling…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

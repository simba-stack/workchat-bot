"""aiogram bot entry — @PrideP2P_bot."""
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import start, kyc, groups
from core.config import settings

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Routers
    dp.include_router(start.router)
    dp.include_router(kyc.router)
    dp.include_router(groups.router)

    try:
        me = await bot.get_me()
        logger.info("PRIDE P2P bot online: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.error("getMe failed: %s", e)
        return

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()

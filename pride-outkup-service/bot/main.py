"""aiogram bot entry — @PrideP2P_bot.

Bot instance держим глобально (module-level), чтобы другие модули
(api/routers/*) могли посылать уведомления через notify().
"""
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import start, kyc, groups, wallet as wallet_h
from core.config import settings

logger = logging.getLogger(__name__)

# Глобальный bot instance — устанавливается в run_bot(), используется в notify_user()
bot: Optional[Bot] = None


async def notify_user(tg_id: int, text: str, parse_mode: str = "HTML") -> bool:
    """Безопасный wrapper — шлёт сообщение пользователю, если бот запущен.

    Возвращает True если доставлено. Не валит вызывающий код при ошибке.
    """
    if not bot:
        logger.warning("[notify_user] bot не инициализирован, skip tg=%s", tg_id)
        return False
    try:
        await bot.send_message(tg_id, text, parse_mode=parse_mode)
        return True
    except Exception as e:
        logger.warning("[notify_user] tg=%s err=%s", tg_id, e)
        return False


async def run_bot() -> None:
    global bot

    token = (settings.bot_token or "").strip()
    if not token or "REPLACE" in token or token.startswith("1234567890:"):
        logger.warning(
            "BOT_TOKEN не задан (или плейсхолдер) — pride-p2p bot НЕ запускаю. "
            "Mini-App и webhook'и работают; задай настоящий BOT_TOKEN и redeploy."
        )
        while True:
            await asyncio.sleep(3600)

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(start.router)
    dp.include_router(kyc.router)
    dp.include_router(wallet_h.router)
    dp.include_router(groups.router)

    try:
        me = await bot.get_me()
        logger.info("PRIDE P2P bot online: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.error("getMe failed: %s", e)
        while True:
            await asyncio.sleep(3600)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        bot = None

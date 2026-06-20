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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.handlers import (
    start, kyc, groups,
    wallet as wallet_h,
    commands as commands_h,
    p2p as p2p_h,
    p2p_create as p2p_create_h,
    cheques as cheques_h,
)
from core.config import settings

logger = logging.getLogger(__name__)

# Глобальный bot instance — устанавливается в run_bot(), используется в notify_user()
bot: Optional[Bot] = None


def _miniapp_link(start_param: str | None = None) -> str:
    """Deeplink в Mini App.

    Формат: https://t.me/<bot_username>/<app_short_name>?startapp=<param>
    app_short_name — это имя web-app которое добавлено в боте через @BotFather.
    Если такого нет — фолбэк на прямой URL miniapp.
    """
    bu = (settings.bot_username or "PrideP2P_bot").lstrip("@")
    app_name = "p2p"  # short name из BotFather (можно вынести в config)
    if start_param:
        return f"https://t.me/{bu}/{app_name}?startapp={start_param}"
    return f"https://t.me/{bu}/{app_name}"


def _build_kb(buttons: list[tuple[str, str]] | None) -> InlineKeyboardMarkup | None:
    """buttons: [(label, url), ...] → InlineKeyboardMarkup (1 button per row)."""
    if not buttons:
        return None
    rows = [[InlineKeyboardButton(text=label, url=url)] for label, url in buttons if label and url]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def notify_user(
    tg_id: int,
    text: str,
    parse_mode: str = "HTML",
    buttons: list[tuple[str, str]] | None = None,
) -> bool:
    """Безопасный wrapper — шлёт сообщение пользователю, если бот запущен.

    Параметры:
    - text: HTML текст
    - buttons: список (label, url) — добавит inline-клавиатуру (по 1 кнопке на строку).
      URL может быть deeplink в Mini App: _miniapp_link("trade_<id>")

    Возвращает True если доставлено. Не валит вызывающий код при ошибке.
    """
    if not bot:
        logger.warning("[notify_user] bot не инициализирован, skip tg=%s", tg_id)
        return False
    try:
        kb = _build_kb(buttons)
        await bot.send_message(tg_id, text, parse_mode=parse_mode, reply_markup=kb)
        return True
    except Exception as e:
        logger.warning("[notify_user] tg=%s err=%s", tg_id, e)
        return False


async def notify_p2p_event(
    tg_id: int,
    *,
    title: str,
    body: str | None = None,
    deeplinks: list[tuple[str, str]] | None = None,
) -> bool:
    """Карточка для P2P события.

    deeplinks: [(label, startapp_param), ...] — каждая кнопка ведёт в Mini App
    с указанным startapp. Например ("Открыть сделку", "trade_abc-123").
    """
    text = f"<b>{title}</b>"
    if body:
        text += f"\n\n{body}"
    buttons = None
    if deeplinks:
        buttons = [(lbl, _miniapp_link(p)) for lbl, p in deeplinks]
    return await notify_user(tg_id, text, buttons=buttons)


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
    # Cheques inline (@PrideP2P_bot 5 USDT за пиццу)
    dp.include_router(cheques_h.router)
    # P2P router до commands — чтобы p2p:* перехватывал callback'и
    # p2p_create регистрируем ПЕРЕД p2p, чтобы pc:* и p2p:create перехватывались
    dp.include_router(p2p_create_h.router)
    dp.include_router(p2p_h.router)
    dp.include_router(commands_h.router)
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

    # Команды бота (минималистично, без эмодзи)
    try:
        from aiogram.types import BotCommand
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="wallet", description="Кошелёк"),
            BotCommand(command="p2p", description="P2P маркет"),
            BotCommand(command="checks", description="Чеки"),
            BotCommand(command="swap", description="Обмен"),
        ])
        logger.info("Bot commands installed")
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        bot = None

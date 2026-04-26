"""
Telegram-бот на aiogram. Принимает запросы клиентов и через userbot создаёт беседы.
"""
import asyncio
import logging
import html

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

import config
from userbot import UserbotService


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
userbot = UserbotService()


WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Я создаю отдельную рабочую беседу, в которой с вами уже будут наши специалисты.\n\n"
    "Чтобы получить беседу, отправьте сообщение:\n"
    "<b>выдай рабочую беседу</b>\n\n"
    "или используйте команду /new_chat"
)


def matches_trigger(text: str) -> bool:
    if not text:
        return False
    low = text.lower().strip()
    return any(p in low for p in config.TRIGGER_PHRASES)


@dp.message(CommandStart())
async def on_start(message: Message):
    await message.answer(WELCOME_TEXT)


@dp.message(Command("help"))
async def on_help(message: Message):
    await message.answer(WELCOME_TEXT)


@dp.message(Command("new_chat"))
async def on_new_chat_cmd(message: Message):
    await create_chat_for(message)


@dp.message(F.text.func(lambda t: matches_trigger(t or "")))
async def on_trigger(message: Message):
    await create_chat_for(message)


async def create_chat_for(message: Message):
    """Создаёт беседу для пользователя, написавшего сообщение."""
    user = message.from_user
    if not user:
        return

    # Имя клиента: ФИО → username → id
    if user.full_name:
        client_name = user.full_name
    elif user.username:
        client_name = "@" + user.username
    else:
        client_name = f"user_{user.id}"

    progress = await message.answer("⏳ Создаю рабочую беседу, минутку...")

    try:
        result = await userbot.create_work_chat(client_name=client_name)
    except Exception as e:
        logger.exception("Ошибка при создании беседы")
        await progress.edit_text(
            f"❌ Не удалось создать беседу.\n\n<code>{html.escape(str(e))}</code>"
        )
        return

    # Формируем отчёт о добавлении работников (только админу)
    statuses_text = "\n".join(
        f"• @{u} — {s}" for u, s in result["statuses"].items()
    ) or "—"

    # Сообщение клиенту
    client_text = (
        f"✅ Беседа создана: <b>{html.escape(result['title'])}</b>\n\n"
        f"Перейдите по ссылке, чтобы войти:\n{result['invite_link']}\n\n"
        f"Наши специалисты уже там и ждут вас 👇"
    )
    await progress.edit_text(client_text, disable_web_page_preview=False)

    # Уведомление админа (если ID указан и это не сам админ написал)
    if config.ADMIN_ID and config.ADMIN_ID != user.id:
        try:
            await bot.send_message(
                config.ADMIN_ID,
                (
                    f"🆕 <b>Новая рабочая беседа</b>\n"
                    f"Клиент: {html.escape(client_name)} "
                    f"(id=<code>{user.id}</code>"
                    + (f", @{user.username}" if user.username else "")
                    + ")\n"
                    f"Беседа: <b>{html.escape(result['title'])}</b>\n"
                    f"Ссылка: {result['invite_link']}\n\n"
                    f"<b>Статусы работников:</b>\n{statuses_text}"
                ),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Не удалось уведомить админа: %s", e)


@dp.message()
async def on_other(message: Message):
    """Любое непонятное сообщение → подсказка."""
    await message.answer(
        "Я понимаю команду <b>выдай рабочую беседу</b> или /new_chat.\n"
        "Напишите её, чтобы создать беседу со специалистами."
    )


async def main():
    # Базовая валидация конфига
    missing = []
    if not config.BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not config.API_ID:
        missing.append("API_ID")
    if not config.API_HASH:
        missing.append("API_HASH")
    if not config.USERBOT_PHONE:
        missing.append("USERBOT_PHONE")
    if missing:
        raise SystemExit(
            f"❌ В .env не заданы: {', '.join(missing)}. "
            f"Скопируйте .env.example в .env и заполните."
        )

    logger.info("Запуск userbot...")
    await userbot.start()

    logger.info("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        await userbot.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())


"""Main entry: aiogram bot + userbot, with admin panel and cooldowns."""
import asyncio
import logging
import html

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

import config
from storage import storage
from userbot import UserbotService
from admin_router import router as admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
userbot = UserbotService()
main_router = Router()


WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Я создам для вас рабочую беседу с нашими специалистами.\n\n"
    "Чтобы получить беседу, отправьте сообщение:\n"
    "<b>выдай рабочую беседу</b>\n\n"
    "Или /new_chat"
)


@main_router.message(CommandStart())
async def on_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT)


@main_router.message(Command("help"))
async def on_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT)


@main_router.message(Command("new_chat"))
async def on_new_chat(message: Message, state: FSMContext):
    await state.clear()
    await create_chat_for(message)


@main_router.message(F.text)
async def on_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    # 1) Secret admin grant command
    secret = "/" + storage.get_secret_command()
    if text == secret:
        await storage.add_admin(message.from_user.id)
        await message.answer(
            "✅ Вы назначены админом.\n\nИспользуйте /admin для панели."
        )
        return

    # 2) Trigger phrases
    triggers = storage.get_triggers()
    low = text.lower()
    if any(p in low for p in triggers):
        await create_chat_for(message)
        return

    # 3) Fallback
    await message.answer(
        "Я понимаю команду <b>выдай рабочую беседу</b> или /new_chat."
    )


async def create_chat_for(message: Message):
    user = message.from_user
    if not user:
        return

    # Cooldown
    remaining = storage.check_cooldown(user.id)
    if remaining is not None:
        m, s = divmod(remaining, 60)
        await message.answer(
            f"⏱ Подождите ещё <b>{m} мин {s} с</b> до следующего запроса."
        )
        return

    if user.full_name:
        client_name = user.full_name
    elif user.username:
        client_name = "@" + user.username
    else:
        client_name = f"user_{user.id}"

    progress = await message.answer("⏳ Создаю рабочую беседу, минутку...")

    try:
        result = await userbot.create_work_chat(
            client_name=client_name,
            client_id=user.id,
        )
    except Exception as e:
        logger.exception("Chat creation failed")
        await progress.edit_text(
            f"❌ Не удалось создать беседу.\n<code>{html.escape(str(e))}</code>"
        )
        return

    await storage.mark_creation(user.id)

    statuses_text = "\n".join(
        f"• @{u} — {s}" for u, s in result["statuses"].items()
    ) or "—"

    client_text = (
        f"✅ Беседа создана: <b>{html.escape(result['title'])}</b>\n\n"
        f"Перейдите по ссылке: {result['invite_link']}\n\n"
        f"Наши специалисты ждут вас 👇"
    )
    await progress.edit_text(client_text, disable_web_page_preview=False)

    # Notify admins
    for admin_id in storage.get_admins():
        if admin_id == user.id:
            continue
        try:
            await bot.send_message(
                admin_id,
                (
                    f"🆕 <b>Новая рабочая беседа</b>\n"
                    f"Клиент: {html.escape(client_name)} "
                    f"(id=<code>{user.id}</code>"
                    + (f", @{user.username}" if user.username else "")
                    + ")\n"
                    f"Беседа: <b>{html.escape(result['title'])}</b>\n"
                    f"Ссылка: {result['invite_link']}\n\n"
                    f"<b>Работники:</b>\n{statuses_text}"
                ),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Admin notify failed (%s): %s", admin_id, e)


async def main():
    missing = []
    if not config.BOT_TOKEN: missing.append("BOT_TOKEN")
    if not config.API_ID: missing.append("API_ID")
    if not config.API_HASH: missing.append("API_HASH")
    if missing:
        raise SystemExit(f"❌ Не заданы переменные: {', '.join(missing)}")

    logger.info("Loading storage from %s ...", config.STORAGE_PATH)
    await storage.load()
    logger.info("Secret admin command: /%s", storage.get_secret_command())
    logger.info("Admins: %s", storage.get_admins())

    logger.info("Starting userbot...")
    await userbot.start()

    # Order: admin_router first (FSM + /admin), then main (catch-all)
    dp.include_router(admin_router)
    dp.include_router(main_router)

    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await userbot.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

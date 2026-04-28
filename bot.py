"""Main entry: aiogram bot + userbot, with admin panel, cooldowns, captcha."""
import asyncio
import logging
import html
import random

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    User as TgUser,
)

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

CAPTCHA_MAX_ATTEMPTS = 3

# Интервал периодической очистки storage (раз в 6 часов)
_CLEANUP_INTERVAL_SEC = 6 * 3600


class CaptchaFSM(StatesGroup):
    waiting = State()


def _gen_captcha() -> tuple[str, int, list[int]]:
    """Возвращает (текст_примера, правильный_ответ, 4_варианта_перемешанных)."""
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    if random.random() < 0.5:
        text = f"{a} + {b}"
        ans = a + b
    else:
        if b > a:
            a, b = b, a
        text = f"{a} − {b}"
        ans = a - b
    wrong: set[int] = set()
    while len(wrong) < 3:
        delta = random.randint(-4, 4)
        if delta == 0:
            continue
        w = ans + delta
        if w < 0 or w == ans:
            continue
        wrong.add(w)
    options = [ans, *wrong]
    random.shuffle(options)
    return text, ans, options


def _captcha_kb(options: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(o), callback_data=f"cap:{o}")
        for o in options
    ]])


async def _start_captcha(message: Message, state: FSMContext):
    text, ans, options = _gen_captcha()
    await message.answer(
        "🤖 Подтвердите, что вы не бот.\n\n"
        f"Решите пример: <b>{text} = ?</b>",
        reply_markup=_captcha_kb(options),
    )
    await state.set_state(CaptchaFSM.waiting)
    await state.update_data(captcha_answer=ans, captcha_attempts=0)


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
    await _start_captcha(message, state)


@main_router.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    # 1) Секретная команда для назначения первого админа
    secret = "/" + storage.get_secret_command()
    if text == secret:
        await storage.add_admin(message.from_user.id)
        await message.answer(
            "✅ Вы назначены админом.\n\nИспользуйте /admin для панели."
        )
        return

    # 2) Триггерные фразы → капча → кулдаун → беседа
    triggers = storage.get_triggers()
    low = text.lower()
    if any(p in low for p in triggers):
        await _start_captcha(message, state)
        return

    # 3) Fallback
    await message.answer(
        "Я понимаю команду <b>выдай рабочую беседу</b> или /new_chat."
    )


@main_router.callback_query(CaptchaFSM.waiting, F.data.startswith("cap:"))
async def on_captcha_answer(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    expected = data.get("captcha_answer")
    attempts = int(data.get("captcha_attempts", 0))
    try:
        chosen = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return

    if chosen == expected:
        await state.clear()
        try:
            await call.message.edit_text("✅ Капча пройдена. Создаю рабочую беседу…")
        except Exception:
            pass
        await call.answer()
        await _create_chat_for_user(call.message.chat.id, call.from_user)
        return

    attempts += 1
    if attempts >= CAPTCHA_MAX_ATTEMPTS:
        await state.clear()
        try:
            await call.message.edit_text(
                f"❌ Слишком много неправильных ответов ({CAPTCHA_MAX_ATTEMPTS}/{CAPTCHA_MAX_ATTEMPTS}).\n"
                "Отправьте триггер ещё раз, чтобы получить новую капчу."
            )
        except Exception:
            pass
        await call.answer("Капча провалена", show_alert=True)
        return

    text, ans, options = _gen_captcha()
    try:
        await call.message.edit_text(
            f"❌ Неверно. Попытка {attempts}/{CAPTCHA_MAX_ATTEMPTS}.\n\n"
            f"Новый пример: <b>{text} = ?</b>",
            reply_markup=_captcha_kb(options),
        )
    except Exception:
        pass
    await state.update_data(captcha_answer=ans, captcha_attempts=attempts)
    await call.answer("Неверно")


async def _safe_send(chat_id: int, text: str, **kwargs) -> bool:
    """Отправляет сообщение с автоматическим retry при FloodWait."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramRetryAfter as e:
        logger.warning("FloodWait %ds when sending to %s — retrying once", e.retry_after, chat_id)
        await asyncio.sleep(e.retry_after + 1)
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except Exception as retry_e:
            logger.warning("Retry failed for %s: %s", chat_id, retry_e)
            return False
    except Exception as e:
        logger.warning("send_message to %s failed: %s", chat_id, e)
        return False


async def _create_chat_for_user(reply_chat_id: int, user: TgUser):
    """Создаёт беседу. reply_chat_id — куда писать ответы клиенту."""
    if not user:
        return

    # Проверка кулдауна (после успешной капчи)
    remaining = storage.check_cooldown(user.id)
    if remaining is not None:
        m, s = divmod(remaining, 60)
        await _safe_send(
            reply_chat_id,
            f"⏱ Подождите ещё <b>{m} мин {s} с</b> до следующего запроса.",
        )
        return

    if user.full_name:
        client_name = user.full_name
    elif user.username:
        client_name = "@" + user.username
    else:
        client_name = f"user_{user.id}"

    progress = await bot.send_message(reply_chat_id, "⏳ Создаю рабочую беседу, минутку...")

    try:
        result = await userbot.create_work_chat(
            client_name=client_name,
            client_id=user.id,
        )
    except Exception as e:
        logger.exception("Chat creation failed")
        try:
            await progress.edit_text(
                f"❌ Не удалось создать беседу.\n<code>{html.escape(str(e))}</code>"
            )
        except Exception:
            pass
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
    try:
        await progress.edit_text(client_text, disable_web_page_preview=False)
    except Exception:
        pass

    # Уведомляем всех админов (с обработкой FloodWait)
    for admin_id in storage.get_admins():
        if admin_id == user.id:
            continue
        await _safe_send(
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


async def _periodic_cleanup():
    """Фоновая задача: раз в _CLEANUP_INTERVAL_SEC чистит storage от старых записей."""
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SEC)
        try:
            await storage.cleanup()
        except Exception as e:
            logger.warning("Periodic cleanup failed: %s", e)


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

    # Порядок важен: admin_router сначала (FSM + /admin), потом main (catch-all)
    dp.include_router(admin_router)
    dp.include_router(main_router)

    # Запускаем фоновую очистку storage
    asyncio.create_task(_periodic_cleanup())

    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await userbot.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

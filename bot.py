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


# === Опрос источника трафика на /start ===
SOURCES = [
    "Continental",
    "FRK",
    "BG CHAT",
    "MARKET 404",
    "ZLOY ZAM",
    "VERA CHAT",
    "BRO CHAT",
    "GRAND",
    "ШТУРМ",
    "ПОСОВЕТОВАЛИ",
]

START_GREETING = (
    "👋 Здравствуйте!\n\n"
    "Перед тем как создать рабочую беседу — подскажите, "
    "<b>где вы о нас узнали?</b>"
)

WELCOME_AFTER_SURVEY = (
    "Спасибо!\n\n"
    "Чтобы получить рабочую беседу с нашими специалистами — "
    "нажмите кнопку ниже."
)

CAPTCHA_MAX_ATTEMPTS = 3

# Интервал периодической очистки storage (раз в 6 часов)
_CLEANUP_INTERVAL_SEC = 6 * 3600


def _sources_kb() -> InlineKeyboardMarkup:
    """Клавиатура с 10 источниками (5 рядов по 2 кнопки)."""
    rows = []
    for i in range(0, len(SOURCES), 2):
        row = [InlineKeyboardButton(text=SOURCES[i], callback_data=f"src:{i}")]
        if i + 1 < len(SOURCES):
            row.append(InlineKeyboardButton(text=SOURCES[i + 1], callback_data=f"src:{i + 1}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_chat_kb() -> InlineKeyboardMarkup:
    """Кнопка «Получить рабочую беседу» — ведёт на капчу."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📩 Получить рабочую беседу", callback_data="gw:get")
    ]])


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


def _make_captcha_text() -> tuple[str, InlineKeyboardMarkup, int]:
    """Готовит текст + клавиатуру + правильный ответ капчи. Side-effect-free."""
    text, ans, options = _gen_captcha()
    msg = (
        "🤖 Подтвердите, что вы не бот.\n\n"
        f"Решите пример: <b>{text} = ?</b>"
    )
    return msg, _captcha_kb(options), ans


async def _start_captcha(message: Message, state: FSMContext):
    """Показ капчи новым сообщением (триггерные фразы)."""
    msg, kb, ans = _make_captcha_text()
    await message.answer(msg, reply_markup=kb)
    await state.set_state(CaptchaFSM.waiting)
    await state.update_data(captcha_answer=ans, captcha_attempts=0)


async def _start_captcha_inline(call: CallbackQuery, state: FSMContext):
    """Показ капчи редактированием текущего сообщения (после клика по inline-кнопке)."""
    msg, kb, ans = _make_captcha_text()
    try:
        await call.message.edit_text(msg, reply_markup=kb)
    except Exception:
        await call.message.answer(msg, reply_markup=kb)
    await state.set_state(CaptchaFSM.waiting)
    await state.update_data(captcha_answer=ans, captcha_attempts=0)


async def _send_post_survey(message_or_call, state: FSMContext):
    """Отправляет welcome-сообщение с кнопкой «Получить рабочую беседу».
    Принимает либо Message (новый месседж), либо CallbackQuery (edit_text)."""
    await state.clear()
    if isinstance(message_or_call, CallbackQuery):
        try:
            await message_or_call.message.edit_text(
                WELCOME_AFTER_SURVEY, reply_markup=_get_chat_kb()
            )
        except Exception:
            await message_or_call.message.answer(
                WELCOME_AFTER_SURVEY, reply_markup=_get_chat_kb()
            )
    else:
        await message_or_call.answer(
            WELCOME_AFTER_SURVEY, reply_markup=_get_chat_kb()
        )


@main_router.message(CommandStart())
async def on_start(message: Message, state: FSMContext):
    await state.clear()
    # Если пользователь уже был атрибутирован — пропускаем опрос,
    # сразу даём кнопку «Получить рабочую беседу».
    if storage.get_user_source(message.from_user.id):
        await _send_post_survey(message, state)
        return
    await message.answer(START_GREETING, reply_markup=_sources_kb())


@main_router.message(Command("help"))
async def on_help(message: Message, state: FSMContext):
    await state.clear()
    await on_start(message, state)


@main_router.callback_query(F.data.startswith("src:"))
async def on_source_pick(call: CallbackQuery, state: FSMContext):
    """Пользователь выбрал источник трафика."""
    try:
        idx = int(call.data.split(":", 1)[1])
        source = SOURCES[idx]
    except (ValueError, IndexError):
        await call.answer()
        return
    recorded = await storage.register_source(call.from_user.id, source)
    if recorded:
        logger.info("Source attributed: user=%s -> %s", call.from_user.id, source)
        await call.answer(f"Спасибо! Источник: {source}")
    else:
        await call.answer()
    await _send_post_survey(call, state)


@main_router.callback_query(F.data == "gw:get")
async def on_get_chat_clicked(call: CallbackQuery, state: FSMContext):
    """Пользователь нажал «Получить рабочую беседу» → показываем капчу."""
    await call.answer()
    await _start_captcha_inline(call, state)


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

    # 2) Триггерные фразы → капча → кулдаун → беседа (legacy fallback)
    triggers = storage.get_triggers()
    low = text.lower()
    if any(p in low for p in triggers):
        await _start_captcha(message, state)
        return

    # 3) Fallback — отправляем на /start
    await message.answer(
        "Чтобы получить рабочую беседу — отправьте /start.",
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

"""Main entry: aiogram bot + userbot, with admin panel, cooldowns, captcha."""
import asyncio
import logging
import html
import os
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
    MessageEntity,
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

# Главный welcome-баннер @PrideInviteWork_bot.
# Шаблон: {nick} подставится через format() из first_name пользователя.
WELCOME_BANNER = (
    "👋 <b>{nick}</b>, добро пожаловать в <b>ЭКО-СИСТЕМУ PRIDE</b>!\n\n"
    "🤝 Безупречная репутация и <b>120.000 $ +</b> сделок в Continental.\n\n"
    "👉 <b>Наши ресурсы:</b>\n"
    "🟢 @pride_projectv2\n\n"
    "👉 Чтобы начать сотрудничество с нашей Эко-Системой, просто нажмите "
    "на кнопку <b>«Подключиться»</b> ниже этого сообщения и следуйте инструкциям.\n\n"
    "🧑‍🚒 <b>Набор сотрудников</b> — актуальный список вакансий в разделе "
    "<b>«Вакансии»</b>."
)

# Эмодзи которые могут быть premium (заменяются на custom_emoji если в storage
# заданы document_id'ы). Порядок важен — entities должны быть в порядке
# появления в тексте. SIMBA может привязать premium document_id'ы через JARVIS.
WELCOME_PREMIUM_EMOJI_SLOTS = ["👋", "🤝", "👉", "🟢", "👉", "🧑\u200d🚒"]

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
    """Кнопка «Получить рабочую беседу» — ведёт на капчу.
    Legacy — оставлено для совместимости со старым flow после source survey."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📩 Получить рабочую беседу", callback_data="gw:get")
    ]])


def _welcome_kb() -> InlineKeyboardMarkup:
    """Главная клавиатура welcome-баннера PrideInviteWork_bot."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключиться", callback_data="iw:sell")],
        [InlineKeyboardButton(text="💼 Вакансии",   callback_data="iw:jobs")],
        [
            InlineKeyboardButton(text="💬 ЧАТ PRIDE",  url="https://t.me/pride_projectv2"),
            InlineKeyboardButton(text="📢 КАНАЛ PRIDE", url="https://t.me/pride_projectv2"),
        ],
    ])


def _back_to_welcome_kb() -> InlineKeyboardMarkup:
    """Кнопка ← Назад в главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="iw:back"),
    ]])


def _build_welcome_entities(text: str) -> list[MessageEntity]:
    """Строит MessageEntity[type='custom_emoji'] для каждого эмодзи из
    WELCOME_PREMIUM_EMOJI_SLOTS если в storage задан его document_id.

    Возвращает массив сущностей с правильными offset/length (в UTF-16 code
    units — как требует Telegram). Если premium-эмодзи не настроены, вернёт
    пустой список (текст останется с обычными эмодзи)."""
    emoji_map = storage.get_invite_premium_emoji() or {}
    if not emoji_map:
        return []
    entities = []
    # UTF-16 offsets — Telegram считает в code units (BMP=1, не-BMP=2).
    # Эмодзи 🔥🤝 и пр. — в supplementary plane, занимают 2 code units.
    # Простой счётчик через encode('utf-16-le').
    for emoji_char in WELCOME_PREMIUM_EMOJI_SLOTS:
        doc_id = emoji_map.get(emoji_char)
        if not doc_id:
            continue
        # Найти первое появление эмодзи в тексте
        idx_chars = text.find(emoji_char)
        if idx_chars < 0:
            continue
        # Сконвертировать char-offset в UTF-16 code units
        prefix = text[:idx_chars]
        offset_u16 = len(prefix.encode("utf-16-le")) // 2
        length_u16 = len(emoji_char.encode("utf-16-le")) // 2
        try:
            entities.append(MessageEntity(
                type="custom_emoji",
                offset=offset_u16,
                length=length_u16,
                custom_emoji_id=str(doc_id),
            ))
        except Exception as e:
            logger.debug("MessageEntity build failed for %s: %s", emoji_char, e)
    return entities


async def _send_welcome_banner(target_chat_id: int, user: TgUser) -> bool:
    """Отправляет главный welcome-баннер: GIF (если настроен) + текст с
    premium-emoji-entities + клавиатура. Возвращает True если успешно."""
    nick = (user.first_name or user.username or "друг").strip()
    text = WELCOME_BANNER.format(nick=html.escape(nick))
    entities = _build_welcome_entities(text)
    kb = _welcome_kb()
    gif_id = storage.get_invite_welcome_gif()

    try:
        if gif_id:
            # send_animation поддерживает caption + entities + reply_markup
            await bot.send_animation(
                chat_id=target_chat_id,
                animation=gif_id,
                caption=text,
                caption_entities=entities or None,
                reply_markup=kb,
            )
        else:
            await bot.send_message(
                chat_id=target_chat_id,
                text=text,
                entities=entities or None,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        return True
    except Exception as e:
        logger.warning("welcome banner send failed: %s", e)
        # Fallback — без GIF/entities
        try:
            await bot.send_message(
                chat_id=target_chat_id,
                text=text,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            return True
        except Exception:
            return False


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
    # Воронка: считаем нажатия /start
    try:
        await storage.bump_funnel("starts")
    except Exception:
        pass
    # Track bot user (для рассылок)
    try:
        await storage.track_bot_user(
            message.from_user.id,
            first_name=message.from_user.first_name or "",
            username=message.from_user.username or "",
        )
    except Exception:
        pass
    # Новый welcome — главное меню сразу, без сухого «где о нас узнали».
    # Source survey теперь сидит ВНУТРИ кнопки «Продать ИП» (iw:sell).
    await _send_welcome_banner(message.chat.id, message.from_user)


# ───── Главное меню InviteWork: callbacks ────────────────────────

@main_router.callback_query(F.data == "iw:sell")
async def on_invite_sell(call: CallbackQuery, state: FSMContext):
    """«Продать ИП» — если источник ещё не зафиксирован, спрашиваем; иначе
    сразу даём кнопку получения рабочей беседы."""
    await call.answer()
    if storage.get_user_source(call.from_user.id):
        # Сразу показываем кнопку получения беседы
        try:
            await call.message.edit_text(
                WELCOME_AFTER_SURVEY,
                reply_markup=_get_chat_kb(),
            )
        except Exception:
            await call.message.answer(
                WELCOME_AFTER_SURVEY,
                reply_markup=_get_chat_kb(),
            )
        return
    # Иначе — опрос «где узнали»
    try:
        await call.message.edit_text(START_GREETING, reply_markup=_sources_kb())
    except Exception:
        await call.message.answer(START_GREETING, reply_markup=_sources_kb())


@main_router.callback_query(F.data == "iw:jobs")
async def on_invite_jobs(call: CallbackQuery, state: FSMContext):
    """«Вакансии» — показываем редактируемый из админки текст."""
    await call.answer()
    text = storage.get_invite_jobs_text()
    try:
        await call.message.edit_text(
            text, reply_markup=_back_to_welcome_kb(),
            disable_web_page_preview=True,
        )
    except Exception:
        await call.message.answer(
            text, reply_markup=_back_to_welcome_kb(),
            disable_web_page_preview=True,
        )


@main_router.callback_query(F.data == "iw:back")
async def on_invite_back(call: CallbackQuery, state: FSMContext):
    """Назад в главное меню welcome."""
    await call.answer()
    # При edit_text нельзя добавить GIF — поэтому если приходим с подэкрана,
    # просто меняем текст обратно на welcome (без GIF) с теми же кнопками.
    nick = (call.from_user.first_name or call.from_user.username or "друг").strip()
    text = WELCOME_BANNER.format(nick=html.escape(nick))
    entities = _build_welcome_entities(text)
    try:
        await call.message.edit_text(
            text, entities=entities or None,
            reply_markup=_welcome_kb(),
            disable_web_page_preview=True,
        )
    except Exception:
        await _send_welcome_banner(call.message.chat.id, call.from_user)


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

    # ── Авто-миграция из старой Node.js CRM ───────────────────────
    # Если в репо есть crm.sql и миграция ещё не запускалась — прогоним.
    # Флаг `crm_migration_done` хранится в state.json (per-volume).
    try:
        if (
            os.path.exists("crm.sql")
            and not storage.state.get("crm_migration_done")
        ):
            logger.info("🔄 CRM migration: detected crm.sql, applying...")
            try:
                from migrate_from_old_crm import parse_sql_dump, migrate
                dump = parse_sql_dump("crm.sql")
                report = migrate(storage.state, dump, dry_run=False)
                storage.state["crm_migration_done"] = True
                storage.state["crm_migration_report"] = report
                await storage._save_unlocked()
                logger.info(
                    "✅ CRM migration: +%d owners / +%d chats / +%d drops / +%d lks",
                    report["owners_added"], report["chats_added"],
                    report["drops_added"], report["lks_added"],
                )
            except Exception as e:
                logger.error("CRM migration failed: %s — continuing without it", e)
        elif storage.state.get("crm_migration_done"):
            logger.info("CRM migration: already applied (skipping)")
    except Exception as e:
        logger.warning("CRM migration check failed: %s", e)

    logger.info("Starting userbot...")
    await userbot.start()

    # Порядок важен: admin_router сначала (FSM + /admin), потом main (catch-all)
    dp.include_router(admin_router)
    dp.include_router(main_router)

    # Запускаем фоновую очистку storage
    asyncio.create_task(_periodic_cleanup())

    # FastAPI дашборд — запускаем параллельно. При любой ошибке/отсутствии
    # модулей дашборд просто не стартует, бот продолжает работать как обычно.
    dashboard_task = asyncio.create_task(_start_dashboard_api())

    # CRM-бот — отдельный aiogram bot для управления партнёрами/дропами.
    # Запускается в том же процессе как async task — делит storage с main.
    # При краше CRM (try/except) main продолжает работать.
    crm_task = None
    try:
        from crm_bot import run_crm_bot
        crm_task = asyncio.create_task(_safe_crm_task())
        logger.info("CRM bot task created")
    except Exception as e:
        logger.warning("CRM bot module load failed: %s", e)

    # === Outsource bot (@marketplace_PRIDE_BOT — лавка PRIDE для управляющих) ===
    outsource_task = None
    try:
        from outsource_bot import run_outsource_bot  # noqa: F401
        outsource_task = asyncio.create_task(_safe_outsource_task())
        logger.info("Outsource bot task created")
    except Exception as e:
        logger.warning("Outsource bot module load failed: %s", e)

    # === Tron monitor (auto-credit USDT TRC20 пополнений) ===
    tron_monitor_task = None
    try:
        tron_monitor_task = asyncio.create_task(_safe_tron_monitor_task())
        logger.info("Tron monitor task created")
    except Exception as e:
        logger.warning("Tron monitor load failed: %s", e)

    # === Guard bot (@PrideGuard_bot — 2FA подтверждение крупных выплат) ===
    guard_bot_task = None
    try:
        from guard_bot import run_guard_bot

        async def _safe_guard_bot_task():
            try:
                await run_guard_bot()
            except Exception as e:
                logger.exception("guard_bot task crashed: %s", e)

        guard_bot_task = asyncio.create_task(_safe_guard_bot_task())
        logger.info("Guard bot task created")
    except Exception as e:
        logger.warning("Guard bot module load failed: %s", e)

    # === HEALTHCHECK на старте ===
    # Прогоняем все системы и шлём отчёт в HEALTH_CHAT_ID (если задан).
    # Делается в фоне чтобы не блокировать запуск polling.
    asyncio.create_task(_run_startup_healthcheck())

    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        if not dashboard_task.done():
            dashboard_task.cancel()
        if crm_task and not crm_task.done():
            crm_task.cancel()
        if outsource_task and not outsource_task.done():
            outsource_task.cancel()
        if tron_monitor_task and not tron_monitor_task.done():
            tron_monitor_task.cancel()
        try:
            await userbot.stop()
        except Exception as e:
            logger.warning("Userbot stop error: %s", e)


async def _run_startup_healthcheck():
    """На старте бота прогоняет health_check и постит отчёт в HEALTH_CHAT_ID.

    HEALTH_CHAT_ID — env-переменная с chat_id куда слать отчёты. Если пусто,
    fallback на ADMIN_ID (личка владельца).
    """
    # Дать всему остальному встать перед healthcheck
    await asyncio.sleep(5)
    try:
        from health_check import HealthChecker
        h = HealthChecker()
        await h.run_all()
        text = h.format_telegram_message()
        target = int(
            os.getenv("HEALTH_CHAT_ID", "0") or
            config.ADMIN_ID or 0
        )
        if not target:
            logger.warning(
                "HEALTH_CHAT_ID не задан и ADMIN_ID = 0 — healthcheck не отправлен"
            )
            return
        await bot.send_message(target, text, parse_mode="HTML",
                               disable_web_page_preview=True)
        logger.info("Healthcheck sent to chat %s", target)
    except Exception as e:
        logger.exception("startup healthcheck failed: %s", e)


async def _safe_crm_task():
    try:
        from crm_bot import run_crm_bot
        await run_crm_bot()
    except Exception as e:
        logger.error("CRM bot crashed: %s — main bot continues", e)


async def _safe_outsource_task():
    try:
        from outsource_bot import run_outsource_bot
        await run_outsource_bot()
    except Exception as e:
        logger.error("Outsource bot crashed: %s — main bot continues", e)


async def _safe_tron_monitor_task():
    """Auto-credit USDT TRC20 пополнений для @marketplace_PRIDE_BOT.

    Ждём пока outsource_bot инициализируется и регистрирует свой Bot instance,
    потом передаём его в монитор для отправки уведомлений юзерам.
    """
    try:
        # Даём боту время инициализироваться (получить инстанс)
        await asyncio.sleep(8)
        from outsource_bot import get_outsource_bot
        from tron_monitor import run_tron_monitor
        bot = get_outsource_bot()
        await run_tron_monitor(bot=bot)
    except Exception as e:
        logger.error("Tron monitor crashed: %s — main bot continues", e)


async def _start_dashboard_api():
    """Запускает FastAPI дашборд параллельно с ботом."""
    try:
        import uvicorn
        from api import app
        port = int(os.getenv("PORT", "8000"))
        config_ = uvicorn.Config(
            app, host="0.0.0.0", port=port, log_level="info",
            access_log=False, lifespan="on",
        )
        server = uvicorn.Server(config_)
        await server.serve()
    except Exception as e:
        logger.warning("Dashboard API crashed: %s", e)


if __name__ == "__main__":
    asyncio.run(main())

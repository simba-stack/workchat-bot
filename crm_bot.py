"""
PRIDE CRM Bot — ОТДЕЛЬНЫЙ aiogram-бот, деплоится как самостоятельный
Railway service из того же репо.

═══════════════════════════════════════════════════════════════════
ИЗОЛЯЦИЯ
═══════════════════════════════════════════════════════════════════
- Своя start command:  python crm_bot.py
- Свой state файл:     /app/data/crm_state.json (env CRM_STORAGE_PATH)
- Свой Railway сервис
- НЕ зависит от main bot.py (если main крашится — CRM работает)
- НЕ влияет на main (если crm крашится — main работает)
- Интеграция с экосистемой PRIDE — позже через API-вызовы (этап 5+)

═══════════════════════════════════════════════════════════════════
КОНЦЕПЦИИ
═══════════════════════════════════════════════════════════════════
Owner (поставщик)     — TG-юзер который продаёт нам РС
Drop (клиент)         — ФИО + анкета + документы
DropLK (ЛК банка)     — банк + value(логин/пароль) + sms_history + дедик
CRM Chat              — групповой чат-партнёр привязан к Owner'у
Admin chat            — куда падают новые дропы для обработки
Password chat         — где админы PRIDE заполняют RDP / пароли

═══════════════════════════════════════════════════════════════════
КОМАНДЫ (Этап 1)
═══════════════════════════════════════════════════════════════════
В ЛС бота:
  /start          — регистрация как поставщик + личный профиль
  /profile        — посмотреть свой профиль
  /clients        — список моих дропов (с inline-кнопками)
  /help           — помощь

В рабочей группе (CRM chat):
  /clients        — список дропов закреплённых за этой группой
  /profile        — профиль поставщика этой группы

Команды настройки (только SIMBA):
  /crm_register_chat @username  — закрепить эту группу за поставщиком
  /crm_set_admin                — пометить как admin-чат CRM
  /crm_set_password             — пометить как password-чат
  /crm_unregister               — снять закрепление группы
  /crm_info                     — статус CRM
"""

import asyncio
import logging
import os
import time
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# Используем ОБЩИЙ storage с main-ботом (через тот же state.json).
# Это даёт мгновенную интеграцию: дроп создан в CRM → его сразу видит
# дашборд / userbot / API. Никаких HTTP-вызовов между процессами.
from storage import storage as crm_storage

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# КОНФИГ
# ════════════════════════════════════════════════════════════════

# Bot token — зашит в код (по запросу владельца).
# Если есть env CRM_BOT_TOKEN — она переопределит.
_HARDCODED_TOKEN = "8929170452:AAE6zXBd80CL4CaKSqNgilBiMBKV1lPMCJ8"
CRM_BOT_TOKEN = os.getenv("CRM_BOT_TOKEN") or _HARDCODED_TOKEN

# Владелец CRM — только он может настраивать (set_admin / register_chat).
_HARDCODED_OWNER_IDS = {8151738775, 397572312, 5830088389, 8328099603}
CRM_OWNER_IDS = set(_HARDCODED_OWNER_IDS)
_env_owner = os.getenv("CRM_OWNER_TG_ID", "")
if _env_owner:
    try:
        CRM_OWNER_IDS.add(int(_env_owner.strip()))
    except Exception:
        pass

EPHEMERAL_TTL = 5

# ID групп — захардкожены по запросу владельца.
# Чтобы переопределить — установи env CRM_ADMIN_CHAT_ID / CRM_PASSWORD_CHAT_ID.
# Бот должен быть В этих группах + админ.
HARDCODED_ADMIN_CHAT_ID = -1003852131311     # «Доступы» PRIDE — приёмка дропов
HARDCODED_PASSWORD_CHAT_ID = -1003788743917  # «Пароли» PRIDE — RDP + новые пароли


def get_admin_chat_id() -> int:
    """Резолв admin-чата. Приоритет: env → storage → hardcoded."""
    env = os.getenv("CRM_ADMIN_CHAT_ID", "").strip()
    if env and env.lstrip("-").isdigit():
        return int(env)
    fr = crm_storage.find_crm_admin_chat()
    if fr:
        return int(fr)
    return HARDCODED_ADMIN_CHAT_ID


def get_password_chat_id() -> int:
    env = os.getenv("CRM_PASSWORD_CHAT_ID", "").strip()
    if env and env.lstrip("-").isdigit():
        return int(env)
    fr = crm_storage.find_crm_password_chat()
    if fr:
        return int(fr)
    return HARDCODED_PASSWORD_CHAT_ID


async def get_admin_chat_resolved(bot) -> Optional[int]:
    """Получает admin-chat_id и проверяет доступность. Авто-корректирует если нужно."""
    cid = get_admin_chat_id()
    if not cid:
        return None
    resolved = await _resolve_chat_id_variants(bot, cid)
    if resolved and resolved != cid:
        try:
            await crm_storage.register_crm_chat(
                chat_id=resolved, owner_id="_admin",
                is_admin=True, is_password=False,
            )
        except Exception:
            pass
    return resolved


async def get_password_chat_resolved(bot) -> Optional[int]:
    """Получает password-chat_id и проверяет доступность. Авто-корректирует."""
    cid = get_password_chat_id()
    if not cid:
        return None
    resolved = await _resolve_chat_id_variants(bot, cid)
    if resolved and resolved != cid:
        try:
            await crm_storage.register_crm_chat(
                chat_id=resolved, owner_id="_password",
                is_admin=False, is_password=True,
            )
        except Exception:
            pass
    return resolved


async def _resolve_chat_id_variants(bot, raw_id: int) -> Optional[int]:
    """Пытается достучаться до чата перебором форматов id.
    Telegram bot API хочет:
      • supergroup: -100XXXXXXXXXX (13 цифр после знака)
      • basic group: -XXXXXXXX (просто отрицательный)
      • channel: -100XXXXXXXXXX
    Если пользователь ввёл голый id без префикса — пробуем все варианты.
    Возвращает рабочий int chat_id или None."""
    if not bot:
        return None
    raw = str(raw_id).lstrip("-")
    candidates = []
    candidates.append(int(raw_id))                          # как есть
    if not raw.startswith("100"):
        candidates.append(-int(f"100{raw}"))                # -100ID
    candidates.append(-int(raw))                            # -ID
    candidates.append(int(raw))                             # bare positive
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        try:
            await bot.get_chat(c)
            logger.info("[crm] resolved admin chat: %s", c)
            return c
        except Exception:
            continue
    return None


# ════════════════════════════════════════════════════════════════
# FSM States
# ════════════════════════════════════════════════════════════════

class DropForm(StatesGroup):
    waiting_fio = State()
    waiting_about = State()       # legacy — оставлен для совместимости
    waiting_scan = State()
    # Новые поля анкеты
    waiting_social = State()
    waiting_residence = State()
    waiting_other_banks = State()


class LKForm(StatesGroup):
    waiting_bank = State()
    waiting_value = State()


class FillForm(StatesGroup):
    waiting_new_login = State()       # 1/8
    waiting_new_password = State()    # 2/8
    waiting_new_mail = State()        # 3/8
    waiting_new_number = State()      # 4/8
    waiting_code_word = State()       # 5/8
    waiting_ded_location = State()    # 6/8
    waiting_ded_ip = State()          # 7/8
    waiting_ded_pass = State()        # 8/8


class SMSForm(StatesGroup):
    waiting_code = State()


class PriceForm(StatesGroup):
    waiting_price = State()


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════

def is_owner(user_id: int) -> bool:
    return int(user_id) in CRM_OWNER_IDS


def is_pride_registered(user_id: int, username: str = "") -> bool:
    """Проверка: юзер зарегистрирован в экосистеме PRIDE.

    Это обязательное условие пользования CRM-ботом.
    True если:
      • Юзер — владелец CRM (SIMBA и др. админы)
      • Юзер делал /start у main-бота (есть в bot_users)
      • У юзера @username привязан к managed_chat (взаимодействовал с ассистентом)
      • Юзер — client_id какого-то managed_chat (т.е. создавал/состоит в work-чате)
    """
    if not user_id:
        return False
    # 1. Владельцы CRM — всегда допускаются (для настройки/тестов)
    if int(user_id) in CRM_OWNER_IDS:
        return True
    # 2. Делал /start у main-бота PRIDE
    try:
        bot_users = crm_storage.state.get("bot_users") or {}
        if str(user_id) in bot_users or int(user_id) in {int(k) for k in bot_users.keys() if str(k).isdigit()}:
            return True
    except Exception:
        pass
    # 3. @username в индексе клиентов ассистента
    if username:
        try:
            cid = crm_storage.find_chat_by_client_username(username)
            if cid:
                return True
        except Exception:
            pass
    # 4. user_id фигурирует как client_id в managed_chats
    try:
        managed = crm_storage.state.get("managed_chats") or {}
        for info in managed.values():
            try:
                if int(info.get("client_id") or 0) == int(user_id):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _require_pride(message: Message) -> bool:
    """Проверка регистрации в PRIDE; если нет — отказ + объяснение.
    Возвращает True если можно продолжать."""
    user = message.from_user
    if not user:
        return False
    if is_pride_registered(user.id, user.username or ""):
        return True
    await message.reply(
        "🔒 <b>Доступ закрыт</b>\n\n"
        "Чтобы пользоваться CRM, нужно сначала быть зарегистрированным в "
        "системе <b>PRIDE</b>.\n\n"
        "<b>Что делать:</b>\n"
        "1. Напишите боту @PrideInviteWork_bot команду /start\n"
        "   <i>(там создаётся ваша рабочая беседа с ассистентом)</i>\n"
        "2. Дождитесь приглашения в рабочий чат с ассистентом\n"
        "3. Тогда вернитесь сюда и снова напишите /start\n\n"
        "Если уже есть рабочая беседа — попросите владельца добавить вас."
    )
    return False


async def ephemeral(message: Message, text: str, ttl: int = EPHEMERAL_TTL):
    try:
        msg = await message.reply(text)
    except Exception:
        return
    await asyncio.sleep(ttl)
    try:
        await msg.delete()
    except Exception:
        pass


async def _safe_delete(bot: Bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def _send(message: Message, text: str, **kwargs):
    """Отправить новое сообщение в тот же чат — без reply.
    Используется ВМЕСТО message.reply() когда исходное сообщение
    может быть удалено (FSM flow, callback delete+show)."""
    return await message.bot.send_message(message.chat.id, text, **kwargs)


async def _ensure_owner(message: Message) -> Optional[dict]:
    user = message.from_user
    if not user:
        return None
    owner = crm_storage.find_crm_owner_by_tg(user.id)
    if owner:
        updates = {}
        if owner.get("username") != (user.username or ""):
            updates["username"] = user.username or ""
        if owner.get("name") != (user.full_name or ""):
            updates["name"] = user.full_name or ""
        if updates:
            await crm_storage.update_crm_owner(owner["owner_id"], **updates)
        return owner
    owner_id = await crm_storage.add_crm_owner(
        tg_user_id=user.id,
        username=user.username or "",
        name=user.full_name or "",
    )
    logger.info("CRM: new owner %s @%s", owner_id, user.username)
    return crm_storage.get_crm_owner(owner_id)


def _drop_status_emoji(status: str) -> str:
    return {
        "draft":    "📝", "pending":  "⏳", "accepted": "✅",
        "done":     "🏁", "brak":     "❌",
    }.get(status, "•")


def _drop_status_text(status: str) -> str:
    return {
        "draft":    "черновик",
        "pending":  "ожидает обработки",
        "accepted": "в работе",
        "done":     "отработан",
        "brak":     "брак",
    }.get(status, status)


# ════════════════════════════════════════════════════════════════
# Bot + Router
# ════════════════════════════════════════════════════════════════

router = Router(name="crm_main")


# ─── /start ─────────────────────────────────────────────────────

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start_private(message: Message):
    # 🔒 Обязательное условие: пользователь должен быть в экосистеме PRIDE
    # (рабочая беседа с ассистентом ИЛИ /start у main-бота).
    if not await _require_pride(message):
        return
    owner = await _ensure_owner(message)
    if not owner:
        return
    await _show_profile(message, owner, in_group=False)


@router.message(CommandStart())
async def cmd_start_group(message: Message):
    await _ensure_owner(message)
    await message.reply(
        "👋 <b>PRIDE CRM</b>\n\n"
        "В этой группе вы можете управлять клиентами:\n"
        "• <code>/clients</code> — список ваших клиентов\n"
        "• <code>/profile</code> — ваш профиль\n"
        "• <code>/help</code> — справка\n\n"
        "Группа должна быть закреплена за партнёром через "
        "<code>/crm_register_chat @username</code> (только владелец)."
    )


# ─── /profile ───────────────────────────────────────────────────

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    # В ЛС — проверка PRIDE-регистрации. В группе — проверка только если
    # группа не зарегистрирована (но это уже отдельная логика).
    if message.chat.type == "private":
        if not await _require_pride(message):
            return
    owner = await _ensure_owner(message)
    if not owner:
        return
    in_group = message.chat.type != "private"
    await _show_profile(message, owner, in_group=in_group)


async def _show_profile(message: Message, owner: dict, in_group: bool = False):
    joined = time.strftime("%d.%m.%Y", time.localtime(owner.get("joined_at") or 0))
    drops_total = int(owner.get("total_drops") or 0)
    revenue = float(owner.get("total_revenue_usd") or 0)
    rating = float(owner.get("rating") or 5.0)
    warnings = int(owner.get("warnings") or 0)
    banned_until = float(owner.get("banned_until") or 0)
    drops = crm_storage.list_crm_drops(owner_id=owner["owner_id"])
    drops_active = sum(1 for d in drops.values() if d.get("status") in ("pending", "accepted"))
    drops_done = sum(1 for d in drops.values() if d.get("status") == "done")
    drops_pending = sum(1 for d in drops.values() if d.get("status") in ("draft", "pending"))
    drops_brak = sum(1 for d in drops.values() if d.get("status") == "brak")

    # Расчёт средней цены и среднего времени до done
    done_drops = [d for d in drops.values() if d.get("status") == "done"]
    avg_price = (sum(int(d.get("price_usdt") or 0) for d in done_drops) / len(done_drops)) if done_drops else 0
    completion_rate = (drops_done / max(1, drops_total)) * 100 if drops_total else 0

    # Бейджи статуса
    status_line = ""
    if banned_until > time.time():
        until_str = time.strftime("%d.%m %H:%M", time.localtime(banned_until))
        status_line = f"🚫 <b>ЗАБЛОКИРОВАН</b> до {until_str}\n\n"
    elif warnings > 0:
        status_line = f"⚠️ Предупреждений: <b>{warnings}</b>\n\n"

    text = (
        f"👤 <b>Профиль партнёра</b>\n\n"
        f"{status_line}"
        f"<b>Имя:</b> {owner.get('name') or '—'}\n"
        f"<b>Username:</b> @{owner.get('username') or '—'}\n"
        f"<b>ID:</b> <code>{owner['owner_id']}</code>\n"
        f"<b>С нами с:</b> {joined}\n\n"
        f"<b>📊 Статистика:</b>\n"
        f"• Всего клиентов: <b>{drops_total}</b>\n"
        f"• В ожидании: <b>{drops_pending}</b>\n"
        f"• В работе: <b>{drops_active}</b>\n"
        f"• Отработано: <b>{drops_done}</b>\n"
        f"• Брак: <b>{drops_brak}</b>\n"
        f"• % успеха: <b>{completion_rate:.0f}%</b>\n"
        f"• Средняя цена: <b>${avg_price:.0f}</b>\n"
        f"• Рейтинг: <b>{rating:.1f}/5.0</b> ⭐\n"
    )
    kb = [
        [InlineKeyboardButton(text="📇 Мои клиенты", callback_data="drops")],
        [InlineKeyboardButton(text="➕ Новый клиент", callback_data="newdrop")],
        [InlineKeyboardButton(text="❓ Помощь / FAQ", callback_data="help")],
    ]
    if in_group:
        kb.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")])
    await _send(message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "help")
async def cb_help_inline(call: CallbackQuery):
    """Inline-кнопка «Помощь» из профиля."""
    await call.answer()
    text = (
        "❓ <b>FAQ для партнёров</b>\n\n"
        "<b>1) Как добавить нового клиента?</b>\n"
        "/clients → «➕ Добавить клиента» → введи ФИО → выбери банк → данные.\n\n"
        "<b>2) Когда оплата?</b>\n"
        "По умолчанию — гарант ПОСЛЕ отработки. Хотите USDT TRC20 или деньги вперёд — "
        "напишите ассистенту в work-чате.\n\n"
        "<b>3) Что значит «ожидает приёмки»?</b>\n"
        "Вы заполнили анкету и нажали «Отдать в работу» — ждём пока SIMBA подтвердит.\n\n"
        "<b>4) Где статус ЛК?</b>\n"
        "В /clients → ЛК показывают статус: ✏️draft → ⏳pending → ✅accepted → 🏁done.\n\n"
        "<b>5) Что-то не работает?</b>\n"
        "Пиши в личку SIMBA или жми «📞 Связаться с админом» ниже."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Связаться с админом", url="https://t.me/SIMBA")],
        [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_profile")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "back_profile")
async def cb_back_profile(call: CallbackQuery):
    """Назад к профилю."""
    await call.answer()
    owner = crm_storage.find_crm_owner_by_tg(call.from_user.id)
    if not owner:
        await call.message.answer("Профиль не найден.")
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_profile(call.message, owner, in_group=False)


# ─── /help ──────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    base = (
        "<b>📋 Команды CRM:</b>\n\n"
        "• <code>/clients</code> — мои клиенты\n"
        "• <code>/profile</code> — мой профиль\n"
        "• <code>/help</code> — эта справка\n"
    )
    if is_owner(message.from_user.id):
        base += (
            "\n<b>🛡 Команды владельца:</b>\n"
            "• <code>/crm_register_chat @username</code> — закрепить группу\n"
            "• <code>/crm_set_admin</code> — admin-чат CRM (новые дропы)\n"
            "• <code>/crm_set_password</code> — password-чат (RDP/пароли)\n"
            "• <code>/crm_unregister</code> — снять закрепление\n"
            "• <code>/crm_info</code> — статус CRM\n"
        )
    await message.reply(base)


# ─── /clients ───────────────────────────────────────────────────

@router.message(Command("clients"))
async def cmd_clients(message: Message):
    if message.chat.type == "private":
        if not await _require_pride(message):
            return
    owner = await _ensure_owner(message)
    if not owner:
        return
    if message.chat.type != "private":
        await _safe_delete(message.bot, message.chat.id, message.message_id)
        chat_info = crm_storage.get_crm_chat(message.chat.id)
        if not chat_info:
            await ephemeral(
                message,
                "❌ Эта группа не закреплена за партнёром.\n"
                "Владелец должен использовать /crm_register_chat",
            )
            return
        if chat_info.get("is_admin") or chat_info.get("is_password"):
            await ephemeral(message, "ℹ Это служебный чат.")
            return
        owner = crm_storage.get_crm_owner(chat_info["owner_id"])
        if not owner:
            await ephemeral(message, "❌ Партнёр группы не найден.")
            return
    await _show_clients(message, owner)


async def _show_clients(message: Message, owner: dict, edit_msg_id: Optional[int] = None):
    drops = crm_storage.list_crm_drops(owner_id=owner["owner_id"])
    order = {"accepted": 0, "pending": 1, "draft": 2, "done": 3, "brak": 4}
    sorted_drops = sorted(
        drops.values(),
        key=lambda d: (order.get(d.get("status"), 99), -float(d.get("created_at") or 0)),
    )

    kb_rows = []
    for d in sorted_drops:
        emoji = _drop_status_emoji(d.get("status", "draft"))
        label = f"{emoji} {d.get('fio', '—')[:40]}"
        kb_rows.append([InlineKeyboardButton(text=label, callback_data=f"drop:{d['drop_id']}")])
    if not kb_rows:
        kb_rows.append([InlineKeyboardButton(text="⚠️ Клиентов пока нет", callback_data="noop")])
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить клиента", callback_data=f"newdrop:{owner['owner_id']}")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Профиль", callback_data="profile")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")])

    label = (owner.get("username") and ("@" + owner["username"])) or owner.get("name", "")
    text = f"📇 <b>Клиенты {label}:</b>"
    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if edit_msg_id:
        try:
            await message.bot.edit_message_text(
                text, chat_id=message.chat.id,
                message_id=edit_msg_id, reply_markup=markup,
            )
            return
        except TelegramBadRequest:
            pass
    await _send(message, text, reply_markup=markup)


# ─── Callbacks: cancel / noop / drops / profile ─────────────────

@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    owner = crm_storage.find_crm_owner_by_tg(call.from_user.id)
    if not owner:
        await call.answer("Сначала /start")
        return
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    in_group = call.message.chat.type != "private"
    await _show_profile(call.message, owner, in_group=in_group)


@router.callback_query(F.data == "drops")
async def cb_drops(call: CallbackQuery):
    owner = crm_storage.find_crm_owner_by_tg(call.from_user.id)
    if call.message.chat.type != "private":
        chat_info = crm_storage.get_crm_chat(call.message.chat.id)
        if chat_info:
            owner = crm_storage.get_crm_owner(chat_info["owner_id"])
    if not owner:
        await call.answer("Сначала /start", show_alert=True)
        return
    await call.answer()
    await _show_clients(call.message, owner, edit_msg_id=call.message.message_id)


# ─── Добавление дропа: FSM flow ────────────────────────────────

@router.callback_query(F.data.startswith("newdrop:"))
async def cb_newdrop(call: CallbackQuery, state: FSMContext):
    owner_id = call.data.split(":", 1)[1]
    owner = crm_storage.get_crm_owner(owner_id)
    if not owner:
        await call.answer("Партнёр не найден", show_alert=True)
        return
    if int(call.from_user.id) != int(owner.get("tg_user_id") or 0):
        chat_info = crm_storage.get_crm_chat(call.message.chat.id)
        if not chat_info or chat_info.get("owner_id") != owner_id:
            await call.answer("Не ваш партнёр", show_alert=True)
            return
    await call.answer()
    await state.set_state(DropForm.waiting_fio)
    await state.update_data(
        owner_id=owner_id,
        work_chat_id=call.message.chat.id if call.message.chat.type != "private" else None,
        menu_msg_id=call.message.message_id,
    )
    try:
        await call.message.edit_text(
            "<b>➕ Добавление клиента</b>\n\n"
            "Введите <b>ФИО</b> клиента полностью.\n\n"
            "<i>⚠ Бот реагирует на ваше следующее сообщение.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data="drops"),
            ]]),
        )
    except TelegramBadRequest:
        await call.message.reply("Введите ФИО клиента:")


@router.message(DropForm.waiting_fio, F.text & ~F.text.startswith("/"))
async def handle_fio(message: Message, state: FSMContext):
    data = await state.get_data()
    fio = (message.text or "").strip()
    if len(fio) < 5 or len(fio) > 100:
        await ephemeral(message, "❌ ФИО слишком короткое или длинное (5-100 символов)")
        return
    owner_id = data.get("owner_id")
    if not owner_id:
        await message.reply("❌ Сессия истекла, начни заново через /clients")
        await state.clear()
        return
    drop_id = await crm_storage.add_crm_drop(
        owner_id=owner_id, fio=fio,
        work_chat_id=data.get("work_chat_id"),
    )
    drop = crm_storage.get_crm_drop(drop_id)
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("menu_msg_id"):
        await _safe_delete(message.bot, message.chat.id, data["menu_msg_id"])
    await _show_drop(message, drop)
    await state.clear()
    # SSE
    _emit_crm_event("drop.created", {
        "drop_id": drop_id, "fio": fio, "owner_id": owner_id,
    })
    # Кросс-нотификация: если партнёр в ЛС бота — пишем в его work-чат с ассистентом
    if message.chat.type == "private":
        owner = crm_storage.get_crm_owner(owner_id)
        await _notify_work_chat(
            message.bot, owner,
            f"➕ <b>Новый клиент в CRM:</b> {fio}\n"
            f"<i>(добавлен через ЛС CRM-бота)</i>",
        )


# ─── Карточка дропа ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("drop:"))
async def cb_drop(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_drop(call.message, drop)


def _check_drop_complete(drop: dict) -> dict:
    """Возвращает dict с булевыми флагами заполненности всех обязательных полей.
    Используется для чек-листа в карточке + для проверки готовности к dropsend."""
    lks = crm_storage.list_crm_drop_lks(drop_id=drop.get("drop_id", ""))
    return {
        "fio": bool(drop.get("fio")),
        "social": bool((drop.get("social") or "").strip()),
        "residence": bool((drop.get("residence") or "").strip()),
        "other_banks": bool((drop.get("other_banks") or "").strip()),
        "scan": bool(drop.get("scan_file_ids")),
        "lks": len(lks) > 0,
    }


def _drop_is_ready_to_send(drop: dict) -> bool:
    """True если можно отдать в работу — все 6 пунктов заполнены."""
    if drop.get("status") != "draft":
        return False
    return all(_check_drop_complete(drop).values())


async def _show_drop(message: Message, drop: dict):
    lks = crm_storage.list_crm_drop_lks(drop_id=drop["drop_id"])
    status_emoji = _drop_status_emoji(drop.get("status", "draft"))
    status_text = _drop_status_text(drop.get("status", "draft"))
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    owner_label = (
        owner and (owner.get("username") and f"@{owner['username']}" or owner.get("name"))
    ) or "—"
    check = _check_drop_complete(drop)

    def ck(v):
        return "✅" if v else "❌"

    lines = [
        f"{status_emoji} <b>Клиент {drop.get('fio') or '—'}</b>",
        f"<i>статус: {status_text}</i>",
        "",
        f"<b>Партнёр:</b> {owner_label}",
        f"<b>ID:</b> <code>{drop['drop_id']}</code>",
        "",
        "<b>ПРОГРЕСС:</b>",
        f"  {ck(check['fio'])} ФИО",
        f"  {ck(check['social'])} Соц. сеть"
        + (f": <code>{(drop.get('social') or '')[:40]}</code>" if check['social'] else ""),
        f"  {ck(check['residence'])} Место жительства"
        + (f": <code>{(drop.get('residence') or '')[:40]}</code>" if check['residence'] else ""),
        f"  {ck(check['other_banks'])} Доп. банки"
        + (f": <code>{(drop.get('other_banks') or '')[:40]}</code>" if check['other_banks'] else ""),
        f"  {ck(check['scan'])} Документы"
        + (f" ({len(drop.get('scan_file_ids') or [])} фото)" if check['scan'] else ""),
        f"  {ck(check['lks'])} ЛК банков"
        + (f" ({len(lks)})" if check['lks'] else ""),
    ]

    kb_rows = []
    # «Отдать в работу» — наверху, только если всё заполнено
    if _drop_is_ready_to_send(drop):
        kb_rows.append([InlineKeyboardButton(
            text="🚀 Отдать в работу", callback_data=f"dropsend:{drop['drop_id']}",
        )])

    # Кнопки заполнения каждого пункта анкеты
    kb_rows.append([InlineKeyboardButton(
        text=f"{ck(check['social'])} Соц. сеть",
        callback_data=f"dropsocial:{drop['drop_id']}",
    )])
    kb_rows.append([InlineKeyboardButton(
        text=f"{ck(check['residence'])} Место жительства",
        callback_data=f"dropresidence:{drop['drop_id']}",
    )])
    kb_rows.append([InlineKeyboardButton(
        text=f"{ck(check['other_banks'])} Доп. банки",
        callback_data=f"dropotherbanks:{drop['drop_id']}",
    )])
    kb_rows.append([InlineKeyboardButton(
        text=f"{ck(check['scan'])} Документы"
             + (" (изменить)" if check['scan'] else ""),
        callback_data=f"dropdoc:{drop['drop_id']}",
    )])
    if check["scan"]:
        kb_rows.append([InlineKeyboardButton(
            text="👁 Посмотреть доки", callback_data=f"showdoc:{drop['drop_id']}",
        )])
    kb_rows.append([InlineKeyboardButton(
        text=f"{ck(check['lks'])} ЛК банков",
        callback_data=f"droplk:{drop['drop_id']}",
    )])

    # Кнопка изменения ФИО (если ещё в draft/brak)
    if drop.get("status") in ("draft", "brak"):
        kb_rows.append([InlineKeyboardButton(
            text="✏️ Изменить ФИО",
            callback_data=f"dropeditfio:{drop['drop_id']}",
        )])
        kb_rows.append([InlineKeyboardButton(
            text="🗑 Удалить", callback_data=f"dropdelete:{drop['drop_id']}",
        )])
    kb_rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="drops")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")])

    await _send(
        message, "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


# ─── Удаление дропа ────────────────────────────────────────────

@router.callback_query(F.data.startswith("dropdelete:"))
async def cb_dropdelete(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    if drop.get("status") not in ("draft", "brak"):
        await call.answer("Можно удалить только черновик/брак", show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(
        f"⚠ Удалить клиента <b>{drop.get('fio')}</b>?\n\n"
        f"Все его ЛК тоже будут удалены. Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"dropdeleteyes:{drop_id}"),
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"drop:{drop_id}"),
            ],
        ]),
    )


@router.callback_query(F.data.startswith("dropdeleteyes:"))
async def cb_dropdelete_confirmed(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Уже удалён", show_alert=True)
        return
    owner_id = drop.get("owner_id")
    await crm_storage.delete_crm_drop(drop_id)
    await call.answer("Удалено")
    owner = crm_storage.get_crm_owner(owner_id) if owner_id else None
    try:
        await call.message.delete()
    except Exception:
        pass
    if owner:
        await _show_clients(call.message, owner)


# ════════════════════════════════════════════════════════════════
# ЭТАП 2 — Анкета + документы
# ════════════════════════════════════════════════════════════════

# ─── Соц. сеть / Место жительства / Доп. банки — отдельные FSM ───

async def _start_anketa_field(
    call: CallbackQuery, state: FSMContext,
    drop_id: str, fsm_state: State, title: str, prompt: str, field: str,
):
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(fsm_state)
    await state.update_data(
        drop_id=drop_id, field=field,
        menu_msg_id=call.message.message_id,
    )
    cur_val = drop.get(field) or ""
    cur_text = f"\n<b>Текущее значение:</b>\n<code>{cur_val[:200]}</code>\n" if cur_val else ""
    try:
        await call.message.edit_text(
            f"<b>{title} — {drop.get('fio')}</b>\n\n"
            f"{prompt}{cur_text}\n"
            f"<i>⚠ Бот реагирует на ваше следующее сообщение.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"drop:{drop_id}"),
            ]]),
        )
    except TelegramBadRequest:
        pass


async def _save_anketa_field(message: Message, state: FSMContext, min_len: int = 2):
    data = await state.get_data()
    drop_id = data.get("drop_id")
    field = data.get("field")
    if not drop_id or not field:
        await state.clear()
        return
    value = (message.text or "").strip()
    if len(value) < min_len:
        await ephemeral(message, f"❌ Слишком коротко (минимум {min_len} символа)")
        return
    await crm_storage.update_crm_drop(drop_id, **{field: value})
    drop = crm_storage.get_crm_drop(drop_id)
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("menu_msg_id"):
        await _safe_delete(message.bot, message.chat.id, data["menu_msg_id"])
    await state.clear()
    await _show_drop(message, drop)


@router.callback_query(F.data.startswith("dropsocial:"))
async def cb_dropsocial(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    await _start_anketa_field(
        call, state, drop_id,
        DropForm.waiting_social,
        title="🌐 Соц. сеть",
        prompt="Введите ссылки на соц. сети клиента (VK / Instagram / Telegram / Facebook). Можно несколько строк:",
        field="social",
    )


@router.message(DropForm.waiting_social, F.text & ~F.text.startswith("/"))
async def handle_social(message: Message, state: FSMContext):
    await _save_anketa_field(message, state, min_len=3)


@router.callback_query(F.data.startswith("dropresidence:"))
async def cb_dropresidence(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    await _start_anketa_field(
        call, state, drop_id,
        DropForm.waiting_residence,
        title="🏠 Место жительства",
        prompt="Введите город / адрес проживания клиента:",
        field="residence",
    )


@router.message(DropForm.waiting_residence, F.text & ~F.text.startswith("/"))
async def handle_residence(message: Message, state: FSMContext):
    await _save_anketa_field(message, state, min_len=3)


@router.callback_query(F.data.startswith("dropotherbanks:"))
async def cb_dropotherbanks(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    await _start_anketa_field(
        call, state, drop_id,
        DropForm.waiting_other_banks,
        title="🏦 Доп. банки клиента",
        prompt=(
            "Укажите ВСЕ банки где у клиента есть ИП/счета "
            "(даже если мы пока не берём в работу).\n"
            "Это важно для оценки клиента. Пример:\n"
            "<code>Сбер — есть, ВТБ — был закрыт, Газпром — открыт 2 мес назад</code>"
        ),
        field="other_banks",
    )


@router.message(DropForm.waiting_other_banks, F.text & ~F.text.startswith("/"))
async def handle_other_banks(message: Message, state: FSMContext):
    await _save_anketa_field(message, state, min_len=3)


# Legacy dropanketa — теперь редирект на checklist (отдельных полей нет)
@router.callback_query(F.data.startswith("dropanketa:"))
async def cb_dropanketa(call: CallbackQuery, state: FSMContext):
    """Legacy анкета — теперь предлагает кнопки на под-поля."""
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_drop(call.message, drop)


@router.callback_query(F.data.startswith("dropdoc:"))
async def cb_dropdoc(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(DropForm.waiting_scan)
    await state.update_data(drop_id=drop_id, files=[], menu_msg_id=call.message.message_id)
    try:
        await call.message.edit_text(
            f"<b>📎 Документы клиента {drop.get('fio')}</b>\n\n"
            f"Отправьте фото документов (можно несколько — будут добавлены подряд).\n\n"
            f"Когда закончите — нажмите <b>«Готово»</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Готово", callback_data=f"dropdoc_done:{drop_id}")],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"drop:{drop_id}")],
            ]),
        )
    except TelegramBadRequest:
        pass


@router.message(DropForm.waiting_scan, F.photo)
async def handle_scan_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    files = list(data.get("files") or [])
    # Берём самое крупное фото
    largest = message.photo[-1]
    files.append(largest.file_id)
    await state.update_data(files=files)
    # ack
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    # update menu text with count
    drop_id = data.get("drop_id")
    menu_msg_id = data.get("menu_msg_id")
    if drop_id and menu_msg_id:
        try:
            await message.bot.edit_message_text(
                f"<b>📎 Документы клиента</b>\n\n"
                f"Загружено фото: <b>{len(files)}</b>\n\n"
                f"Можешь добавить ещё или нажми «Готово».",
                chat_id=message.chat.id,
                message_id=menu_msg_id,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Готово", callback_data=f"dropdoc_done:{drop_id}")],
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"drop:{drop_id}")],
                ]),
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("dropdoc_done:"))
async def cb_dropdoc_done(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    data = await state.get_data()
    files = list(data.get("files") or [])
    if not files:
        await call.answer("Сначала прикрепи хотя бы одно фото", show_alert=True)
        return
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        await state.clear()
        return
    await crm_storage.update_crm_drop(drop_id, scan_file_ids=files)
    drop = crm_storage.get_crm_drop(drop_id)
    await call.answer(f"✅ Сохранено {len(files)} фото")
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.clear()
    await _show_drop(call.message, drop)


@router.callback_query(F.data.startswith("showdoc:"))
async def cb_showdoc(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop or not drop.get("scan_file_ids"):
        await call.answer("Документов нет", show_alert=True)
        return
    await call.answer()
    files = drop["scan_file_ids"]
    try:
        if len(files) == 1:
            await call.message.bot.send_photo(call.message.chat.id, files[0])
        else:
            from aiogram.types import InputMediaPhoto
            media = [InputMediaPhoto(media=fid) for fid in files[:10]]
            await call.message.bot.send_media_group(call.message.chat.id, media)
    except Exception as e:
        logger.warning("showdoc failed: %s", e)
        await ephemeral(call.message, f"❌ Не удалось показать: {e}")


# ════════════════════════════════════════════════════════════════
# ЭТАП 3 — ЛК банков
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("droplk:"))
async def cb_droplk(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    await _show_drop_lks(call.message, drop)


async def _show_drop_lks(message: Message, drop: dict):
    lks = crm_storage.list_crm_drop_lks(drop_id=drop["drop_id"])

    lines = [f"🏦 <b>ЛК банков клиента {drop.get('fio')}</b>", ""]
    if not lks:
        lines.append("<i>ЛК пока нет. Добавь первый банк.</i>")
    else:
        for lk in lks.values():
            status_e = {"new": "🆕", "pending": "⏳", "ready": "✅", "done": "🏁"}.get(lk.get("status"), "•")
            lines.append(
                f"{status_e} <b>{lk.get('bank')}</b>\n"
                f"   <code>{(lk.get('value') or '—')[:80]}</code>"
            )

    kb_rows = []
    for lk in lks.values():
        kb_rows.append([InlineKeyboardButton(
            text=f"🏦 {lk.get('bank')}",
            callback_data=f"lkview:{lk['droplk_id']}",
        )])
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить банк", callback_data=f"banklk:{drop['drop_id']}")])
    kb_rows.append([InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"drop:{drop['drop_id']}")])

    try:
        await message.delete()
    except Exception:
        pass
    await _send(message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("banklk:"))
async def cb_banklk(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    # Берём список банков из нашего pricing
    pricing = crm_storage.state.get("pricing") or {}
    banks = sorted(pricing.keys())
    if not banks:
        # Дефолтный список
        banks = ["АЛЬФА", "ОЗОН", "РАЙФ", "ТОЧКА", "УРАЛСИБ", "ВТБ", "ЛОКО", "БКС", "ДЕЛО", "УБРИР"]
    kb_rows = []
    row = []
    for i, b in enumerate(banks, 1):
        row.append(InlineKeyboardButton(text=b, callback_data=f"newlk:{drop_id}:{b}"))
        if i % 2 == 0:
            kb_rows.append(row); row = []
    if row:
        kb_rows.append(row)
    kb_rows.append([InlineKeyboardButton(text="◀️ Отмена", callback_data=f"droplk:{drop_id}")])
    try:
        await call.message.edit_text(
            f"<b>🏦 Выбери банк для {drop.get('fio')}:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("newlk:"))
async def cb_newlk(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":", 2)
    if len(parts) < 3:
        await call.answer("Ошибка данных", show_alert=True)
        return
    drop_id, bank = parts[1], parts[2]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(LKForm.waiting_value)
    await state.update_data(drop_id=drop_id, bank=bank, menu_msg_id=call.message.message_id)
    try:
        await call.message.edit_text(
            f"<b>🏦 Новый ЛК — {bank}</b>\n\n"
            f"Введите данные ЛК (логин/пароль, ссылку, что есть):\n\n"
            f"<i>Можно несколькими строками.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"droplk:{drop_id}"),
            ]]),
        )
    except TelegramBadRequest:
        pass


@router.message(LKForm.waiting_value, F.text & ~F.text.startswith("/"))
async def handle_lk_value(message: Message, state: FSMContext):
    data = await state.get_data()
    drop_id = data.get("drop_id")
    bank = data.get("bank")
    value = (message.text or "").strip()
    drop = crm_storage.get_crm_drop(drop_id) if drop_id else None
    if not drop:
        await message.reply("❌ Сессия истекла")
        await state.clear()
        return
    droplk_id = await crm_storage.add_crm_drop_lk(
        drop_id=drop_id, owner_id=drop["owner_id"],
        bank=bank, value=value,
    )
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("menu_msg_id"):
        await _safe_delete(message.bot, message.chat.id, data["menu_msg_id"])
    await state.clear()
    drop = crm_storage.get_crm_drop(drop_id)
    await _send(
        message,
        f"✅ ЛК <b>{bank}</b> сохранён.",
    )
    await _show_drop_lks(message, drop)
    # SSE
    _emit_crm_event("lk.added", {
        "droplk_id": droplk_id, "drop_id": drop_id, "bank": bank,
    })
    # Кросс-нотификация
    if message.chat.type == "private":
        owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
        await _notify_work_chat(
            message.bot, owner,
            f"🏦 <b>Новый ЛК {bank}</b> у клиента <b>{drop.get('fio')}</b>\n"
            f"<i>(добавлен через ЛС CRM-бота)</i>",
        )


@router.callback_query(F.data.startswith("lkview:"))
async def cb_lkview(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    drop = crm_storage.get_crm_drop(lk.get("drop_id"))
    await call.answer()
    status_e = {"new": "🆕", "pending": "⏳", "ready": "✅", "done": "🏁"}.get(lk.get("status"), "•")
    text = (
        f"{status_e} <b>{lk.get('bank')}</b>\n"
        f"клиент: <b>{drop and drop.get('fio') or '—'}</b>\n\n"
        f"<b>Данные ЛК:</b>\n<code>{lk.get('value') or '—'}</code>\n"
    )
    # Сделка показывается только если она реально привязана (auto-set от AI)
    if lk.get("deal"):
        text += f"\n<b>Сделка:</b> #{lk.get('deal')}\n"
    # 🔒 БЕЗОПАСНОСТЬ: new_password / new_mail / ded_ip / ded_pass и т.п. —
    # это данные операционистов и сервера. Они НЕ должны попадать в чаты партнёров
    # (включая ЛС CRM-бота и work-чаты). Видны только в группе «PRIDE | Пароли».
    if lk.get("new_password") or lk.get("ded_ip"):
        text += "\n<i>🔒 Данные перевязки заполнены операционистами.</i>\n"
    # SMS history также — операционные данные, скрываем от партнёра
    kb = [
        [InlineKeyboardButton(text="✏️ Изменить данные", callback_data=f"lkeditvalue:{droplk_id}")],
        [InlineKeyboardButton(text="🗑 Удалить ЛК", callback_data=f"lkdelete:{droplk_id}")],
        [InlineKeyboardButton(text="◀️ К списку ЛК", callback_data=f"droplk:{lk.get('drop_id')}")],
    ]
    try:
        await call.message.delete()
    except Exception:
        pass
    await _send(call.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("lkdelete:"))
async def cb_lkdelete(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("Уже удалён", show_alert=True)
        return
    drop_id = lk.get("drop_id")
    await crm_storage.delete_crm_drop_lk(droplk_id)
    await call.answer("Удалено")
    drop = crm_storage.get_crm_drop(drop_id) if drop_id else None
    if drop:
        await _show_drop_lks(call.message, drop)


# ════════════════════════════════════════════════════════════════
# ЭТАП 4 — Отправка в admin-чат + Принять / Отклонить
# ════════════════════════════════════════════════════════════════

async def _render_admin_text(drop: dict) -> str:
    """Текст контрольного сообщения в admin-чате."""
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    lks = crm_storage.list_crm_drop_lks(drop_id=drop["drop_id"])
    lines = [f"<b>ПОСТАВЩИК:</b> @{owner and owner.get('username') or '—'}\n"]
    lines.append(f"<b>ФИО:</b> {drop.get('fio') or '—'}")
    if drop.get("social"):
        lines.append(f"<b>Соц. сеть:</b> {drop['social']}")
    if drop.get("residence"):
        lines.append(f"<b>Место жительства:</b> {drop['residence']}")
    if drop.get("other_banks"):
        lines.append(f"<b>Доп. банки:</b> {drop['other_banks']}")
    lines.append("")
    for lk in lks.values():
        lines.append(f"<b>Банк:</b> {lk.get('bank')}")
        lines.append(f"<code>{lk.get('value') or '—'}</code>")
        lines.append(f"<b>Сделка #:</b> {lk.get('deal') or '—'}")
    if drop.get("about"):
        lines.append(f"\n<b>Доп. инфо:</b>\n{drop['about']}")
    if drop.get("status") == "accepted":
        lines.append(f"\n<b>ЗАПОЛНЕНИЕ АДМИНАМИ PRIDE:</b>")
        for lk in lks.values():
            if lk.get("link_pass"):
                lines.append(f"  • {lk.get('bank')}: <a href=\"{lk['link_pass']}\">Перейти</a>")
            else:
                lines.append(f"  • {lk.get('bank')}: <i>заполнить</i>")
        lines.append(f"\n<b>Цена:</b> ${drop.get('price_usdt') or 0}")
        lines.append(f"<b>Дата перевяза:</b> {time.strftime('%d.%m.%Y', time.localtime(drop.get('accept_ts') or 0))}")
        lines.append(f"✅ <b>Пролито:</b> {drop.get('prolit_count') or 0}")
        # SMS list
        lines.append("\n<b>📩 SMS-коды:</b>")
        for lk in lks.values():
            sms = lk.get("sms_history") or []
            if not sms:
                lines.append(f"СМС [{lk.get('bank')}]: <i>нет кодов</i>")
            else:
                lines.append(f"СМС [{lk.get('bank')}]:")
                for s in sms[-5:]:
                    lines.append(f"  {s.get('code')} — {s.get('time')}")
    return "\n".join(lines)


def _admin_keyboard(drop: dict) -> InlineKeyboardMarkup:
    """Кнопки для контрольного сообщения в admin-чате."""
    status = drop.get("status")
    if status in ("draft", "pending"):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"acceptdrop:{drop['drop_id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"declinedrop:{drop['drop_id']}"),
        ]])
    if status == "accepted":
        # Кнопки SMS на каждый ЛК + изменить цену
        lks = crm_storage.list_crm_drop_lks(drop_id=drop["drop_id"])
        rows = []
        for lk in lks.values():
            if lk.get("status") == "ready":
                rows.append([InlineKeyboardButton(
                    text=f"[{lk.get('bank')}] Запросить SMS",
                    callback_data=f"takesmscodedrop:{lk['droplk_id']}",
                )])
            else:
                rows.append([InlineKeyboardButton(
                    text=f"[{lk.get('bank')}] Запросить код",
                    callback_data=f"takecodedrop:{lk['droplk_id']}",
                )])
        rows.append([InlineKeyboardButton(
            text="💰 Изменить цену",
            callback_data=f"dropeditprice:{drop['drop_id']}",
        )])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return InlineKeyboardMarkup(inline_keyboard=[])


@router.callback_query(F.data.startswith("dropsend:"))
async def cb_dropsend(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    if drop.get("status") not in ("draft",):
        await call.answer("Уже отправлен или обработан", show_alert=True)
        return
    # Жёсткая проверка чек-листа
    if not _drop_is_ready_to_send(drop):
        check = _check_drop_complete(drop)
        missing = [k for k, v in check.items() if not v]
        await call.answer(
            "❌ Заполни всё: " + ", ".join(missing),
            show_alert=True,
        )
        return
    lks = crm_storage.list_crm_drop_lks(drop_id=drop_id)

    bot = call.message.bot
    # Резолв + авто-коррекция admin-чата
    admin_chat_id = await get_admin_chat_resolved(bot)
    if not admin_chat_id:
        raw = get_admin_chat_id()
        await call.answer("Admin-чат недоступен", show_alert=True)
        await ephemeral(
            call.message,
            f"❌ <b>Бот не имеет доступа к admin-чату</b>\n"
            f"Hardcoded: <code>{raw}</code>\n\n"
            f"<b>Что сделать:</b>\n"
            f"1. Открой группу «PRIDE | ДОСТУПЫ»\n"
            f"2. Добавь CRM-бота как админа\n"
            f"3. В группе пропиши <code>/crm_set_admin</code>",
            ttl=30,
        )
        return
    await call.answer("⏳ Отправляю...")

    # 1) Постим фотки
    try:
        files = drop["scan_file_ids"]
        if len(files) == 1:
            await bot.send_photo(admin_chat_id, files[0])
        else:
            from aiogram.types import InputMediaPhoto
            media = [InputMediaPhoto(media=f) for f in files[:10]]
            await bot.send_media_group(admin_chat_id, media)
    except Exception as e:
        logger.warning("dropsend photos failed: %s", e)

    # 2) Контрольное сообщение
    await crm_storage.update_crm_drop(drop_id, status="pending", send_ts=time.time())
    drop = crm_storage.get_crm_drop(drop_id)
    text = await _render_admin_text(drop)
    try:
        ctrl = await bot.send_message(admin_chat_id, text, reply_markup=_admin_keyboard(drop))
        await crm_storage.update_crm_drop(drop_id, admin_msg_id=ctrl.message_id)
    except Exception as e:
        logger.error("dropsend ctrl msg failed: %s", e)
        # Откат статуса
        await crm_storage.update_crm_drop(drop_id, status="draft")
        await ephemeral(call.message, f"❌ Не удалось отправить в admin-чат: {e}", ttl=15)
        return

    # 3) Апдейтим у партнёра
    try:
        await call.message.delete()
    except Exception:
        pass
    await _send(
        call.message,
        f"🚀 <b>Клиент {drop.get('fio')} отправлен в работу.</b>\n"
        f"<i>Ожидайте решения админов PRIDE.</i>",
    )


@router.callback_query(F.data.startswith("acceptdrop:"))
async def cb_acceptdrop(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    # Защита от двойного клика — атомарная блокировка через in-memory set
    if drop_id in _accepting_now:
        await call.answer("⏳ Уже обрабатываю...", show_alert=False)
        return
    _accepting_now.add(drop_id)
    try:
        drop = crm_storage.get_crm_drop(drop_id)
        if not drop:
            await call.answer("Клиент не найден", show_alert=True)
            return
        if drop.get("status") == "accepted":
            await call.answer("Уже принят", show_alert=True)
            return
        if drop.get("status") not in ("pending", "draft"):
            await call.answer("Нельзя принять в этом статусе", show_alert=True)
            return
        await _cb_acceptdrop_inner(call, drop_id, drop)
    finally:
        _accepting_now.discard(drop_id)


# Локальный set для защиты от двойного нажатия Accept/Decline
_accepting_now: set = set()


async def _cb_acceptdrop_inner(call: CallbackQuery, drop_id: str, drop: dict):
    bot = call.message.bot

    # СНАЧАЛА проверим что бот имеет доступ к password-чату
    # (иначе принимать дроп бессмысленно)
    pwd_chat = await get_password_chat_resolved(bot)
    if not pwd_chat:
        raw_pwd = get_password_chat_id()
        await call.answer("Password-чат недоступен — смотри подробности ниже", show_alert=True)
        await bot.send_message(
            call.message.chat.id,
            f"❌ <b>Не могу запостить в password-чат</b>\n\n"
            f"Hardcoded ID: <code>{raw_pwd}</code>\n"
            f"Пробовал варианты — бот ни в один не пускают.\n\n"
            f"<b>Что сделать:</b>\n"
            f"1. Открой группу «PRIDE | ПАРОЛИ»\n"
            f"2. Убедись что CRM-бот там как админ\n"
            f"3. В группе напиши <code>/crm_set_password</code>\n"
            f"   — бот сохранит правильный chat_id сам.",
        )
        return

    await call.answer("⏳ Принимаю...")
    await crm_storage.update_crm_drop(drop_id, status="accepted", accept_ts=time.time())
    drop = crm_storage.get_crm_drop(drop_id)
    lks = crm_storage.list_crm_drop_lks(drop_id=drop_id)

    # Постим в password-чат — на каждый ЛК отдельное сообщение с кнопкой «Заполнить»
    posted = 0
    errors = []
    for lk in lks.values():
        text2 = _render_password_text(drop, lk)
        try:
            msg = await bot.send_message(
                pwd_chat, text2,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="✏️ Заполнить",
                        callback_data=f"filldrop:{lk['droplk_id']}",
                    ),
                ]]),
            )
            # Сформируем link_pass (t.me/c/<bare_id>/<msg_id>)
            pwd_str = str(pwd_chat).replace("-100", "").lstrip("-")
            link_pass = f"https://t.me/c/{pwd_str}/{msg.message_id}"
            await crm_storage.update_crm_drop_lk(
                lk["droplk_id"],
                msgid_pass=msg.message_id, link_pass=link_pass,
            )
            posted += 1
        except Exception as e:
            logger.warning("acceptdrop password post failed for lk=%s: %s", lk["droplk_id"], e)
            errors.append(f"{lk.get('bank')}: {e}")

    if errors:
        await bot.send_message(
            call.message.chat.id,
            f"⚠️ <b>Часть ЛК не запостилась в пароли:</b>\n"
            + "\n".join(f"  • {e}" for e in errors),
        )
    if posted == 0:
        # Откат
        await crm_storage.update_crm_drop(drop_id, status="pending")
        return

    # ═══════ ЭТАП 7: интеграция с экосистемой PRIDE ═══════
    # 1) Создаём lk_cards в нашем главном storage.lk_cards
    #    (та же таблица что Группа 1 ЛК / userbot / дашборд использует)
    try:
        lk_card_ids = await _create_lk_cards_from_crm_drop(drop)
        if lk_card_ids:
            # Сохраняем связь crm_drop → lk_cards
            await crm_storage.update_crm_drop(drop_id, lk_card_ids=lk_card_ids)
    except Exception as e:
        logger.warning("CRM→lk_cards bridge failed: %s", e)

    # 2) Энкуим команду для userbot → запостит анкеты в Группу 1 ЛК PRIDE
    try:
        await _queue_anketa_post_via_userbot(drop_id)
    except Exception as e:
        logger.warning("queue anketa post failed: %s", e)

    # 3) Уведомляем партнёра в его work-чате с ассистентом.
    #    Это единственное место — сообщение объясняет CRM-handoff и метод оплаты.
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    if owner:
        # Считаем сколько ЛК и какие банки приняты
        lks = list(crm_storage.list_crm_drop_lks(drop_id=drop_id).values())
        banks_line = ", ".join(sorted({(l.get("bank") or "").upper() for l in lks if l.get("bank")}))
        n_lks = len(lks)
        # Цена
        price_line = ""
        try:
            price = int(drop.get("price_usdt") or 0)
            if price:
                price_line = f"\n💵 Цена: <b>${price}</b>"
        except Exception:
            pass

        handoff_text = (
            f"✅ <b>Клиент {drop.get('fio')} принят в работу.</b>\n"
            f"📋 ЛК: <b>{n_lks}</b> ({banks_line or '—'})"
            f"{price_line}\n"
            f"💳 Метод оплаты: <b>уточняется у клиента</b>\n\n"
            f"<i>Карточки уже в Группе 1 ЛК. Если клиент хочет USDT — "
            f"AI уточнит адрес. Если деньги вперёд — переключим на Тимона.</i>"
        )
        try:
            await _notify_work_chat(bot, owner, handoff_text)
        except Exception:
            pass

        # 4) Энкуим pending_perevyaz чтобы ассистент знал — этому клиенту
        # нужно уточнить метод оплаты (страховка если AI пропустит).
        try:
            wc = owner.get("work_chat_id")
            if wc and lks:
                first_bank = (lks[0].get("bank") or "").upper()
                await crm_storage.set_pending_perevyaz(
                    int(wc),
                    bank=first_bank,
                    fio=drop.get("fio") or "",
                )
        except Exception as e:
            logger.debug("set_pending_perevyaz failed: %s", e)

    # Апдейтим контрольное сообщение в admin-чате
    drop = crm_storage.get_crm_drop(drop_id)
    text = await _render_admin_text(drop)
    try:
        await bot.edit_message_text(
            text, chat_id=call.message.chat.id,
            message_id=call.message.message_id, reply_markup=_admin_keyboard(drop),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("acceptdrop admin edit failed: %s", e)

    # 5) Эмитим SSE-событие для веб-дашборда
    try:
        _emit_crm_event("drop.accepted", {
            "drop_id": drop_id,
            "fio": drop.get("fio"),
            "owner_id": drop.get("owner_id"),
            "lk_card_ids": drop.get("lk_card_ids") or [],
        })
    except Exception:
        pass


def _render_password_text(drop: dict, lk: dict) -> str:
    owner = crm_storage.get_crm_owner(drop.get("owner_id", "")) or {}
    return (
        f"🔐 <b>ЛК {lk.get('bank')}</b> · {drop.get('fio') or '—'}\n"
        f"<i>поставщик: @{owner.get('username') or '—'}</i>\n\n"
        f"<b>Новый логин:</b> <code>{lk.get('new_login') or '—'}</code>\n"
        f"<b>Новый пароль:</b> <code>{lk.get('new_password') or '—'}</code>\n"
        f"<b>Новая почта:</b> <code>{lk.get('new_mail') or '—'}</code>\n"
        f"<b>Новый номер:</b> <code>{lk.get('new_number') or '—'}</code>\n"
        f"<b>Кодовое слово:</b> <code>{lk.get('code_word') or '—'}</code>\n\n"
        f"<b>🖥 Дедик:</b>\n"
        f"  Где установлен: <code>{lk.get('ded_location') or '—'}</code>\n"
        f"  IP: <code>{lk.get('ded_ip') or '—'}</code>\n"
        f"  Логин: <code>{lk.get('ded_login') or 'Administrator'}</code>\n"
        f"  Пароль: <code>{lk.get('ded_pass') or '—'}</code>"
    )


def _password_filled_keyboard(droplk_id: str, link_access: str = "") -> InlineKeyboardMarkup:
    rows = []
    if link_access:
        rows.append([InlineKeyboardButton(text="🔗 Перейти", url=link_access)])
    rows.append([
        InlineKeyboardButton(text="+ Пул Инка", callback_data=f"addpool:{droplk_id}_inka"),
        InlineKeyboardButton(text="+ Пул ЮР-ЮР", callback_data=f"addpool:{droplk_id}_urur"),
    ])
    rows.append([InlineKeyboardButton(text="✅ Успешно отработано", callback_data=f"dropdone:{droplk_id}")])
    rows.append([InlineKeyboardButton(text="❌ Сообщить о проблеме", callback_data=f"dropproblem:{droplk_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("declinedrop:"))
async def cb_declinedrop(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    # Защита от двойного клика
    if drop_id in _accepting_now:
        await call.answer("⏳ Уже обрабатываю...", show_alert=False)
        return
    _accepting_now.add(drop_id)
    try:
        drop = crm_storage.get_crm_drop(drop_id)
        if not drop:
            await call.answer("Клиент не найден", show_alert=True)
            return
        if drop.get("status") == "brak":
            await call.answer("Уже отклонён", show_alert=True)
            return
        await crm_storage.update_crm_drop(drop_id, status="brak")
        await call.answer("Отклонено")
    finally:
        _accepting_now.discard(drop_id)
    _emit_crm_event("drop.declined", {
        "drop_id": drop_id, "fio": drop.get("fio"),
        "owner_id": drop.get("owner_id"),
    }, severity="warning")
    # Уведомляем партнёра — клиент отклонён
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    if owner:
        try:
            await _notify_work_chat(
                call.message.bot, owner,
                f"❌ <b>Клиент {drop.get('fio')} отклонён в CRM.</b>\n"
                f"<i>Проверьте данные и подайте заново.</i>",
            )
        except Exception:
            pass
    try:
        await call.message.edit_text(
            f"❌ <b>Отклонено</b>\n\n<b>ПОСТАВЩИК:</b> {(crm_storage.get_crm_owner(drop.get('owner_id')) or {}).get('username')}\n"
            f"<b>ФИО:</b> {drop.get('fio')}",
            reply_markup=None,
        )
    except Exception:
        pass
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    if owner and owner.get("work_chat_id"):
        try:
            await call.message.bot.send_message(
                owner["work_chat_id"],
                f"❌ <b>Клиент {drop.get('fio')} отклонён.</b>",
            )
        except Exception:
            pass


async def _fill_ask_step(message, state, prompt_text: str, next_state):
    """Удалить ответ оператора и предыдущий вопрос бота, спросить новый шаг.
    Используется в fill-flow чтобы НЕ засорять чат паролей (там лежат
    реальные пароли/коды — их видеть в истории нельзя)."""
    data = await state.get_data()
    fill_msgs = list(data.get("fill_msgs") or [])
    # 1) Удалить ответ оператора (с конфиденциальным значением)
    try:
        await message.delete()
    except Exception:
        pass
    # 2) Удалить предыдущий prompt бота
    for mid in fill_msgs:
        try:
            await message.bot.delete_message(message.chat.id, mid)
        except Exception:
            pass
    # 3) Отправить новый prompt и запомнить его msg_id
    sent = await message.bot.send_message(message.chat.id, prompt_text)
    await state.update_data(fill_msgs=[sent.message_id])
    await state.set_state(next_state)


# ════════════════════════════════════════════════════════════════
# ЭТАП 5 — Заполнение admin'ом в password-чате (FSM 5 шагов)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("filldrop:"))
async def cb_filldrop(call: CallbackQuery, state: FSMContext):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(FillForm.waiting_new_login)
    drop = crm_storage.get_crm_drop(lk["drop_id"])
    # Шлём первый вопрос обычным send_message чтобы запомнить его id
    sent = await call.message.bot.send_message(
        call.message.chat.id,
        f"<b>✏️ Заполнение {lk.get('bank')} ({drop.get('fio')})</b>\n\n"
        f"<b>Шаг 1/8:</b> Новый логин (или «-»):"
    )
    await state.update_data(
        droplk_id=droplk_id, fill_data={},
        fill_msgs=[sent.message_id],
    )


@router.message(FillForm.waiting_new_login, F.text & ~F.text.startswith("/"))
async def fill_login(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["new_login"] = (message.text or "").strip()
    await state.update_data(**data)
    await _fill_ask_step(message, state, "<b>Шаг 2/8:</b> Новый пароль (или «-»):", FillForm.waiting_new_password)


@router.message(FillForm.waiting_new_password, F.text & ~F.text.startswith("/"))
async def fill_pass(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["new_password"] = (message.text or "").strip()
    await state.update_data(**data)
    await _fill_ask_step(message, state, "<b>Шаг 3/8:</b> Новая почта (или «-»):", FillForm.waiting_new_mail)


@router.message(FillForm.waiting_new_mail, F.text & ~F.text.startswith("/"))
async def fill_mail(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["new_mail"] = (message.text or "").strip()
    await state.update_data(**data)
    await _fill_ask_step(message, state, "<b>Шаг 4/8:</b> Новый номер (или «-»):", FillForm.waiting_new_number)


@router.message(FillForm.waiting_new_number, F.text & ~F.text.startswith("/"))
async def fill_number(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["new_number"] = (message.text or "").strip()
    await state.update_data(**data)
    await _fill_ask_step(message, state, "<b>Шаг 5/8:</b> Кодовое слово (или «-»):", FillForm.waiting_code_word)


@router.message(FillForm.waiting_code_word, F.text & ~F.text.startswith("/"))
async def fill_code_word(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["code_word"] = (message.text or "").strip()
    await state.update_data(**data)
    await _fill_ask_step(message, state, "<b>Шаг 6/8:</b> Где установлен дедик (город / провайдер / своя машина / VPS):", FillForm.waiting_ded_location)


@router.message(FillForm.waiting_ded_location, F.text & ~F.text.startswith("/"))
async def fill_ded_location(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["ded_location"] = (message.text or "").strip()
    await state.update_data(**data)
    await _fill_ask_step(message, state, "<b>Шаг 7/8:</b> IP дедика:", FillForm.waiting_ded_ip)


@router.message(FillForm.waiting_ded_ip, F.text & ~F.text.startswith("/"))
async def fill_ip(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["ded_ip"] = (message.text or "").strip()
    await state.update_data(**data)
    await _fill_ask_step(message, state, "<b>Шаг 8/8:</b> Пароль дедика:", FillForm.waiting_ded_pass)


@router.message(FillForm.waiting_ded_pass, F.text & ~F.text.startswith("/"))
async def fill_pass2(message: Message, state: FSMContext):
    data = await state.get_data()
    fd = data.setdefault("fill_data", {})
    fd["ded_pass"] = (message.text or "").strip()
    droplk_id = data.get("droplk_id")
    # Удалить ответ оператора (с паролем!) и последний вопрос бота
    try:
        await message.delete()
    except Exception:
        pass
    for mid in (data.get("fill_msgs") or []):
        try:
            await message.bot.delete_message(message.chat.id, mid)
        except Exception:
            pass
    if not droplk_id:
        await state.clear()
        return
    # Сохраняем все 8 полей
    await crm_storage.update_crm_drop_lk(
        droplk_id,
        new_login=fd.get("new_login") or "",
        new_password=fd.get("new_password") or "",
        new_mail=fd.get("new_mail") or "",
        new_number=fd.get("new_number") or "",
        code_word=fd.get("code_word") or "",
        ded_location=fd.get("ded_location") or "",
        ded_ip=fd.get("ded_ip") or "",
        ded_pass=fd.get("ded_pass") or "",
        status="ready",
    )
    await state.clear()

    lk = crm_storage.get_crm_drop_lk(droplk_id)
    drop = crm_storage.get_crm_drop(lk["drop_id"])

    # Обновляем сообщение в password-чате (с новыми кнопками)
    bot = message.bot
    pwd_chat = get_password_chat_id()
    if pwd_chat and lk.get("msgid_pass"):
        try:
            await bot.edit_message_text(
                _render_password_text(drop, lk),
                chat_id=pwd_chat, message_id=lk["msgid_pass"],
                reply_markup=_password_filled_keyboard(droplk_id),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("password edit failed: %s", e)

    # Обновляем контрольное сообщение в admin-чате
    admin_chat = get_admin_chat_id()
    if admin_chat and drop.get("admin_msg_id"):
        try:
            await bot.edit_message_text(
                await _render_admin_text(drop),
                chat_id=admin_chat, message_id=drop["admin_msg_id"],
                reply_markup=_admin_keyboard(drop),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("admin edit (after fill) failed: %s", e)

    await message.reply(f"✅ ЛК <b>{lk.get('bank')}</b> заполнен.")


# Пулы Инка / ЮР-ЮР
@router.callback_query(F.data.startswith("addpool:"))
async def cb_addpool(call: CallbackQuery):
    arg = call.data.split(":", 1)[1]
    parts = arg.split("_", 1)
    if len(parts) != 2:
        await call.answer("Bad data", show_alert=True)
        return
    droplk_id, pool_type = parts
    pool_name = {"inka": "Инка", "urur": "ЮР-ЮР"}.get(pool_type, pool_type)
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await call.answer(f"✅ Добавлено в пул {pool_name}")
    # Простая запись в drop's history
    drop_id = lk.get("drop_id")
    drop = crm_storage.get_crm_drop(drop_id) if drop_id else None
    if drop:
        new_count = int(drop.get("prolit_count") or 0) + 1
        await crm_storage.update_crm_drop(drop_id, prolit_count=new_count)
        # Обновим admin message
        admin_chat = get_admin_chat_id()
        if admin_chat and drop.get("admin_msg_id"):
            try:
                drop = crm_storage.get_crm_drop(drop_id)
                await call.message.bot.edit_message_text(
                    await _render_admin_text(drop),
                    chat_id=admin_chat, message_id=drop["admin_msg_id"],
                    reply_markup=_admin_keyboard(drop),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("dropdone:"))
async def cb_dropdone(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await crm_storage.update_crm_drop_lk(droplk_id, status="done")
    await call.answer("✅ Отмечено")
    # SSE: один ЛК завершён
    _emit_crm_event("lk.done", {
        "droplk_id": droplk_id,
        "bank": lk.get("bank"),
        "drop_id": lk.get("drop_id"),
    })
    # Проверка: все ЛК дропа done → drop.status = done
    drop_id = lk.get("drop_id")
    if drop_id:
        all_lks = crm_storage.list_crm_drop_lks(drop_id=drop_id)
        if all_lks and all(l.get("status") == "done" for l in all_lks.values()):
            await crm_storage.update_crm_drop(drop_id, status="done", done_ts=time.time())
            drop = crm_storage.get_crm_drop(drop_id) or {}
            owner = crm_storage.get_crm_owner(drop.get("owner_id", "")) or {}

            # ─── Уведомление: все ЛК этого дропа закрыты ───
            try:
                banks = sorted({(l.get("bank") or "").upper() for l in all_lks.values()})
                summary_text = (
                    f"🎉 <b>Все ЛК клиента {drop.get('fio') or '—'} отработаны</b>\n"
                    f"Банки: {', '.join(banks) or '—'}\n"
                    f"Всего ЛК: <b>{len(all_lks)}</b>"
                )
                # 1) Партнёру в DM
                if owner.get("tg_user_id"):
                    try:
                        await call.message.bot.send_message(
                            owner["tg_user_id"], summary_text,
                        )
                    except Exception as e:
                        logger.debug("dropdone DM partner failed: %s", e)
                # 2) Партнёру в work_chat
                await _notify_work_chat(call.message.bot, owner, summary_text)
                # 3) В admin-чат CRM
                admin_chat_id = await get_admin_chat_resolved(call.message.bot)
                if admin_chat_id:
                    try:
                        await call.message.bot.send_message(admin_chat_id, summary_text)
                    except Exception as e:
                        logger.debug("dropdone admin notify failed: %s", e)
            except Exception as e:
                logger.warning("all-LKs-done notify failed: %s", e)

            # SSE
            _emit_crm_event("drop.done", {
                "drop_id": drop_id,
                "fio": drop.get("fio"),
                "owner_id": drop.get("owner_id"),
            }, severity="success")


@router.callback_query(F.data.startswith("dropproblem:"))
async def cb_dropproblem(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await call.answer("⚠ Помечено как проблема")
    # Уведомление в admin
    admin_chat = get_admin_chat_id()
    if admin_chat:
        try:
            await call.message.bot.send_message(
                admin_chat,
                f"⚠️ <b>Проблема с ЛК {lk.get('bank')}</b>\n"
                f"droplk_id: <code>{droplk_id}</code>",
            )
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# ЭТАП 6 — SMS-коды (запрос + ввод партнёром)
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# SMS multi-stage flow — управление кодами входа и перевязки.
# 6 стадий, кнопка в admin-чате меняется по мере прохождения.
# ════════════════════════════════════════════════════════════════
# Stages:
#  ""              — старт (показываем «Запросить готовность»)
#  asking_ready    — спросили клиента «готовы дать код?»
#  client_ready    — клиент подтвердил (или менеджер прожал руками)
#  awaiting_login  — попросили login-код, ждём от клиента
#  login_received  — login-код пришёл, ждём подтверждения «успешный вход»
#  login_success   — вход подтверждён, можно спрашивать код перевяза
#  awaiting_perevyaz — попросили перевяз-код
#  perevyaz_received — код пришёл
#  done            — перевязка финализирована

_SMS_STAGE_LABELS = {
    "":                 ("⚪ Старт", "❓ Спросить готовность"),
    "asking_ready":     ("⏳ Спросили готовность", "✅ Клиент готов"),
    "client_ready":     ("✅ Клиент готов", "📩 Дать код входа"),
    "awaiting_login":   ("⏳ Ждём код входа", "✏️ Ввести код входа"),
    "login_received":   ("📩 Код входа получен", "✅ Успешный вход"),
    "login_success":    ("✅ Вход успешен", "📩 Дать код перевяза"),
    "awaiting_perevyaz":("⏳ Ждём код перевяза", "✏️ Ввести код перевяза"),
    "perevyaz_received":("📩 Код перевяза получен", "✅ Перевязка успешна"),
    "done":             ("🏁 Завершено", None),
}


def _sms_flow_text(lk: dict, drop: dict) -> str:
    stage = lk.get("sms_stage") or ""
    label, _ = _SMS_STAGE_LABELS.get(stage, ("?", None))
    bank = lk.get("bank") or "—"
    fio = drop.get("fio") if drop else "—"
    login_code = lk.get("sms_login_code") or ""
    perevyaz_code = lk.get("sms_perevyaz_code") or ""
    lines = [
        f"📩 <b>SMS-Flow {bank}</b> · {fio}",
        f"<b>Стадия:</b> {label}",
    ]
    if login_code:
        lines.append(f"🔑 <b>Код входа:</b> <code>{login_code}</code>")
    if perevyaz_code:
        lines.append(f"🔑 <b>Код перевязки:</b> <code>{perevyaz_code}</code>")
    return "\n".join(lines)


def _sms_flow_keyboard(lk: dict) -> InlineKeyboardMarkup:
    stage = lk.get("sms_stage") or ""
    droplk_id = lk.get("droplk_id")
    _, next_label = _SMS_STAGE_LABELS.get(stage, (None, None))
    rows = []
    if next_label:
        rows.append([InlineKeyboardButton(
            text=next_label, callback_data=f"smsadv:{droplk_id}",
        )])
    # Кнопка «Отмена» если flow ещё в процессе
    if stage and stage != "done":
        rows.append([InlineKeyboardButton(
            text="❌ Сбросить flow", callback_data=f"smsreset:{droplk_id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_to_client_chat(bot, drop, owner, text):
    """Шлёт сообщение в work_chat клиента (там где ассистент)."""
    if not owner:
        return False
    wc = owner.get("work_chat_id")
    if not wc:
        return False
    try:
        await bot.send_message(wc, text)
        return True
    except Exception as e:
        logger.warning("send to client chat failed: %s", e)
        return False


async def _post_or_update_sms_tracker(bot, droplk_id):
    """Создаёт/апдейтит сообщение SMS-трекера в admin-чате CRM."""
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        return None
    drop = crm_storage.get_crm_drop(lk.get("drop_id"))
    admin_chat = await get_admin_chat_resolved(bot)
    if not admin_chat:
        return None
    text = _sms_flow_text(lk, drop)
    kb = _sms_flow_keyboard(lk)
    msg_id = lk.get("sms_tracker_msg_id")
    if msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=admin_chat, message_id=msg_id,
                reply_markup=kb, disable_web_page_preview=True,
            )
            return msg_id
        except Exception:
            pass  # message may be deleted, post new
    try:
        sent = await bot.send_message(admin_chat, text, reply_markup=kb)
        await crm_storage.update_crm_drop_lk(droplk_id, sms_tracker_msg_id=sent.message_id)
        return sent.message_id
    except Exception as e:
        logger.warning("post sms tracker failed: %s", e)
        return None


# LEGACY компат: takecodedrop и takesmscodedrop запускают новый flow
@router.callback_query(F.data.startswith("takecodedrop:"))
async def cb_takecodedrop(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="")
    await _post_or_update_sms_tracker(call.message.bot, droplk_id)
    await call.answer("📩 SMS-flow начат")


@router.callback_query(F.data.startswith("takesmscodedrop:"))
async def cb_takesmscodedrop(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="")
    await _post_or_update_sms_tracker(call.message.bot, droplk_id)
    await call.answer("📩 SMS-flow начат")


@router.callback_query(F.data.startswith("smsadv:"))
async def cb_smsadv(call: CallbackQuery, state: FSMContext):
    """Продвинуть SMS-flow на следующую стадию."""
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    drop = crm_storage.get_crm_drop(lk.get("drop_id"))
    owner = crm_storage.get_crm_owner(drop.get("owner_id", "") if drop else "")
    bot = call.message.bot
    stage = lk.get("sms_stage") or ""
    bank = lk.get("bank") or "—"

    if stage == "":
        # Старт — спрашиваем готовность у клиента
        await _send_to_client_chat(
            bot, drop, owner,
            f"Готовы дать код для входа в ЛК <b>{bank}</b>?",
        )
        await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="asking_ready")
        await call.answer("✅ Спросили клиента")
    elif stage == "asking_ready":
        # Менеджер видит что клиент готов → переход
        await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="client_ready")
        await call.answer("✅ Отмечено: клиент готов")
    elif stage == "client_ready":
        # Просим код входа
        await _send_to_client_chat(
            bot, drop, owner,
            f"Пришлите <b>код входа в ЛК {bank}</b> следующим сообщением.",
        )
        await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="awaiting_login")
        await call.answer("📩 Запросили код входа")
    elif stage == "awaiting_login":
        # Менеджер вводит код вручную
        await state.set_state(SMSForm.waiting_code)
        await state.update_data(droplk_id=droplk_id, sms_kind="login")
        await call.message.reply(
            f"📩 Введите код <b>входа</b> для {bank} следующим сообщением:"
        )
        await call.answer()
    elif stage == "login_received":
        # Подтвердить успешный вход
        await _send_to_client_chat(
            bot, drop, owner,
            f"✅ Вход в ЛК <b>{bank}</b> выполнен успешно.",
        )
        await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="login_success")
        await call.answer("✅ Клиента уведомили")
    elif stage == "login_success":
        # Просим код перевязки
        await _send_to_client_chat(
            bot, drop, owner,
            f"Пришлите <b>код перевязки для ЛК {bank}</b> следующим сообщением.",
        )
        await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="awaiting_perevyaz")
        await call.answer("📩 Запросили код перевяза")
    elif stage == "awaiting_perevyaz":
        await state.set_state(SMSForm.waiting_code)
        await state.update_data(droplk_id=droplk_id, sms_kind="perevyaz")
        await call.message.reply(
            f"📩 Введите код <b>перевязки</b> для {bank} следующим сообщением:"
        )
        await call.answer()
    elif stage == "perevyaz_received":
        # Финал
        await _send_to_client_chat(
            bot, drop, owner,
            f"✅ Перевязка ЛК <b>{bank}</b> успешно выполнена.",
        )
        await crm_storage.update_crm_drop_lk(droplk_id, sms_stage="done")
        await call.answer("🏁 Перевязка завершена")
    else:
        await call.answer()
    await _post_or_update_sms_tracker(bot, droplk_id)


@router.callback_query(F.data.startswith("smsreset:"))
async def cb_smsreset(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await crm_storage.update_crm_drop_lk(
        droplk_id, sms_stage="",
        sms_login_code="", sms_perevyaz_code="",
    )
    await call.answer("🔄 Flow сброшен")
    await _post_or_update_sms_tracker(call.message.bot, droplk_id)


@router.callback_query(F.data.startswith("givemecode:"))
async def cb_givemecode(call: CallbackQuery, state: FSMContext):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(SMSForm.waiting_code)
    await state.update_data(droplk_id=droplk_id)
    try:
        await call.message.edit_text(
            f"<b>📩 Введите код для {lk.get('bank')}:</b>\n\n"
            f"<i>Бот реагирует на следующее сообщение.</i>",
        )
    except TelegramBadRequest:
        pass


@router.message(SMSForm.waiting_code, F.text & ~F.text.startswith("/"))
async def handle_sms_code(message: Message, state: FSMContext):
    data = await state.get_data()
    code = (message.text or "").strip()
    droplk_id = data.get("droplk_id")
    sms_kind = data.get("sms_kind") or "login"
    if not droplk_id:
        await state.clear()
        return
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await state.clear()
        return
    drop = crm_storage.get_crm_drop(lk.get("drop_id"))
    await crm_storage.append_crm_sms(droplk_id, code=code)
    # По типу записываем в соответствующее поле + переключаем stage
    if sms_kind == "perevyaz":
        await crm_storage.update_crm_drop_lk(
            droplk_id,
            sms_perevyaz_code=code,
            sms_stage="perevyaz_received",
        )
    else:
        await crm_storage.update_crm_drop_lk(
            droplk_id,
            sms_login_code=code,
            sms_stage="login_received",
        )
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await _post_or_update_sms_tracker(message.bot, droplk_id)
    # Обновляем сообщение в admin-чате
    admin_chat = get_admin_chat_id()
    if drop and admin_chat and drop.get("admin_msg_id"):
        try:
            drop = crm_storage.get_crm_drop(lk.get("drop_id"))
            await message.bot.edit_message_text(
                await _render_admin_text(drop),
                chat_id=admin_chat, message_id=drop["admin_msg_id"],
                reply_markup=_admin_keyboard(drop),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("sms admin edit failed: %s", e)


# Цена покупки (этап 4)
@router.callback_query(F.data.startswith("dropeditprice:"))
async def cb_dropeditprice(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(PriceForm.waiting_price)
    await state.update_data(drop_id=drop_id)
    await call.message.reply(
        f"<b>💰 Новая цена для {drop.get('fio')}</b>\n\n"
        f"Текущая: ${drop.get('price_usdt') or 0}\n"
        f"Введите новую цену в USD:"
    )


@router.message(PriceForm.waiting_price, F.text & ~F.text.startswith("/"))
async def handle_price(message: Message, state: FSMContext):
    data = await state.get_data()
    drop_id = data.get("drop_id")
    try:
        price = int((message.text or "").replace("$", "").strip())
    except ValueError:
        await ephemeral(message, "❌ Введи число")
        return
    if not drop_id:
        await state.clear()
        return
    drop = crm_storage.get_crm_drop(drop_id)
    old_price = int(drop.get("price_usdt") or 0) if drop else 0
    await crm_storage.update_crm_drop(drop_id, price_usdt=price)
    await state.clear()
    drop = crm_storage.get_crm_drop(drop_id)
    await message.reply(f"✅ Цена обновлена: <b>${price}</b>")
    admin_chat = get_admin_chat_id()
    if admin_chat and drop.get("admin_msg_id"):
        try:
            await message.bot.edit_message_text(
                await _render_admin_text(drop),
                chat_id=admin_chat, message_id=drop["admin_msg_id"],
                reply_markup=_admin_keyboard(drop),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    # ─── Уведомить партнёра о смене цены ───
    try:
        owner = crm_storage.get_crm_owner(drop.get("owner_id", "") if drop else "")
        if owner and price != old_price:
            arrow = "📈" if price > old_price else "📉"
            txt = (
                f"{arrow} <b>Цена изменена</b>\n"
                f"Клиент: {drop.get('fio')}\n"
                f"Было: ${old_price} → Стало: <b>${price}</b>"
            )
            # 1) DM партнёру
            tg_id = owner.get("tg_user_id")
            if tg_id:
                try:
                    await message.bot.send_message(tg_id, txt)
                except Exception as e:
                    logger.debug("price DM failed: %s", e)
            # 2) В work-чат партнёра
            await _notify_work_chat(message.bot, owner, txt)
    except Exception as e:
        logger.warning("price-change notify failed: %s", e)


# ════════════════════════════════════════════════════════════════
# ЭТАП 7 — Интеграция с экосистемой PRIDE
# ════════════════════════════════════════════════════════════════

def _emit_crm_event(event_type: str, payload: dict, severity: str = "info") -> None:
    """Эмит SSE-события для веб-дашборда. Best-effort — ошибки логируются."""
    try:
        from event_bus import emit_event
        emit_event(f"crm.{event_type}", payload=payload, character="crm", severity=severity)
    except Exception as e:
        logger.debug("emit_crm_event failed: %s", e)


async def _create_lk_cards_from_crm_drop(drop: dict) -> list:
    """Создаёт записи в storage.lk_cards для каждого CRM ЛК этого дропа.
    Возвращает list created lk_card_ids.

    Это та же таблица lk_cards что использует userbot/web/группа 1 ЛК PRIDE.
    Связь сохраняется в crm_drops[d].lk_card_ids."""
    lks = crm_storage.list_crm_drop_lks(drop_id=drop["drop_id"])
    if not lks:
        return []
    owner = crm_storage.get_crm_owner(drop.get("owner_id", "")) or {}
    pricing = crm_storage.state.get("pricing") or {}
    created = []
    for lk in lks.values():
        bank = (lk.get("bank") or "").upper()
        price = float(pricing.get(bank, drop.get("price_usdt", 0)) or 0)
        try:
            card_id = await crm_storage.add_lk_card(
                bank=bank,
                fio=drop.get("fio") or "",
                supplier=owner.get("username") or "",
                price_usdt=price,
                # payment_method ПУСТОЙ — AI должен уточнить у клиента и
                # вписать через set_payment_method tool. Без пре-заполнения.
                payment_method="",
                status="В_РАБОТЕ",
                work_chat_id=owner.get("work_chat_id") or 0,
                client_username=owner.get("username") or "",
                created_by="crm_bot",
                deal_id=lk.get("deal") or "",
            )
            created.append(card_id)
            logger.info(
                "CRM→lk_cards: drop=%s crm_lk=%s → lk_card=%s",
                drop.get("drop_id"), lk.get("droplk_id"), card_id,
            )
        except Exception as e:
            logger.warning("create_lk_card failed for crm_lk=%s: %s",
                           lk.get("droplk_id"), e)
    return created


async def _queue_anketa_post_via_userbot(drop_id: str):
    """Энкуит команду в dashboard_commands — userbot подберёт и
    запостит анкету в нашу Группу 1 ЛК PRIDE (через Telethon)."""
    try:
        await crm_storage.enqueue_dashboard_command(
            f"__crm_post_anketa {drop_id}",
            source="crm_bot:acceptdrop",
        )
        logger.info("queued __crm_post_anketa for drop=%s", drop_id)
    except Exception as e:
        logger.warning("queue anketa post failed: %s", e)


async def _notify_work_chat(bot, owner: dict, text: str):
    """Кросс-нотификация: партнёр работал в ЛС бота — в его work-чате
    с ассистентом появляется уведомление о новой активности.
    Ассистент / тимлид видит без необходимости лезть в CRM."""
    if not owner:
        return
    wc = owner.get("work_chat_id")
    if not wc:
        return
    decorated = _decor(text)
    try:
        await bot.send_message(wc, decorated)
    except Exception as e:
        # Fallback без premium-emoji
        logger.debug("notify work_chat with premium emoji failed: %s", e)
        try:
            await bot.send_message(wc, text)
        except Exception as e2:
            logger.debug("notify work_chat plain failed: %s", e2)


# ════════════════════════════════════════════════════════════════
# Polish — редактирование ЛК + дропа
# ════════════════════════════════════════════════════════════════

class EditForm(StatesGroup):
    waiting_lk_value = State()
    waiting_lk_deal = State()
    waiting_drop_fio = State()


@router.callback_query(F.data.startswith("lkeditvalue:"))
async def cb_lkeditvalue(call: CallbackQuery, state: FSMContext):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(EditForm.waiting_lk_value)
    await state.update_data(droplk_id=droplk_id)
    await call.message.reply(
        f"<b>✏️ Изменить данные ЛК {lk.get('bank')}</b>\n\n"
        f"Текущее значение:\n<code>{lk.get('value') or '—'}</code>\n\n"
        f"Введите новое значение:"
    )


@router.message(EditForm.waiting_lk_value, F.text & ~F.text.startswith("/"))
async def handle_lk_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    droplk_id = data.get("droplk_id")
    value = (message.text or "").strip()
    if not droplk_id or len(value) < 2:
        await ephemeral(message, "❌ Слишком коротко")
        return
    await crm_storage.update_crm_drop_lk(droplk_id, value=value)
    await state.clear()
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    await message.reply(f"✅ Данные ЛК {lk.get('bank')} обновлены")


@router.callback_query(F.data.startswith("lkeditdeal:"))
async def cb_lkeditdeal(call: CallbackQuery, state: FSMContext):
    """DEPRECATED: ручной ввод номера сделки больше не используется.
    Сделки создаются автоматически через AI-ассистента (record_deal).
    Хэндлер оставлен только чтобы старые кнопки в чатах не падали с ошибкой."""
    await call.answer(
        "ℹ️ Номера сделок теперь привязываются автоматически через ассистента.",
        show_alert=True,
    )


@router.message(EditForm.waiting_lk_deal, F.text & ~F.text.startswith("/"))
async def handle_lk_deal(message: Message, state: FSMContext):
    data = await state.get_data()
    droplk_id = data.get("droplk_id")
    deal = (message.text or "").strip().lstrip("#")
    if deal in ("-", "—"):
        deal = ""
    if not droplk_id:
        return
    await crm_storage.update_crm_drop_lk(droplk_id, deal=deal)
    await state.clear()
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    await message.reply(f"✅ Сделка ЛК {lk.get('bank')}: <b>{deal or '—'}</b>")


@router.callback_query(F.data.startswith("dropeditfio:"))
async def cb_dropeditfio(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(EditForm.waiting_drop_fio)
    await state.update_data(drop_id=drop_id, menu_msg_id=call.message.message_id)
    try:
        await call.message.edit_text(
            f"<b>✏️ Изменить ФИО</b>\n\n"
            f"Текущее: <b>{drop.get('fio')}</b>\n\n"
            f"Введите новое ФИО:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"drop:{drop_id}"),
            ]]),
        )
    except TelegramBadRequest:
        pass


@router.message(EditForm.waiting_drop_fio, F.text & ~F.text.startswith("/"))
async def handle_drop_fio_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    drop_id = data.get("drop_id")
    fio = (message.text or "").strip()
    if not drop_id or len(fio) < 5 or len(fio) > 100:
        await ephemeral(message, "❌ ФИО 5-100 символов")
        return
    await crm_storage.update_crm_drop(drop_id, fio=fio)
    await state.clear()
    drop = crm_storage.get_crm_drop(drop_id)
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("menu_msg_id"):
        await _safe_delete(message.bot, message.chat.id, data["menu_msg_id"])
    await _show_drop(message, drop)


# ─── Команды владельца ────────────────────────────────────────

@router.message(Command("crm_register_chat"))
async def cmd_register_chat(message: Message):
    if not is_owner(message.from_user.id):
        return
    if message.chat.type == "private":
        await message.reply("Команду нужно вызывать <b>в группе</b>.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "❌ Укажи username партнёра:\n"
            "<code>/crm_register_chat @username</code>"
        )
        return
    username = parts[1].strip().lstrip("@")
    owner = crm_storage.find_crm_owner_by_username(username)
    if not owner:
        owner_id = await crm_storage.add_crm_owner(
            tg_user_id=0, username=username, name=username,
            work_chat_id=message.chat.id,
        )
        owner = crm_storage.get_crm_owner(owner_id)
    else:
        await crm_storage.update_crm_owner(owner["owner_id"], work_chat_id=message.chat.id)
    await crm_storage.register_crm_chat(
        chat_id=message.chat.id, owner_id=owner["owner_id"],
        is_admin=False, is_password=False,
    )
    await message.reply(
        f"✅ Группа закреплена за партнёром <b>@{username}</b>.\n"
        f"Партнёр может теперь использовать /clients."
    )


@router.message(Command("crm_set_admin"))
async def cmd_set_admin(message: Message):
    if not is_owner(message.from_user.id):
        return
    if message.chat.type == "private":
        await message.reply("Команду нужно вызывать <b>в группе</b>.")
        return
    await crm_storage.register_crm_chat(
        chat_id=message.chat.id, owner_id="_admin",
        is_admin=True, is_password=False,
    )
    await message.reply(
        "✅ Эта группа теперь <b>admin-чат CRM</b>.\n"
        "Сюда будут падать новые дропы для обработки админами PRIDE."
    )


@router.message(Command("crm_set_password"))
async def cmd_set_password(message: Message):
    if not is_owner(message.from_user.id):
        return
    if message.chat.type == "private":
        await message.reply("Команду нужно вызывать <b>в группе</b>.")
        return
    await crm_storage.register_crm_chat(
        chat_id=message.chat.id, owner_id="_password",
        is_admin=False, is_password=True,
    )
    await message.reply(
        "✅ Эта группа теперь <b>password-чат CRM</b>.\n"
        "Сюда падают данные ЛК для заполнения (RDP / новые пароли)."
    )


@router.message(Command("crm_unregister"))
async def cmd_unregister(message: Message):
    if not is_owner(message.from_user.id):
        return
    if message.chat.type == "private":
        return
    ok = await crm_storage.unregister_crm_chat(message.chat.id)
    await message.reply("✅ Закрепление снято" if ok else "ℹ Эта группа не была закреплена")


@router.message(Command("crm_info"))
async def cmd_info(message: Message):
    if not is_owner(message.from_user.id):
        return
    owners = crm_storage.list_crm_owners()
    drops = crm_storage.list_crm_drops()
    lks = crm_storage.list_crm_drop_lks()
    chats = crm_storage.list_crm_chats()
    admin_chat = crm_storage.find_crm_admin_chat()
    pwd_chat = crm_storage.find_crm_password_chat()
    text = (
        "<b>🗂 CRM Status:</b>\n\n"
        f"• Партнёров: <b>{len(owners)}</b>\n"
        f"• Клиентов: <b>{len(drops)}</b>\n"
        f"• ЛК банков: <b>{len(lks)}</b>\n"
        f"• Закреплённых групп: <b>{len(chats)}</b>\n\n"
        f"• Admin chat: <code>{admin_chat or 'не задан'}</code>\n"
        f"• Password chat: <code>{pwd_chat or 'не задан'}</code>\n"
    )
    await message.reply(text)


@router.message(Command("crm_pending"))
async def cmd_pending(message: Message):
    """Список drops в pending/draft статусе — для контроля SIMBA."""
    if not is_owner(message.from_user.id):
        return
    all_drops = crm_storage.list_crm_drops()
    pending = [d for d in all_drops.values()
               if d.get("status") in ("pending", "draft", "in_review")]
    if not pending:
        await message.reply("✅ Нет дропов в ожидании")
        return
    # Сортировка по дате создания (новые внизу)
    pending.sort(key=lambda d: d.get("created_at") or "")
    lines = ["<b>⏳ Ожидают приёмки:</b>\n"]
    for d in pending[:40]:
        owner = crm_storage.get_crm_owner(d.get("owner_id", "")) or {}
        owner_label = owner.get("username") or owner.get("name") or "?"
        lks_cnt = len(crm_storage.list_crm_drop_lks(drop_id=d["drop_id"]))
        status = d.get("status", "?")
        status_icon = {"draft": "✏️", "pending": "⏳", "in_review": "👀"}.get(status, "❔")
        lines.append(
            f"{status_icon} <code>{d['drop_id']}</code> · "
            f"<b>{d.get('fio') or '—'}</b> · "
            f"ЛК: {lks_cnt} · "
            f"@{owner_label}"
        )
    if len(pending) > 40:
        lines.append(f"\n<i>… и ещё {len(pending) - 40}</i>")
    await message.reply("\n".join(lines))


# ════════════════════════════════════════════════════════════════
# АДМИН-ПАНЕЛЬ CRM-бота (/admincrm)
# Доступна только владельцам (CRM_OWNER_IDS).
# Меню: рассылка по клиентам / модерация партнёров / регулятор.
# ════════════════════════════════════════════════════════════════

class AdminCRMFSM(StatesGroup):
    bc_filter_value = State()    # ожидаем значение фильтра (банк/метод/статус)
    bc_text = State()             # ожидаем текст рассылки
    bc_confirm = State()          # ожидаем подтверждения
    warn_reason = State()         # причина предупреждения партнёру


def _admincrm_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка по клиентам", callback_data="ac:broadcast")],
        [InlineKeyboardButton(text="🚫 Модерация партнёров", callback_data="ac:mod")],
        [InlineKeyboardButton(text="📊 Регулятор CRM",       callback_data="ac:reg")],
        [InlineKeyboardButton(text="💰 Прайс ЛК",            callback_data="ac:pricing")],
        [InlineKeyboardButton(text="❌ Закрыть",             callback_data="ac:close")],
    ])


def _bc_filter_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 По банку ЛК",       callback_data="ac:bc_f:bank")],
        [InlineKeyboardButton(text="📊 По статусу дропа", callback_data="ac:bc_f:status")],
        [InlineKeyboardButton(text="💳 По методу оплаты",  callback_data="ac:bc_f:method")],
        [InlineKeyboardButton(text="🌐 Всем активным",     callback_data="ac:bc_f:all")],
        [InlineKeyboardButton(text="◀️ Назад",             callback_data="ac:main")],
    ])


@router.message(Command("admincrm"))
async def cmd_admincrm(message: Message, state: FSMContext):
    """Главное меню админки CRM. Доступно только для CRM_OWNER_IDS."""
    if not is_owner(message.from_user.id):
        return
    await state.clear()
    await message.reply(
        "🛠 <b>Админ-панель CRM</b>\n\nВыбери раздел:",
        reply_markup=_admincrm_menu_kb(),
    )


@router.callback_query(F.data.startswith("ac:"))
async def cb_admincrm(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        await call.answer("Нет прав", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    await call.answer()

    if action == "close":
        try:
            await call.message.delete()
        except Exception:
            pass
        return

    if action == "main":
        await state.clear()
        try:
            await call.message.edit_text(
                "🛠 <b>Админ-панель CRM</b>\n\nВыбери раздел:",
                reply_markup=_admincrm_menu_kb(),
            )
        except Exception:
            await call.message.answer(
                "🛠 <b>Админ-панель CRM</b>",
                reply_markup=_admincrm_menu_kb(),
            )
        return

    if action == "broadcast":
        try:
            await call.message.edit_text(
                "📢 <b>Рассылка по клиентским чатам</b>\n\n"
                "<i>Шаги: фильтр → текст → подтверждение → отправка.</i>\n\n"
                "Выбери фильтр:",
                reply_markup=_bc_filter_kb(),
            )
        except Exception:
            pass
        return

    if action.startswith("bc_f:"):
        kind = action.split(":", 1)[1]
        await state.update_data(bc_filter_kind=kind, bc_filter_value=None)
        if kind == "all":
            await state.update_data(bc_filter_value="*")
            await call.message.edit_text(
                "📢 Фильтр: <b>Всем активным</b>\n\n"
                "Пришли текст рассылки одним сообщением (или /admincrm чтобы отменить):",
            )
            await state.set_state(AdminCRMFSM.bc_text)
            return
        # Просим значение
        prompts = {
            "bank":   "Пришли название банка (например: <code>Альфа</code>, <code>Сбер</code>, <code>Озон</code>).",
            "status": "Пришли статус: <code>accepted</code> / <code>done</code> / <code>pending</code> / <code>draft</code>.",
            "method": "Пришли метод оплаты: <code>GUARANTOR_AFTER_WORK</code> / <code>USDT_TRC20</code> / <code>GUARANTOR_BEFORE</code>.",
        }
        await call.message.edit_text(
            f"📢 Фильтр: <b>{kind}</b>\n\n{prompts.get(kind, '')}\n\nИли /admincrm чтобы отменить.",
        )
        await state.set_state(AdminCRMFSM.bc_filter_value)
        return

    if action == "mod":
        owners = crm_storage.list_crm_owners()
        # Топ-15 партнёров с warn/ban для быстрого доступа
        items = []
        for oid, o in owners.items():
            warns = int(o.get("warnings") or 0)
            banned = int(o.get("banned_until") or 0)
            items.append((oid, o, warns, banned))
        items.sort(key=lambda x: (-(x[3] > time.time()), -x[2], -(o.get("total_drops") or 0)))
        items = items[:15]
        text = "🚫 <b>Модерация партнёров</b> (топ-15)\n\n"
        rows = []
        for oid, o, warns, banned in items:
            tag = ""
            if banned > time.time():
                tag = "🚫"
            elif warns > 0:
                tag = f"⚠️{warns}"
            label = f"{tag} @{o.get('username') or oid}"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"ac:mod_o:{oid}")])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ac:main")])
        try:
            await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            pass
        return

    if action.startswith("mod_o:"):
        oid = action.split(":", 1)[1]
        owner = crm_storage.get_crm_owner(oid)
        if not owner:
            return
        banned = int(owner.get("banned_until") or 0)
        warns = int(owner.get("warnings") or 0)
        banned_str = "🚫 ЗАБАНЕН" if banned > time.time() else "—"
        text = (
            f"👤 <b>@{owner.get('username') or oid}</b>\n"
            f"TG ID: <code>{owner.get('tg_user_id')}</code>\n"
            f"Warnings: <b>{warns}</b>\n"
            f"Ban: {banned_str}\n"
            f"Drops: <b>{owner.get('total_drops') or 0}</b>"
        )
        rows = [
            [InlineKeyboardButton(text="⚠️ +1 warn", callback_data=f"ac:mod_warn:{oid}")],
        ]
        if banned > time.time():
            rows.append([InlineKeyboardButton(text="↩️ Снять бан", callback_data=f"ac:mod_unban:{oid}")])
        else:
            rows.append([InlineKeyboardButton(text="🚫 Бан на 7 дней", callback_data=f"ac:mod_ban:{oid}")])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ac:mod")])
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if action.startswith("mod_warn:"):
        oid = action.split(":", 1)[1]
        o = crm_storage.get_crm_owner(oid) or {}
        warns = int(o.get("warnings") or 0) + 1
        await crm_storage.update_crm_owner(oid, warnings=warns)
        await call.answer(f"⚠️ Warns: {warns}")
        # Уведомим партнёра
        try:
            if o.get("tg_user_id"):
                await call.message.bot.send_message(
                    o["tg_user_id"],
                    f"⚠️ Получено предупреждение от админа. Всего warnings: <b>{warns}</b>",
                )
        except Exception:
            pass
        return

    if action.startswith("mod_ban:"):
        oid = action.split(":", 1)[1]
        until = time.time() + 7 * 86400
        await crm_storage.update_crm_owner(oid, banned_until=until)
        await call.answer("🚫 Забанен на 7 дней")
        o = crm_storage.get_crm_owner(oid) or {}
        try:
            if o.get("tg_user_id"):
                await call.message.bot.send_message(
                    o["tg_user_id"],
                    "🚫 Вы временно заблокированы в CRM на <b>7 дней</b>.\n"
                    "По вопросам — пишите SIMBA.",
                )
        except Exception:
            pass
        return

    if action.startswith("mod_unban:"):
        oid = action.split(":", 1)[1]
        await crm_storage.update_crm_owner(oid, banned_until=0)
        await call.answer("↩️ Бан снят")
        o = crm_storage.get_crm_owner(oid) or {}
        try:
            if o.get("tg_user_id"):
                await call.message.bot.send_message(
                    o["tg_user_id"],
                    "✅ Блокировка снята — снова можешь работать с CRM.",
                )
        except Exception:
            pass
        return

    if action == "reg":
        owners = crm_storage.list_crm_owners()
        drops = crm_storage.list_crm_drops()
        lks = crm_storage.list_crm_drop_lks()
        drafts = sum(1 for d in drops.values() if d.get("status") == "draft")
        managed = crm_storage.state.get("managed_chats") or {}
        text = (
            "📊 <b>Регулятор CRM</b>\n\n"
            f"• Партнёров: <b>{len(owners)}</b>\n"
            f"• Клиентов: <b>{len(drops)}</b> (драфтов: {drafts})\n"
            f"• ЛК: <b>{len(lks)}</b>\n"
            f"• Managed-чатов: <b>{len(managed)}</b>\n"
        )
        rows = [
            [InlineKeyboardButton(text="🧹 Очистить драфты", callback_data="ac:reg_clean_drafts")],
            [InlineKeyboardButton(text="💾 Бэкап state.json", callback_data="ac:reg_backup")],
            [InlineKeyboardButton(text="🔄 Пересчитать статистику", callback_data="ac:reg_recalc")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ac:main")],
        ]
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if action == "reg_clean_drafts":
        drops = crm_storage.list_crm_drops()
        now = time.time()
        old = [did for did, d in drops.items()
               if d.get("status") == "draft"
               and (now - (d.get("created_at") or 0)) > 7 * 86400]
        for did in old:
            await crm_storage.delete_crm_drop(did)
        await call.answer(f"🧹 Удалено драфтов: {len(old)}")
        return

    if action == "reg_backup":
        # Просто триггерим save (state.json уже на диске)
        try:
            await crm_storage._save_unlocked()
            await call.answer("💾 state.json сохранён")
        except Exception as e:
            await call.answer(f"Err: {e}", show_alert=True)
        return

    if action == "reg_recalc":
        owners = crm_storage.list_crm_owners()
        drops = crm_storage.list_crm_drops()
        # Пересчёт total_drops у каждого owner
        for oid, o in owners.items():
            cnt = sum(1 for d in drops.values() if d.get("owner_id") == oid)
            if (o.get("total_drops") or 0) != cnt:
                await crm_storage.update_crm_owner(oid, total_drops=cnt)
        await call.answer("🔄 Стата пересчитана")
        return

    if action == "pricing":
        pricing = crm_storage.state.get("pricing") or {}
        if not pricing:
            text = "💰 <b>Прайс ЛК</b>\n\n<i>Пустой. Установи через брейн-чат: «прайс БАНК ЦЕНА».</i>"
        else:
            lines = [f"  • <b>{k}</b>: ${v}" for k, v in sorted(pricing.items())]
            text = "💰 <b>Прайс ЛК</b>\n\n" + "\n".join(lines)
        rows = [[InlineKeyboardButton(text="◀️ Назад", callback_data="ac:main")]]
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return


@router.message(AdminCRMFSM.bc_filter_value, F.text & ~F.text.startswith("/"))
async def fsm_bc_filter_value(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    await state.update_data(bc_filter_value=(message.text or "").strip())
    await message.reply(
        f"📢 Фильтр: <code>{message.text.strip()}</code>\n\n"
        "Пришли <b>текст рассылки</b> одним сообщением:"
    )
    await state.set_state(AdminCRMFSM.bc_text)


@router.message(AdminCRMFSM.bc_text, F.text & ~F.text.startswith("/"))
async def fsm_bc_text(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        return
    await state.update_data(bc_text=text)
    data = await state.get_data()
    # Соберём список целевых чатов
    chats = _collect_broadcast_targets(
        data.get("bc_filter_kind"),
        data.get("bc_filter_value"),
    )
    if not chats:
        await message.reply("❌ По фильтру никого не нашлось.")
        await state.clear()
        return
    await message.reply(
        f"📢 <b>Подтверждение рассылки</b>\n\n"
        f"Фильтр: <b>{data.get('bc_filter_kind')}</b> = <code>{data.get('bc_filter_value')}</code>\n"
        f"Получателей: <b>{len(chats)}</b>\n\n"
        f"<b>Превью:</b>\n{text[:300]}{'...' if len(text)>300 else ''}\n\n"
        f"Отправить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="ac:bc_send")],
            [InlineKeyboardButton(text="❌ Отменить",  callback_data="ac:main")],
        ]),
    )
    await state.update_data(bc_targets=chats)
    await state.set_state(AdminCRMFSM.bc_confirm)


@router.callback_query(F.data == "ac:bc_send")
async def cb_bc_send(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    chats = data.get("bc_targets") or []
    text = data.get("bc_text") or ""
    if not chats or not text:
        await call.answer("Нет данных")
        await state.clear()
        return
    await call.answer("🚀 Шлю...")
    sent = 0
    failed = 0
    for chat_id in chats:
        try:
            await call.message.bot.send_message(chat_id, text)
            sent += 1
        except Exception as e:
            failed += 1
            logger.debug("bc fail %s: %s", chat_id, e)
        await asyncio.sleep(0.05)  # тротлинг
    await state.clear()
    try:
        await call.message.edit_text(
            f"📊 <b>Рассылка завершена</b>\n\n"
            f"✅ Отправлено: <b>{sent}</b>\n"
            f"❌ Не удалось: <b>{failed}</b>",
            reply_markup=_admincrm_menu_kb(),
        )
    except Exception:
        pass


def _collect_broadcast_targets(kind: str, value: str) -> list:
    """Возвращает список chat_id для рассылки по фильтру."""
    drops = crm_storage.list_crm_drops()
    lks = crm_storage.list_crm_drop_lks()
    owners = crm_storage.list_crm_owners()
    managed = crm_storage.state.get("managed_chats") or {}

    # Все «активные» work_chat'ы клиентов (drops.work_chat_id)
    drop_chats = set()
    for d in drops.values():
        wc = d.get("work_chat_id")
        if wc:
            drop_chats.add(int(wc))

    if kind == "all":
        return list(drop_chats)

    if kind == "bank":
        bank_filter = (value or "").upper().strip()
        # Найти drop_id'ы где есть ЛК с этим банком
        matching_drops = set()
        for l in lks.values():
            if bank_filter in (l.get("bank") or "").upper():
                matching_drops.add(l.get("drop_id"))
        out = []
        for did in matching_drops:
            d = drops.get(did) or {}
            wc = d.get("work_chat_id")
            if wc:
                out.append(int(wc))
        return out

    if kind == "status":
        target = (value or "").lower().strip()
        out = []
        for d in drops.values():
            if (d.get("status") or "").lower() == target and d.get("work_chat_id"):
                out.append(int(d["work_chat_id"]))
        return out

    if kind == "method":
        target = (value or "").upper().strip()
        # Метод оплаты — в lk_cards (общая таблица), фильтруем по supplier=username
        cards = crm_storage.state.get("lk_cards") or {}
        matching_suppliers = set()
        for c in cards.values():
            if (c.get("payment_method") or "").upper() == target:
                matching_suppliers.add((c.get("supplier") or "").lstrip("@").lower())
        out = []
        for oid, o in owners.items():
            uname = (o.get("username") or "").lower()
            if uname in matching_suppliers:
                # Шлём в work_chat дропов этого партнёра
                for d in drops.values():
                    if d.get("owner_id") == oid and d.get("work_chat_id"):
                        out.append(int(d["work_chat_id"]))
        return out

    return []


# ════════════════════════════════════════════════════════════════
# Payment-due reminders loop
# ════════════════════════════════════════════════════════════════

# Конфигурируется через env: дни до напоминания (default 3)
PAYMENT_REMINDER_DAYS = int(os.getenv("CRM_PAYMENT_REMINDER_DAYS", "3") or 3)
# Период проверки в секундах (default 1 час)
_PAYMENT_LOOP_INTERVAL = int(os.getenv("CRM_PAYMENT_LOOP_INTERVAL", "3600") or 3600)
# Cooldown между напоминаниями про один и тот же дроп (24 часа)
_REMIND_COOLDOWN = 24 * 3600


async def _payment_reminder_tick(bot) -> int:
    """Один тик проверки. Возвращает количество отправленных напоминаний."""
    now = time.time()
    threshold = now - PAYMENT_REMINDER_DAYS * 86400
    drops = crm_storage.list_crm_drops()
    sent = 0
    for drop_id, drop in (drops or {}).items():
        status = drop.get("status")
        accept_ts = drop.get("accept_ts") or 0
        last_remind = drop.get("last_remind_ts") or 0
        # Условие: принят в работу, но >N дней не закрыт, и не напоминали последние 24ч
        if status != "accepted":
            continue
        if not accept_ts or accept_ts > threshold:
            continue
        if now - last_remind < _REMIND_COOLDOWN:
            continue

        owner = crm_storage.get_crm_owner(drop.get("owner_id", "")) or {}
        days_in_work = int((now - accept_ts) / 86400)
        lks = list(crm_storage.list_crm_drop_lks(drop_id=drop_id).values())
        done_count = sum(1 for l in lks if l.get("status") == "done")
        total = len(lks)
        progress = (str(done_count) + "/" + str(total)) if total else "0/0"

        reminder_text = (
            f"⏰ <b>Напоминание о выплате</b>\n\n"
            f"Клиент <b>{drop.get('fio') or '—'}</b> в работе <b>{days_in_work} дн.</b>\n"
            f"Прогресс ЛК: <b>{progress}</b>\n"
            f"Цена: ${drop.get('price_usdt') or 0}\n\n"
            f"<i>Если уже отработан — пометьте «✅ Успешно отработано» в чате паролей.</i>"
        )

        # 1) DM партнёру
        if owner.get("tg_user_id"):
            try:
                await bot.send_message(owner["tg_user_id"], reminder_text)
                sent += 1
            except Exception as e:
                logger.debug("payment reminder DM failed: %s", e)
        # 2) work_chat партнёра
        try:
            await _notify_work_chat(bot, owner, reminder_text)
        except Exception:
            pass
        # 3) admin-чат CRM
        try:
            admin_chat_id = await get_admin_chat_resolved(bot)
            if admin_chat_id:
                await bot.send_message(
                    admin_chat_id,
                    f"⏰ <b>{drop.get('fio')}</b> @{owner.get('username') or '—'} — "
                    f"{days_in_work}д в работе, прогресс {progress}",
                )
        except Exception:
            pass

        # Помечаем что напомнили
        try:
            await crm_storage.update_crm_drop(drop_id, last_remind_ts=now)
        except Exception:
            pass

        # SSE
        _emit_crm_event("drop.reminder", {
            "drop_id": drop_id, "fio": drop.get("fio"),
            "days_in_work": days_in_work, "progress": progress,
        }, severity="warning")

    return sent


async def _payment_reminder_loop(bot):
    """Бесконечный воркер: каждый час проверяет просроченные дропы."""
    await asyncio.sleep(30)
    while True:
        try:
            n = await _payment_reminder_tick(bot)
            if n > 0:
                logger.info("CRM payment reminders sent: %d", n)
        except Exception as e:
            logger.warning("payment reminder tick failed: %s", e)
        try:
            await asyncio.sleep(_PAYMENT_LOOP_INTERVAL)
        except asyncio.CancelledError:
            return


# ════════════════════════════════════════════════════════════════
# SimbaBySnep premium emoji pack — украшение CRM-сообщений
# ════════════════════════════════════════════════════════════════

_SIMBA_PACK_NAME = "SimbaBySnep"
_simba_emoji_cache: list = []

# Эмодзи которые НЕ должны попадать на позитивные сообщения (грусть/злость/ужас).
_NEGATIVE_EMOJI = {
    "😞", "😢", "😭", "😨", "😱", "😰", "😟", "😣", "😖", "😩", "😫",
    "😤", "😡", "😠", "🤬", "💀", "☠️", "🤢", "🤮", "🥶", "🥵",
    "😵", "😴", "🤧", "🤒", "🤕", "😪", "😓", "🙁", "☹️", "😕",
    "😦", "😧", "😨", "😬", "😮‍💨", "💔",
}


async def _load_simba_emoji_pack(bot) -> None:
    """Загружаем premium-эмодзи пак SimbaBySnep, кэшируем document_id'ы.
    Фильтруем грустные/негативные — они не подходят для позитивных событий."""
    global _simba_emoji_cache
    try:
        from aiogram.methods import GetStickerSet
        pack = await bot(GetStickerSet(name=_SIMBA_PACK_NAME))
        result = []
        skipped = 0
        for st in pack.stickers:
            doc_id = getattr(st, "custom_emoji_id", None)
            emoji = st.emoji or "✨"
            if not doc_id:
                continue
            if emoji in _NEGATIVE_EMOJI:
                skipped += 1
                continue
            result.append({"emoji": emoji, "document_id": str(doc_id)})
        _simba_emoji_cache = result
        logger.info(
            "SimbaBySnep loaded: %d positive emoji (filtered %d negative)",
            len(result), skipped,
        )
    except Exception as e:
        logger.warning("SimbaBySnep load failed (fallback to plain): %s", e)
        _simba_emoji_cache = []


def _pick_simba() -> dict:
    if not _simba_emoji_cache:
        return {}
    import random
    return random.choice(_simba_emoji_cache)


def _decor(text: str) -> str:
    """Префиксует текст ОДНИМ premium-эмодзи через <tg-emoji>.
    Если в начале текста уже есть эмодзи (✅ 📈 💰 и т.п.) — НЕ добавляем
    второй чтоб не было «гирлянды»."""
    if not text:
        return text
    # Если первый символ — уже эмодзи или знак статуса, не префигурируем
    first = text.lstrip()[:2]
    if first and any(ord(c) > 0x2600 for c in first):
        return text
    em = _pick_simba()
    if not em:
        return text
    char = em["emoji"]
    doc_id = em["document_id"]
    tag = f'<tg-emoji emoji-id="{doc_id}">{char}</tg-emoji>'
    return tag + " " + text


# ════════════════════════════════════════════════════════════════
# ENTRYPOINT — вызывается из bot.py как asyncio.create_task()
# ════════════════════════════════════════════════════════════════

async def run_crm_bot():
    """Главный entrypoint CRM-бота. Используется из bot.py."""
    if not CRM_BOT_TOKEN:
        logger.warning("CRM bot: токен не задан, не запускаем.")
        return

    logger.info(
        "CRM bot init. Owners=%d Drops=%d LK=%d",
        len(crm_storage.list_crm_owners()),
        len(crm_storage.list_crm_drops()),
        len(crm_storage.list_crm_drop_lks()),
    )

    bot = Bot(
        token=CRM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)


    try:
        me = await bot.get_me()
        logger.info("✅ CRM bot online: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.error("CRM bot getMe failed: %s", e)
        await bot.session.close()
        return

    # Загружаем SimbaBySnep premium-эмодзи (best-effort)
    try:
        await _load_simba_emoji_pack(bot)
    except Exception as e:
        logger.debug("simba pack load skipped: %s", e)

    reminder_task = None
    try:
        reminder_task = asyncio.create_task(_payment_reminder_loop(bot))
        logger.info(
            "CRM payment reminder loop started (every %ds, threshold %dd)",
            _PAYMENT_LOOP_INTERVAL, PAYMENT_REMINDER_DAYS,
        )
    except Exception as e:
        logger.warning("CRM reminder loop start failed: %s", e)

    try:
        await dp.start_polling(bot, polling_timeout=30)
    except Exception as e:
        logger.error("CRM bot polling crashed: %s", e)
    finally:
        if reminder_task and not reminder_task.done():
            reminder_task.cancel()
        try:
            await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    async def _standalone():
        await crm_storage.load()
        await run_crm_bot()
    asyncio.run(_standalone())

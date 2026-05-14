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
_HARDCODED_OWNER_IDS = {8151738775, 397572312, 5830088389}
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
HARDCODED_ADMIN_CHAT_ID = -1003390329578     # «Доступы» — приёмка дропов
HARDCODED_PASSWORD_CHAT_ID = -1005217307307  # «Пароли» — RDP + новые пароли


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
    waiting_new_password = State()
    waiting_new_mail = State()
    waiting_new_number = State()
    waiting_ded_ip = State()
    waiting_ded_pass = State()


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
    drops = crm_storage.list_crm_drops(owner_id=owner["owner_id"])
    drops_active = sum(1 for d in drops.values() if d.get("status") in ("pending", "accepted"))
    drops_done = sum(1 for d in drops.values() if d.get("status") == "done")

    text = (
        f"👤 <b>Профиль партнёра</b>\n\n"
        f"<b>Имя:</b> {owner.get('name') or '—'}\n"
        f"<b>Username:</b> @{owner.get('username') or '—'}\n"
        f"<b>ID:</b> <code>{owner['owner_id']}</code>\n"
        f"<b>С нами с:</b> {joined}\n\n"
        f"<b>📊 Статистика:</b>\n"
        f"• Всего клиентов: <b>{drops_total}</b>\n"
        f"• Активных: <b>{drops_active}</b>\n"
        f"• Отработано: <b>{drops_done}</b>\n"
        f"• Заработано: <b>${revenue:.0f}</b>\n"
        f"• Рейтинг: <b>{rating:.1f}/5.0</b> ⭐\n"
    )
    kb = [[InlineKeyboardButton(text="📇 Мои клиенты", callback_data="drops")]]
    if in_group:
        kb.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")])
    await _send(message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


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

    if drop.get("status") in ("draft", "brak"):
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
        f"<b>Данные ЛК:</b>\n<code>{lk.get('value') or '—'}</code>\n\n"
        f"<b>Сделка:</b> {lk.get('deal') or '—'}\n"
    )
    if lk.get("new_password") or lk.get("ded_ip"):
        text += (
            f"\n<b>Заполнено админом:</b>\n"
            f"Новый пароль: {lk.get('new_password') or '—'}\n"
            f"Новая почта: {lk.get('new_mail') or '—'}\n"
            f"Дедик IP: {lk.get('ded_ip') or '—'}\n"
        )
    if lk.get("sms_history"):
        text += "\n<b>📩 SMS:</b>\n"
        for s in lk["sms_history"][-10:]:
            text += f"  • {s.get('code')} — {s.get('time')}\n"
    kb = [
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

    admin_chat_id = get_admin_chat_id()
    if not admin_chat_id:
        await call.answer("Admin-чат не настроен", show_alert=True)
        return

    await call.answer("⏳ Отправляю...")
    bot = call.message.bot

    # Проверим что бот ИМЕЕТ ДОСТУП к admin-чату.
    # Перебираем варианты формата ID (с -100, без, и т.д.)
    resolved = await _resolve_chat_id_variants(bot, admin_chat_id)
    if not resolved:
        await ephemeral(
            call.message,
            f"❌ Бот не имеет доступа к admin-чату.\n"
            f"Пробовал: <code>{admin_chat_id}</code>, "
            f"<code>-100{str(admin_chat_id).lstrip('-')}</code>\n\n"
            f"<b>Что сделать:</b>\n"
            f"1. Открой саму группу «Доступы»\n"
            f"2. Убедись что CRM-бот в ней как админ\n"
            f"3. Напиши в группе: <code>/crm_set_admin</code>\n"
            f"   — бот возьмёт правильный chat_id сам.",
            ttl=30,
        )
        return
    if resolved != admin_chat_id:
        # Сохраним правильный формат в storage чтоб в след. раз не угадывать
        try:
            await crm_storage.register_crm_chat(
                chat_id=resolved, owner_id="_admin",
                is_admin=True, is_password=False,
            )
            logger.info("Admin chat auto-corrected: %s → %s", admin_chat_id, resolved)
        except Exception:
            pass
        admin_chat_id = resolved

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
    await call.answer("⏳ Принимаю...")
    bot = call.message.bot
    pwd_chat = get_password_chat_id()
    if not pwd_chat:
        await ephemeral(call.message, "❌ Password-чат не настроен")
        return

    await crm_storage.update_crm_drop(drop_id, status="accepted", accept_ts=time.time())
    drop = crm_storage.get_crm_drop(drop_id)
    lks = crm_storage.list_crm_drop_lks(drop_id=drop_id)

    # Постим в password-чат — на каждый ЛК отдельное сообщение с кнопкой «Заполнить»
    for lk in lks.values():
        text2 = _render_password_text(drop, lk)
        try:
            msg = await bot.send_message(
                pwd_chat, text2,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✏️ Заполнить", callback_data=f"filldrop:{lk['droplk_id']}"),
                ]]),
            )
            # Сформируем link_pass
            pwd_str = str(pwd_chat).replace("-100", "")
            link_pass = f"https://t.me/c/{pwd_str}/{msg.message_id}"
            await crm_storage.update_crm_drop_lk(
                lk["droplk_id"],
                msgid_pass=msg.message_id, link_pass=link_pass,
            )
        except Exception as e:
            logger.warning("acceptdrop password post failed for lk=%s: %s", lk["droplk_id"], e)

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

    # Уведомляем партнёра в его чате
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    if owner and owner.get("work_chat_id"):
        try:
            await bot.send_message(
                owner["work_chat_id"],
                f"✅ <b>Клиент {drop.get('fio')} принят в работу.</b>",
            )
        except Exception:
            pass


def _render_password_text(drop: dict, lk: dict) -> str:
    return (
        f"<b>ФИО:</b> {drop.get('fio') or '—'}\n"
        f"<b>Банк:</b> {lk.get('bank')}\n\n"
        f"<b>Новый пароль:</b> {lk.get('new_password') or '—'}\n"
        f"<b>Новая почта:</b> {lk.get('new_mail') or '—'}\n"
        f"<b>Новый номер:</b> {lk.get('new_number') or '—'}\n\n"
        f"<b>Дедик:</b>\n"
        f"IP: {lk.get('ded_ip') or '—'}\n"
        f"Логин: {lk.get('ded_login') or 'Administrator'}\n"
        f"Пароль: {lk.get('ded_pass') or '—'}"
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
    drop = crm_storage.get_crm_drop(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await crm_storage.update_crm_drop(drop_id, status="brak")
    await call.answer("Отклонено")
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
    await state.set_state(FillForm.waiting_new_password)
    await state.update_data(droplk_id=droplk_id, fill_data={})
    drop = crm_storage.get_crm_drop(lk["drop_id"])
    await call.message.reply(
        f"<b>✏️ Заполнение {lk.get('bank')} ({drop.get('fio')})</b>\n\n"
        f"<b>Шаг 1/5:</b> Новый пароль (или «-» если не меняли):"
    )


@router.message(FillForm.waiting_new_password, F.text & ~F.text.startswith("/"))
async def fill_pass(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["new_password"] = (message.text or "").strip()
    await state.update_data(**data)
    await state.set_state(FillForm.waiting_new_mail)
    await message.reply("<b>Шаг 2/5:</b> Новая почта (или «-»):")


@router.message(FillForm.waiting_new_mail, F.text & ~F.text.startswith("/"))
async def fill_mail(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["new_mail"] = (message.text or "").strip()
    await state.update_data(**data)
    await state.set_state(FillForm.waiting_new_number)
    await message.reply("<b>Шаг 3/5:</b> Новый номер (или «-»):")


@router.message(FillForm.waiting_new_number, F.text & ~F.text.startswith("/"))
async def fill_number(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["new_number"] = (message.text or "").strip()
    await state.update_data(**data)
    await state.set_state(FillForm.waiting_ded_ip)
    await message.reply("<b>Шаг 4/5:</b> IP дедика:")


@router.message(FillForm.waiting_ded_ip, F.text & ~F.text.startswith("/"))
async def fill_ip(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("fill_data", {})["ded_ip"] = (message.text or "").strip()
    await state.update_data(**data)
    await state.set_state(FillForm.waiting_ded_pass)
    await message.reply("<b>Шаг 5/5:</b> Пароль дедика:")


@router.message(FillForm.waiting_ded_pass, F.text & ~F.text.startswith("/"))
async def fill_pass2(message: Message, state: FSMContext):
    data = await state.get_data()
    fd = data.setdefault("fill_data", {})
    fd["ded_pass"] = (message.text or "").strip()
    droplk_id = data.get("droplk_id")
    if not droplk_id:
        await message.reply("❌ Сессия истекла")
        await state.clear()
        return
    # Сохраняем все 5 полей
    await crm_storage.update_crm_drop_lk(
        droplk_id,
        new_password=fd.get("new_password") or "",
        new_mail=fd.get("new_mail") or "",
        new_number=fd.get("new_number") or "",
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
    # Проверка: все ЛК дропа done → drop.status = done
    drop_id = lk.get("drop_id")
    if drop_id:
        all_lks = crm_storage.list_crm_drop_lks(drop_id=drop_id)
        if all_lks and all(l.get("status") == "done" for l in all_lks.values()):
            await crm_storage.update_crm_drop(drop_id, status="done", done_ts=time.time())


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

@router.callback_query(F.data.startswith("takecodedrop:"))
async def cb_takecodedrop(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await _request_sms(call, droplk_id, label="код", first_time=True)


@router.callback_query(F.data.startswith("takesmscodedrop:"))
async def cb_takesmscodedrop(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await _request_sms(call, droplk_id, label="SMS-код", first_time=False)


async def _request_sms(call: CallbackQuery, droplk_id: str, label: str, first_time: bool):
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    drop = crm_storage.get_crm_drop(lk.get("drop_id"))
    if not drop:
        await call.answer("Дроп не найден", show_alert=True)
        return
    owner = crm_storage.get_crm_owner(drop.get("owner_id") or "")
    if not owner or not owner.get("work_chat_id"):
        await call.answer("Чат партнёра не найден", show_alert=True)
        return
    await call.answer(f"Запрашиваю {label}...")
    # Пост в чат партнёра
    try:
        msg = await call.message.bot.send_message(
            owner["work_chat_id"],
            f"<b>📩 Прайд запрашивает {label}</b>\n\n"
            f"Банк: <b>{lk.get('bank')}</b>\n"
            f"Клиент: <b>{drop.get('fio')}</b>\n\n"
            f"Нажмите кнопку и отправьте {label} следующим сообщением.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"📩 Отправить {label}", callback_data=f"givemecode:{droplk_id}"),
            ]]),
        )
    except Exception as e:
        logger.warning("takecode post failed: %s", e)


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
    if not droplk_id:
        await state.clear()
        return
    lk = crm_storage.get_crm_drop_lk(droplk_id)
    if not lk:
        await state.clear()
        return
    drop = crm_storage.get_crm_drop(lk.get("drop_id"))
    # Сохраняем код
    await crm_storage.append_crm_sms(droplk_id, code=code)
    await state.clear()
    await message.reply(f"✅ Код <b>{code}</b> отправлен админам PRIDE.")
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


# ════════════════════════════════════════════════════════════════
# ENTRYPOINT — вызывается из bot.py как asyncio.create_task()
# ════════════════════════════════════════════════════════════════

async def run_crm_bot():
    """Главный entrypoint CRM-бота. Используется из bot.py."""
    if not CRM_BOT_TOKEN:
        logger.warning("CRM bot: токен не задан, не запускаем.")
        return

    # state.json уже загружен main-ботом — НЕ грузим снова
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

    try:
        await dp.start_polling(bot, polling_timeout=30)
    except Exception as e:
        logger.error("CRM bot polling crashed: %s", e)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass


# Запуск как standalone (для отладки локально):
#   python crm_bot.py
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    async def _standalone():
        await crm_storage.load()
        await run_crm_bot()
    asyncio.run(_standalone())

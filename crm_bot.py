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


# ════════════════════════════════════════════════════════════════
# FSM States
# ════════════════════════════════════════════════════════════════

class DropForm(StatesGroup):
    waiting_fio = State()
    waiting_about = State()
    waiting_scan = State()


class LKForm(StatesGroup):
    waiting_bank = State()
    waiting_value = State()


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════

def is_owner(user_id: int) -> bool:
    return int(user_id) in CRM_OWNER_IDS


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
    await message.reply(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


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
    await message.reply(text, reply_markup=markup)


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


async def _show_drop(message: Message, drop: dict):
    lks = crm_storage.list_crm_drop_lks(drop_id=drop["drop_id"])
    status_emoji = _drop_status_emoji(drop.get("status", "draft"))
    status_text = _drop_status_text(drop.get("status", "draft"))
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    owner_label = (
        owner and (owner.get("username") and f"@{owner['username']}" or owner.get("name"))
    ) or "—"

    lines = [
        f"{status_emoji} <b>Клиент {drop.get('fio') or '—'}</b>",
        f"<i>статус: {status_text}</i>",
        "",
        f"<b>Партнёр:</b> {owner_label}",
        f"<b>ID:</b> <code>{drop['drop_id']}</code>",
    ]
    if drop.get("about"):
        lines.append("")
        lines.append(f"<b>Анкета:</b>\n{drop['about']}")
    lines.append("")
    lines.append(f"📎 Документы: <b>{len(drop.get('scan_file_ids') or [])}</b>")
    lines.append(f"🏦 ЛК банков: <b>{len(lks)}</b>")

    kb_rows = [
        [InlineKeyboardButton(text="🏦 ЛК ИП", callback_data=f"droplk:{drop['drop_id']}")],
    ]
    if drop.get("scan_file_ids"):
        kb_rows.append([InlineKeyboardButton(text="👁 Посмотреть доки", callback_data=f"showdoc:{drop['drop_id']}")])
    kb_rows.extend([
        [InlineKeyboardButton(
            text="📎 " + ("Изменить" if drop.get("scan_file_ids") else "Добавить") + " доки",
            callback_data=f"dropdoc:{drop['drop_id']}",
        )],
        [InlineKeyboardButton(
            text="📝 " + ("Изменить" if drop.get("about") else "Добавить") + " анкету",
            callback_data=f"dropanketa:{drop['drop_id']}",
        )],
    ])
    if (
        drop.get("status") == "draft"
        and drop.get("about")
        and drop.get("scan_file_ids")
        and lks
    ):
        kb_rows.insert(0, [InlineKeyboardButton(
            text="🚀 Отдать в работу", callback_data=f"dropsend:{drop['drop_id']}",
        )])
    if drop.get("status") in ("draft", "brak"):
        kb_rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"dropdelete:{drop['drop_id']}")])
    kb_rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="drops")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")])

    await message.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


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

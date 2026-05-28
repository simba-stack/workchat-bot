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
from fsm_persistent import AsyncPersistentFSMStorage
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton as _OrigInlineKeyboardButton,
)


# ════════════════════════════════════════════════════════════════
# ЦВЕТНЫЕ КНОПКИ (Bot API 9.4+, aiogram 3.26+)
# Враппер вокруг InlineKeyboardButton который автоматически подбирает
# style на основе текста кнопки. Все 120+ существующих вызовов
# InlineKeyboardButton(...) в этом файле получают цвет БЕЗ изменений
# в самих вызовах — мы просто перехватываем класс.
# ════════════════════════════════════════════════════════════════
def _crm_btn_style(text: str) -> str:
    """Возвращает 'primary'|'success'|'danger' по тексту кнопки."""
    if not text:
        return "primary"
    t = text.lower()
    # DANGER (красный) — отказ, блок, отмена, удаление, брак
    DANGER_MARKERS = (
        "блок", "брак", "отказ", "отмен", "удал", "стоп", "запрет",
        "❌", "🚫", "🛑", "⛔", "💀", "🚷", "🔥", "❗",
    )
    if any(m in t for m in DANGER_MARKERS):
        return "danger"
    # SUCCESS (зелёный) — оплачено, одобрено, отработано, готово, выплата
    SUCCESS_MARKERS = (
        "одобр", "подтвер", "оплач", "оплат", "успех", "отработ", "готов",
        "выплат", "отпуст", "приня", "пополн", "зачисл", "завершен",
        "✅", "✔️", "🟢", "💰", "💸", "💎", "🎉", "💚", "🤝",
    )
    if any(m in t for m in SUCCESS_MARKERS):
        return "success"
    # Все остальные — синий
    return "primary"


def InlineKeyboardButton(*args, **kwargs):  # noqa: N802 (имитируем оригинальное имя)
    """Враппер: если style не задан явно — подбирает по тексту.

    Старые клиенты Telegram (до Bot API 9.4) просто игнорируют поле style
    и отображают серую кнопку как раньше — graceful degradation.
    """
    text = kwargs.get("text") or (args[0] if args else "")
    if "style" not in kwargs:
        kwargs["style"] = _crm_btn_style(str(text))
    try:
        return _OrigInlineKeyboardButton(*args, **kwargs)
    except Exception:
        # Если aiogram-схема не приняла style (старая версия) — пробуем без него
        kwargs.pop("style", None)
        return _OrigInlineKeyboardButton(*args, **kwargs)

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


# === Track-aware routing (CRM vs Credit) ===
# Когда drop/lk относится к credit-стэку (drop_id начинается с "cdrp"),
# сообщения должны идти в КРЕДИТ Доступы/Пароли, а не в общие CRM-чаты.

def _extract_drop_id(obj) -> str:
    """Универсальный extractor drop_id из drop / lk / drop_id-строки.
    Учитывает поля credit_drop_id и outsource_drop_id (унифицирует разные стэки)."""
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    return (
        obj.get("drop_id")
        or obj.get("credit_drop_id")
        or obj.get("outsource_drop_id")
        or ""
    )


def get_admin_chat_id_for(obj) -> int:
    """Возвращает админ-чат с учётом track:
    - credit (cdrp*) → CREDIT_ACCESS_CHAT_ID
    - иначе (crm/outsource) → старая логика (CRM Доступы)
    """
    drop_id = _extract_drop_id(obj)
    if str(drop_id).startswith("cdrp"):
        return config.CREDIT_ACCESS_CHAT_ID
    return get_admin_chat_id()


def get_password_chat_id_for(obj) -> int:
    """Аналогично для password-чата."""
    drop_id = _extract_drop_id(obj)
    if str(drop_id).startswith("cdrp"):
        return config.CREDIT_PASSWORD_CHAT_ID
    return get_password_chat_id()


async def get_admin_chat_resolved_for(bot, obj) -> Optional[int]:
    """Track-aware get_admin_chat_resolved. КРИТИЧНО: для credit-track
    НЕ вызываем register_crm_chat (иначе credit-чат попадает в CRM-namespace
    и find_crm_admin_chat() начнёт возвращать credit-ID для CRM-операций)."""
    drop_id = _extract_drop_id(obj)
    is_credit = str(drop_id).startswith("cdrp")
    cid = get_admin_chat_id_for(obj)
    if not cid:
        return None
    resolved = await _resolve_chat_id_variants(bot, cid)
    if resolved and resolved != cid and not is_credit:
        try:
            await crm_storage.register_crm_chat(
                chat_id=resolved, owner_id="_admin",
                is_admin=True, is_password=False,
            )
        except Exception:
            pass
    return resolved


async def get_password_chat_resolved_for(bot, obj) -> Optional[int]:
    """То же для password-чата (без CRM-загрязнения для credit-track)."""
    drop_id = _extract_drop_id(obj)
    is_credit = str(drop_id).startswith("cdrp")
    cid = get_password_chat_id_for(obj)
    if not cid:
        return None
    resolved = await _resolve_chat_id_variants(bot, cid)
    if resolved and resolved != cid and not is_credit:
        try:
            await crm_storage.register_crm_chat(
                chat_id=resolved, owner_id="_password",
                is_admin=False, is_password=True,
            )
        except Exception:
            pass
    return resolved


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
    # Пошаговый ввод данных ЛК (по точечным вопросам — как в PRIDE PASSWORD).
    # После каждого шага и вопрос, и ответ user'а удаляются из чата
    # (видны только в дашборде / в карточке ЛК).
    waiting_login = State()
    waiting_password = State()
    waiting_phone = State()
    waiting_code_word = State()
    waiting_mail = State()
    # Legacy single-shot ввод — оставляем чтобы не сломать обратную
    # совместимость с возможными старыми FSM-сессиями.
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


def _lk_slot_tag(lk: dict, drop: dict) -> str:
    """Возвращает «#N/total» где N = порядок ЛК в анкете. Пустая строка если нет данных.
    Универсально работает и для crm_drops, и для credit_drops."""
    try:
        siblings = (drop or {}).get("lk_card_ids") or []
        total = len(siblings)
        droplk_id = lk.get("droplk_id") or ""
        if droplk_id and droplk_id in siblings:
            n = siblings.index(droplk_id) + 1
            return f"#{n}/{total}"
    except Exception:
        pass
    return ""


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


# ─── +партнер @username (от userbot/PRIDE ASSISTANT) ─────────────
# Юзербот шлёт эту команду в work-чат клиента сразу после добавления CRM-бота.
# Мы регистрируем @username как CRM-owner и привязываем к этой группе как work_chat.

import re as _re_partner
_PARTNER_RE = _re_partner.compile(r"^\s*\+\s*партн[её]р\s+@?(\w+)\s*$", _re_partner.IGNORECASE)


@router.message(F.text.regexp(r"^\s*\+\s*партн[её]р").as_("matched"))
async def cmd_add_partner_command(message: Message):
    """Регистрирует партнёра по username и привязывает чат как work_chat.
    Идемпотентно — повторный вызов просто обновит привязку."""
    text = (message.text or "").strip()
    m = _PARTNER_RE.match(text)
    if not m:
        return
    username = m.group(1).strip().lstrip("@")
    if not username:
        return
    chat_id = message.chat.id
    # Пытаемся резолвить user_id участника группы по username
    target_user = None
    try:
        from aiogram.exceptions import TelegramBadRequest
        # Сканируем последние сообщения чата чтобы найти юзера с этим username.
        # Лучше всего — пройти по message.chat.get_administrators() и members,
        # но aiogram не даёт listing участников без spec API. Найдём через
        # storage.find_chat_by_client_username (юзербот уже знает кто в чате).
        from storage import storage as main_storage
        chat_key = main_storage.find_chat_by_client_username(username)
        info = main_storage.get_chat_info(chat_key) if chat_key else None
        if info:
            target_user = {
                "tg_user_id": int(info.get("client_id") or 0),
                "username": username,
                "name": info.get("client_name") or username,
            }
    except Exception as e:
        logger.warning("partner-add lookup fail: %s", e)

    if not target_user or not target_user.get("tg_user_id"):
        await message.reply(
            f"⚠️ Не нашёл @{username} в managed-чатах. "
            "Партнёр должен быть участником рабочей беседы PRIDE.",
        )
        return

    # Создаём/обновляем CRM-owner запись
    owner = crm_storage.find_crm_owner_by_tg(target_user["tg_user_id"])
    if owner:
        await crm_storage.update_crm_owner(
            owner["owner_id"],
            username=target_user["username"],
            name=target_user["name"],
            work_chat_id=chat_id,
        )
        owner_id = owner["owner_id"]
        logger.info("CRM partner re-bound: owner=%s chat=%s @%s",
                    owner_id, chat_id, target_user["username"])
    else:
        owner_id = await crm_storage.add_crm_owner(
            tg_user_id=target_user["tg_user_id"],
            username=target_user["username"],
            name=target_user["name"],
            work_chat_id=chat_id,
        )
        logger.info("CRM partner registered: owner=%s chat=%s @%s",
                    owner_id, chat_id, target_user["username"])

    # КРИТИЧНО: регистрируем chat → owner в crm_chats. Без этой записи
    # хэндлер `/clients` в группе вернёт "❌ Эта группа не закреплена".
    try:
        await crm_storage.register_crm_chat(
            chat_id=chat_id, owner_id=owner_id,
            is_admin=False, is_password=False, is_otr=False,
        )
        logger.info("crm_chats: registered chat=%s → owner=%s", chat_id, owner_id)
    except Exception as e:
        logger.warning("register_crm_chat failed: %s", e)

    await message.reply(
        f"✅ <b>Партнёр @{username} добавлен.</b>\n\n"
        f"@{username}, чтобы оформить ваш счёт — пропишите команду "
        f"<code>/clients</code> прямо в этом чате, нажмите кнопку "
        f"«Добавить клиента» и заполните анкету. Когда всё заполнено — "
        f"«Отдать в работу», и наши операционисты возьмут счёт на перевязку.",
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

    # Баланс кошелька партнёра (TRC20 USDT)
    wallet = crm_storage.get_partner_wallet(owner.get("username") or "")
    wallet_balance = float(wallet.get("balance_usdt") or 0)
    pending_count = len(wallet.get("pending_payouts") or [])

    text = (
        f"👤 <b>Профиль партнёра</b>\n\n"
        f"{status_line}"
        f"<b>Имя:</b> {owner.get('name') or '—'}\n"
        f"<b>Username:</b> @{owner.get('username') or '—'}\n"
        f"<b>ID:</b> <code>{owner['owner_id']}</code>\n"
        f"<b>С нами с:</b> {joined}\n\n"
        f"<b>💰 Кошелёк:</b> <b>{wallet_balance:.2f} USDT</b>"
        + (f" · ⏳ {pending_count} pending" if pending_count else "")
        + "\n\n"
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
        [InlineKeyboardButton(text="💼 Кошелёк TRC20", callback_data="wallet")],
        [InlineKeyboardButton(text="❓ Помощь / FAQ", callback_data="help")],
    ]
    if in_group:
        kb.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")])
    await _send(message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


# ============================================================================
# Кошелёк партнёра (TRC20 USDT)
# Custodial-схема: один наш TRC20-адрес для пополнений + ручной payout
# через админ-чат.
# ============================================================================

class WalletForm(StatesGroup):
    waiting_withdraw_amount = State()
    waiting_withdraw_address = State()
    waiting_deposit_txid = State()  # партнёр пишет TXID после пополнения


def _trc20_deposit_address() -> str:
    """Адрес куда партнёры пополняют (env TRC20_DEPOSIT_ADDRESS)."""
    return (os.getenv("TRC20_DEPOSIT_ADDRESS", "") or "").strip()


def _wallet_admin_chat_id() -> str:
    """Чат куда сыпать запросы на выплаты + сообщения о пополнениях.
    По умолчанию — тот же что CRM_ADMIN_CHAT_ID."""
    env = (os.getenv("WALLET_ADMIN_CHAT_ID", "") or "").strip()
    if env:
        return env
    return (os.getenv("CRM_ADMIN_CHAT_ID", "") or "").strip()


WITHDRAW_MIN_USDT = float(os.getenv("WALLET_WITHDRAW_MIN_USDT", "10") or 10)
# Комиссия за вывод TRC20 (USDT). Вычитается СВЕРХ суммы вывода с баланса
# партнёра (партнёр вводит сколько хочет получить — мы списываем сумму + fee).
WITHDRAW_FEE_USDT = float(os.getenv("WALLET_WITHDRAW_FEE_USDT", "4.5") or 4.5)


def _render_wallet_text(owner: dict, wallet: dict) -> str:
    """Текст для меню кошелька."""
    balance = float(wallet.get("balance_usdt") or 0)
    reserved = sum(float(p.get("amount") or 0) for p in (wallet.get("pending_payouts") or []))
    available = max(0.0, balance - reserved)
    text = (
        f"💼 <b>Кошелёк TRC20</b>\n\n"
        f"<b>Баланс:</b> <b>{balance:.2f} USDT</b>\n"
    )
    if reserved > 0:
        text += f"<b>В заявках на вывод:</b> {reserved:.2f} USDT\n"
        text += f"<b>Доступно к выводу:</b> {available:.2f} USDT\n"
    text += "\n"
    pending = wallet.get("pending_payouts") or []
    if pending:
        text += "⏳ <b>В обработке:</b>\n"
        for p in pending[:5]:
            ts_str = time.strftime("%d.%m %H:%M", time.localtime(p.get("ts") or 0))
            text += (
                f"  • <code>{p.get('payout_id')}</code> · "
                f"{float(p.get('amount') or 0):.2f} USDT · {ts_str}\n"
            )
        text += "\n"
    text += (
        f"<i>Минимум на вывод: {WITHDRAW_MIN_USDT:.0f} USDT.\n"
        f"Комиссия за вывод: <b>{WITHDRAW_FEE_USDT:.2f} USDT</b> (вычитается сверх суммы).\n"
        f"Сеть: TRC20 (Tron).</i>"
    )
    return text


@router.callback_query(F.data == "wallet")
async def cb_wallet(call: CallbackQuery, state: FSMContext):
    """Главное меню кошелька."""
    await state.clear()
    owner = crm_storage.find_crm_owner_by_tg(call.from_user.id)
    if not owner:
        await call.answer("Профиль партнёра не найден", show_alert=True)
        return
    wallet = crm_storage.get_partner_wallet(owner.get("username") or "")
    await call.answer()
    text = _render_wallet_text(owner, wallet)
    kb = [
        [InlineKeyboardButton(text="📥 Пополнить", callback_data="wallet:deposit")],
        [InlineKeyboardButton(text="📤 Вывести", callback_data="wallet:withdraw")],
        [InlineKeyboardButton(text="📜 История", callback_data="wallet:history")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="profile")],
    ]
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except TelegramBadRequest:
        await _send(call.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "wallet:deposit")
async def cb_wallet_deposit(call: CallbackQuery):
    """Показать TRC20-адрес для пополнения."""
    addr = _trc20_deposit_address()
    await call.answer()
    if not addr:
        await call.message.edit_text(
            "⚠️ <b>Адрес пополнения ещё не настроен</b>\n\n"
            "Свяжись с админом — он установит TRC20-адрес в настройках бота.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="wallet"),
            ]]),
        )
        return
    text = (
        f"📥 <b>Пополнение TRC20</b>\n\n"
        f"Отправь USDT на этот адрес (сеть <b>TRC20 / Tron</b>):\n\n"
        f"<code>{addr}</code>\n\n"
        f"⚠️ <i>Только TRC20 (Tron). Другие сети — потеря средств!</i>\n\n"
        f"После отправки — нажми <b>«Я отправил»</b>, пришли TXID транзакции, "
        f"админ подтвердит зачисление в течение 1-2 часов."
    )
    kb = [
        [InlineKeyboardButton(text="✅ Я отправил — пришлю TXID", callback_data="wallet:deposit_txid")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="wallet")],
    ]
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "wallet:deposit_txid")
async def cb_wallet_deposit_txid_start(call: CallbackQuery, state: FSMContext):
    """Партнёр готов прислать TXID."""
    await call.answer()
    await state.set_state(WalletForm.waiting_deposit_txid)
    try:
        await call.message.edit_text(
            "💬 <b>Пришли TXID транзакции</b>\n\n"
            "Это длинная строка вида <code>a8b3f9...</code> — можно скопировать из своего кошелька "
            "(USDT TRC20 → история → нажми на отправленную транзакцию).\n\n"
            "Просто отправь её сообщением — админ получит и подтвердит.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data="wallet"),
            ]]),
        )
    except TelegramBadRequest:
        pass


@router.message(WalletForm.waiting_deposit_txid, F.text & ~F.text.startswith("/"))
async def handle_wallet_deposit_txid(message: Message, state: FSMContext):
    txid = (message.text or "").strip()
    if len(txid) < 20 or len(txid) > 80:
        await message.reply(
            "⚠️ Не похоже на TXID. Перепроверь — обычно длина 60-65 символов hex.",
        )
        return
    owner = crm_storage.find_crm_owner_by_tg(message.from_user.id)
    if not owner:
        await message.reply("Профиль не найден")
        await state.clear()
        return
    await state.clear()
    # Уведомляем админа
    admin_chat = _wallet_admin_chat_id()
    if admin_chat:
        try:
            admin_text = (
                f"💰 <b>Запрос пополнения</b>\n\n"
                f"Партнёр: @{owner.get('username') or '—'} (<code>{owner['owner_id']}</code>)\n"
                f"TXID: <code>{txid}</code>\n\n"
                f"<i>Проверь транзакцию в TronScan, потом подтверди сумму ниже.</i>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Подтвердить — ввести сумму",
                    callback_data=f"wadm:cdep:{owner['owner_id']}:{txid[:50]}",
                )],
                [InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"wadm:rdep:{owner['owner_id']}:{txid[:50]}",
                )],
            ])
            await message.bot.send_message(
                int(admin_chat), admin_text, reply_markup=kb,
            )
        except Exception as e:
            logger.warning("wallet deposit admin notify fail: %s", e)
    await message.reply(
        f"✅ <b>TXID получен</b>\n\n"
        f"<code>{txid}</code>\n\n"
        f"Админ проверит транзакцию и зачислит сумму на твой баланс. "
        f"Обычно занимает 1-2 часа в рабочее время.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ К кошельку", callback_data="wallet"),
        ]]),
    )


@router.callback_query(F.data == "wallet:withdraw")
async def cb_wallet_withdraw_start(call: CallbackQuery, state: FSMContext):
    """FSM вывода — шаг 1: ввод суммы."""
    owner = crm_storage.find_crm_owner_by_tg(call.from_user.id)
    if not owner:
        await call.answer("Профиль не найден", show_alert=True)
        return
    wallet = crm_storage.get_partner_wallet(owner.get("username") or "")
    balance = float(wallet.get("balance_usdt") or 0)
    reserved = sum(float(p.get("amount") or 0) for p in (wallet.get("pending_payouts") or []))
    available = max(0.0, balance - reserved)
    # Максимум который партнёр может УКАЗАТЬ к получению = available - fee
    max_recv = max(0.0, available - WITHDRAW_FEE_USDT)
    if max_recv < WITHDRAW_MIN_USDT:
        await call.answer(
            f"Недостаточно. Минимум {WITHDRAW_MIN_USDT:.0f}+{WITHDRAW_FEE_USDT:.1f}fee, доступно {available:.2f}",
            show_alert=True,
        )
        return
    await call.answer()
    await state.set_state(WalletForm.waiting_withdraw_amount)
    await state.update_data(available=available, max_recv=max_recv)
    try:
        await call.message.edit_text(
            f"📤 <b>Вывод TRC20</b>\n\n"
            f"<b>Доступно с баланса:</b> {available:.2f} USDT\n"
            f"<b>Комиссия за вывод:</b> {WITHDRAW_FEE_USDT:.2f} USDT\n"
            f"<b>Можно получить максимум:</b> {max_recv:.2f} USDT\n"
            f"<b>Минимум на получение:</b> {WITHDRAW_MIN_USDT:.0f} USDT\n\n"
            f"Введи <b>сумму к ПОЛУЧЕНИЮ</b> в USDT (число, например <code>50</code>):\n"
            f"<i>С твоего баланса спишется указанная сумма + {WITHDRAW_FEE_USDT:.2f} комиссии.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data="wallet"),
            ]]),
        )
    except TelegramBadRequest:
        pass


@router.message(WalletForm.waiting_withdraw_amount, F.text & ~F.text.startswith("/"))
async def handle_wallet_withdraw_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    try:
        amt = float(text)
    except ValueError:
        await message.reply("⚠️ Введи число, например <code>50</code>.")
        return
    if amt < WITHDRAW_MIN_USDT:
        await message.reply(f"⚠️ Минимум на получение {WITHDRAW_MIN_USDT:.0f} USDT.")
        return
    data = await state.get_data()
    max_recv = float(data.get("max_recv") or 0)
    if amt > max_recv + 1e-9:
        await message.reply(
            f"⚠️ С учётом комиссии {WITHDRAW_FEE_USDT:.2f} можно получить максимум {max_recv:.2f} USDT."
        )
        return
    total_debit = amt + WITHDRAW_FEE_USDT
    await state.update_data(amount=amt, total_debit=total_debit)
    await state.set_state(WalletForm.waiting_withdraw_address)
    await message.reply(
        f"✅ К получению: <b>{amt:.2f} USDT</b>\n"
        f"Комиссия: <b>{WITHDRAW_FEE_USDT:.2f} USDT</b>\n"
        f"Спишется с баланса: <b>{total_debit:.2f} USDT</b>\n\n"
        f"Теперь пришли свой <b>TRC20-адрес</b> для вывода (начинается с <code>T</code>, "
        f"длина 34 символа).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Отмена", callback_data="wallet"),
        ]]),
    )


@router.message(WalletForm.waiting_withdraw_address, F.text & ~F.text.startswith("/"))
async def handle_wallet_withdraw_address(message: Message, state: FSMContext):
    addr = (message.text or "").strip()
    # Базовая валидация TRC20-адреса: 34 символа, начинается с T
    if len(addr) != 34 or not addr.startswith("T"):
        await message.reply(
            "⚠️ Не похоже на TRC20-адрес. Должен начинаться с <b>T</b> и быть длиной 34 символа.\n"
            "Перепроверь в своём кошельке (USDT TRC20 → получить → копировать адрес).",
        )
        return
    data = await state.get_data()
    amt = float(data.get("amount") or 0)               # партнёру к получению
    total_debit = float(data.get("total_debit") or (amt + WITHDRAW_FEE_USDT))
    owner = crm_storage.find_crm_owner_by_tg(message.from_user.id)
    if not owner:
        await message.reply("Профиль не найден")
        await state.clear()
        return
    uname = owner.get("username") or ""
    # Резервируем С БАЛАНСА total_debit (включая комиссию).
    # Партнёру отправляется amt (без fee). Когда админ confirm — спишется total_debit.
    payout_id = await crm_storage.wallet_create_payout_request(uname, total_debit, addr)
    await state.clear()
    if not payout_id:
        await message.reply(
            "❌ Не удалось создать заявку (возможно недостаточно средств).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ К кошельку", callback_data="wallet"),
            ]]),
        )
        return
    # Уведомляем admin
    admin_chat = _wallet_admin_chat_id()
    if admin_chat:
        try:
            admin_text = (
                f"📤 <b>Запрос на вывод</b>\n\n"
                f"Партнёр: @{uname} (<code>{owner['owner_id']}</code>)\n"
                f"К отправке партнёру: <b>{amt:.2f} USDT</b>\n"
                f"Комиссия: {WITHDRAW_FEE_USDT:.2f} USDT (наша маржа)\n"
                f"Списано с баланса: <b>{total_debit:.2f} USDT</b>\n"
                f"Адрес: <code>{addr}</code>\n"
                f"Заявка: <code>{payout_id}</code>\n\n"
                f"<i>Отправь <b>{amt:.2f} USDT</b> на адрес выше — потом подтверди ниже.</i>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Выплачено — ввести TXID",
                    callback_data=f"wadm:cpay:{payout_id}",
                )],
                [InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"wadm:rpay:{payout_id}",
                )],
            ])
            await message.bot.send_message(
                int(admin_chat), admin_text, reply_markup=kb,
            )
        except Exception as e:
            logger.warning("wallet payout admin notify fail: %s", e)
    await message.reply(
        f"✅ <b>Заявка создана</b>\n\n"
        f"<code>{payout_id}</code>\n"
        f"К получению: <b>{amt:.2f} USDT</b>\n"
        f"Комиссия: {WITHDRAW_FEE_USDT:.2f} USDT\n"
        f"Списано с баланса: <b>{total_debit:.2f} USDT</b>\n"
        f"Адрес: <code>{addr}</code>\n\n"
        f"Админ обработает в течение 1-2 часов в рабочее время.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ К кошельку", callback_data="wallet"),
        ]]),
    )


@router.callback_query(F.data == "wallet:history")
async def cb_wallet_history(call: CallbackQuery):
    """Последние 15 операций кошелька."""
    owner = crm_storage.find_crm_owner_by_tg(call.from_user.id)
    if not owner:
        await call.answer("Профиль не найден", show_alert=True)
        return
    wallet = crm_storage.get_partner_wallet(owner.get("username") or "")
    await call.answer()
    history = (wallet.get("history") or [])[-15:][::-1]
    if not history:
        text = "📜 <b>История</b>\n\n<i>Операций пока нет.</i>"
    else:
        lines = ["📜 <b>История операций</b> (последние 15)\n"]
        for h in history:
            ts = time.strftime("%d.%m %H:%M", time.localtime(h.get("ts") or 0))
            typ = h.get("type") or ""
            amt = float(h.get("amount") or 0)
            icon = {
                "credit": "🟢", "debit": "🔴",
                "payout_request": "⏳", "payout_done": "✅",
                "payout_rejected": "❌",
            }.get(typ, "•")
            reason = h.get("reason") or h.get("payout_id") or ""
            lines.append(f"{icon} {ts} · {typ} <b>{amt:.2f}$</b> {reason}")
        text = "\n".join(lines)
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="wallet"),
        ]]))
    except TelegramBadRequest:
        pass


# ============================================================================
# Admin callbacks (wadm:cpay, wadm:rpay, wadm:cdep, wadm:rdep)
# ============================================================================

class WalletAdminForm(StatesGroup):
    waiting_payout_txid = State()  # admin вводит TXID реальной выплаты
    waiting_deposit_amount = State()  # admin вводит сумму подтверждаемого пополнения


@router.callback_query(F.data.startswith("wadm:cpay:"))
async def cb_wallet_admin_confirm_payout(call: CallbackQuery, state: FSMContext):
    """Admin: подтверждаю выплату — далее введу TXID."""
    payout_id = call.data.split(":", 2)[2]
    await call.answer()
    await state.set_state(WalletAdminForm.waiting_payout_txid)
    await state.update_data(payout_id=payout_id, menu_msg_id=call.message.message_id)
    await call.message.reply(
        f"💬 Пришли <b>TXID</b> выплаты <code>{payout_id}</code> (или «-» если без TXID):",
    )


@router.message(WalletAdminForm.waiting_payout_txid, F.text)
async def handle_wadm_payout_txid(message: Message, state: FSMContext):
    txid = (message.text or "").strip()
    if txid == "-":
        txid = ""
    data = await state.get_data()
    payout_id = data.get("payout_id")
    await state.clear()
    result = await crm_storage.wallet_confirm_payout(payout_id, txid=txid)
    if not result:
        await message.reply(f"❌ Заявка <code>{payout_id}</code> не найдена или баланс не покрывает.")
        return
    uname = result.get("username")
    amt = float(result.get("amount") or 0)
    addr = result.get("address") or ""
    await message.reply(
        f"✅ Выплата подтверждена.\n"
        f"@{uname} · {amt:.2f} USDT · {addr[:8]}…{addr[-6:]}\n"
        f"TXID: <code>{txid or '—'}</code>",
    )
    # Уведомляем партнёра
    try:
        owner = crm_storage.find_crm_owner_by_username(uname)
        if owner and owner.get("tg_user_id"):
            await message.bot.send_message(
                int(owner["tg_user_id"]),
                f"✅ <b>Выплата отправлена</b>\n\n"
                f"<b>{amt:.2f} USDT</b> на адрес <code>{addr}</code>\n"
                f"TXID: <code>{txid or '—'}</code>",
            )
    except Exception as e:
        logger.warning("notify partner about payout fail: %s", e)


@router.callback_query(F.data.startswith("wadm:rpay:"))
async def cb_wallet_admin_reject_payout(call: CallbackQuery):
    """Admin: отклонить выплату."""
    payout_id = call.data.split(":", 2)[2]
    await call.answer()
    result = await crm_storage.wallet_reject_payout(payout_id, reason="rejected by admin")
    if not result:
        await call.message.reply("Заявка не найдена.")
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.message.reply(
        f"❌ Заявка <code>{payout_id}</code> отклонена. "
        f"Баланс @{result.get('username')} не изменён."
    )
    # Уведомляем партнёра
    try:
        owner = crm_storage.find_crm_owner_by_username(result.get("username") or "")
        if owner and owner.get("tg_user_id"):
            await call.bot.send_message(
                int(owner["tg_user_id"]),
                f"❌ <b>Заявка на вывод отклонена</b>\n"
                f"<code>{payout_id}</code> на {float(result.get('amount') or 0):.2f} USDT\n"
                f"Свяжись с админом для подробностей.",
            )
    except Exception as e:
        logger.warning("notify partner reject fail: %s", e)


@router.callback_query(F.data.startswith("wadm:cdep:"))
async def cb_wallet_admin_confirm_deposit(call: CallbackQuery, state: FSMContext):
    """Admin подтверждает пополнение — следующий шаг ввести сумму."""
    parts = call.data.split(":", 3)
    if len(parts) < 4:
        await call.answer("Bad callback", show_alert=True)
        return
    owner_id = parts[2]
    txid = parts[3]
    await call.answer()
    await state.set_state(WalletAdminForm.waiting_deposit_amount)
    await state.update_data(owner_id=owner_id, txid=txid)
    await call.message.reply(
        f"💬 Сколько USDT пришло по TXID <code>{txid}</code>?\n"
        f"(Введи число, например <code>100</code>):",
    )


@router.message(WalletAdminForm.waiting_deposit_amount, F.text)
async def handle_wadm_deposit_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    try:
        amt = float(text)
    except ValueError:
        await message.reply("⚠️ Введи число.")
        return
    if amt <= 0:
        await message.reply("⚠️ Сумма должна быть положительной.")
        return
    data = await state.get_data()
    owner_id = data.get("owner_id")
    txid = data.get("txid") or ""
    await state.clear()
    owner = crm_storage.get_crm_owner(owner_id)
    if not owner:
        await message.reply(f"Партнёр <code>{owner_id}</code> не найден.")
        return
    uname = owner.get("username") or ""
    ok = await crm_storage.wallet_credit(
        uname, amt, reason="manual_deposit", txid=txid,
    )
    if not ok:
        await message.reply("❌ Не удалось зачислить (ошибка storage).")
        return
    wallet = crm_storage.get_partner_wallet(uname)
    new_balance = float(wallet.get("balance_usdt") or 0)
    await message.reply(
        f"✅ Зачислено <b>{amt:.2f} USDT</b> на @{uname}.\n"
        f"Новый баланс: <b>{new_balance:.2f} USDT</b>",
    )
    # Уведомляем партнёра
    try:
        if owner.get("tg_user_id"):
            await message.bot.send_message(
                int(owner["tg_user_id"]),
                f"💰 <b>Пополнение зачислено</b>\n\n"
                f"<b>+{amt:.2f} USDT</b>\n"
                f"TXID: <code>{txid}</code>\n"
                f"Новый баланс: <b>{new_balance:.2f} USDT</b>",
            )
    except Exception as e:
        logger.warning("notify partner deposit fail: %s", e)


@router.callback_query(F.data.startswith("wadm:rdep:"))
async def cb_wallet_admin_reject_deposit(call: CallbackQuery):
    """Admin отклоняет пополнение (например невалидный TXID)."""
    parts = call.data.split(":", 3)
    owner_id = parts[2] if len(parts) > 2 else ""
    txid = parts[3] if len(parts) > 3 else ""
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.message.reply(
        f"❌ Пополнение отклонено (owner <code>{owner_id}</code>, TXID <code>{txid}</code>).",
    )
    try:
        owner = crm_storage.get_crm_owner(owner_id)
        if owner and owner.get("tg_user_id"):
            await call.bot.send_message(
                int(owner["tg_user_id"]),
                f"❌ <b>Пополнение отклонено</b>\n"
                f"TXID <code>{txid}</code> не подтвердился (не найден / неверная сумма / спам). "
                f"Свяжись с админом.",
            )
    except Exception as e:
        logger.warning("notify partner deposit reject fail: %s", e)


@router.callback_query(F.data == "profile")
async def cb_profile_back(call: CallbackQuery):
    """Возврат в профиль из кошелька."""
    owner = crm_storage.find_crm_owner_by_tg(call.from_user.id)
    if not owner:
        await call.answer("Профиль не найден", show_alert=True)
        return
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_profile(call.message, owner)


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


# ─── CREDIT (Кредитование): захват чата по тексту ──────────────
# Триггер: текст содержит "кредит*" + "менеджер @ник". Доступно только владельцам.
# Опционально: "как ПАРОЛИ" / "как ДОСТУПЫ" (по умолчанию ДОСТУПЫ).
# Примеры:
#   "Ассистент возьми этот чат под кредитование - менеджер @ivan"
#   "регистрируй чат для кредита, менеджер @anna, как пароли"
#   "@CRM возьми чат кредитование менеджер @oleg"

import re as _re_credit  # отдельный alias чтобы не конфликтовать


@router.message(F.text.regexp(r"(?i)кредит\w*").as_("matched"))
async def cmd_credit_chat_capture(message: Message, matched=None):
    # Только владельцы могут регистрировать кредит-чаты
    if not is_owner(message.from_user.id):
        return
    text = (message.text or "")
    # Должна быть упомянута роль "менеджер"
    if not _re_credit.search(r"менеджер", text, _re_credit.IGNORECASE):
        return
    # Извлекаем @username (требуется)
    m = _re_credit.search(r"менеджер[\s,:\-—]*@?(\w{3,})", text, _re_credit.IGNORECASE)
    if not m:
        await message.reply(
            "Понял про кредитование, но не нашёл <b>@username менеджера</b>.\n"
            "Пример: <code>Ассистент возьми этот чат под кредитование - менеджер @ivan</code>"
        )
        return
    manager_username = m.group(1).lower()
    # Тип чата: pwd/access
    is_password = bool(_re_credit.search(r"парол", text, _re_credit.IGNORECASE))
    is_access = not is_password
    chat_id = message.chat.id
    chat_title = (message.chat.title or "").strip() or "(без названия)"
    try:
        # 1) Регистрируем чат под кредит
        await crm_storage.register_credit_chat(
            chat_id=chat_id,
            manager_username=manager_username,
            is_access=is_access,
            is_password=is_password,
            registered_by_owner_id=message.from_user.id,
        )
        # 2) Регистрируем менеджера (если впервые)
        await crm_storage.register_credit_manager(username=manager_username)
        logger.info(
            "CREDIT chat registered: chat=%s '%s' manager=@%s type=%s by_owner=%s",
            chat_id, chat_title, manager_username,
            "ПАРОЛИ" if is_password else "ДОСТУПЫ", message.from_user.id,
        )
        kind_emoji = "🔐" if is_password else "📥"
        kind_name = "ПАРОЛИ" if is_password else "ДОСТУПЫ"
        await message.reply(
            f"✅ <b>Чат закреплён за КРЕДИТОВАНИЕМ</b>\n\n"
            f"{kind_emoji} Тип: <b>{kind_name}</b>\n"
            f"👤 Менеджер: <b>@{manager_username}</b>\n"
            f"💬 Чат: <code>{chat_id}</code>\n\n"
            f"Все ЛК/анкеты из этого чата теперь идут в раздел "
            f"<b>System → 💳 КРЕДИТ | {kind_name}</b> в дашборде."
        )
    except Exception as e:
        logger.error("CREDIT chat capture failed: %s", e, exc_info=True)
        await message.reply(f"❌ Ошибка при регистрации: <code>{e}</code>")


# ─── /clients ───────────────────────────────────────────────────

@router.message(Command("clients"))
async def cmd_clients(message: Message):
    # === CREDIT branch: если чат закреплён за кредитованием — другой flow ===
    if message.chat.type != "private" and crm_storage.is_credit_chat(message.chat.id):
        await _safe_delete(message.bot, message.chat.id, message.message_id)
        credit_chat = crm_storage.get_credit_chat(message.chat.id) or {}
        manager = credit_chat.get("manager_username") or ""
        if not manager:
            await ephemeral(
                message,
                "❌ Чат помечен как кредитный, но менеджер не назначен.\n"
                "Выполни заново: «Ассистент возьми этот чат под кредитование - менеджер @ник»"
            )
            return
        await _show_credit_clients(message, manager)
        return
    # === CRM branch (оригинал — поставщики) ===
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


async def _show_credit_clients(message: Message, manager_username: str):
    """Аналог _show_clients для credit-чатов. Показывает credit_drops + кнопку «Новая анкета»."""
    drops = crm_storage.list_credit_drops(manager_username=manager_username)
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
        kb_rows.append([InlineKeyboardButton(text="⚠️ Анкет пока нет", callback_data="noop")])
    # Особый callback для credit-create: cnewdrop:<manager_username>
    kb_rows.append([InlineKeyboardButton(text="➕ Новая анкета (кредит)", callback_data=f"cnewdrop:{manager_username}")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")])
    text = f"💳 <b>Кредитные анкеты юриста @{manager_username}:</b>"
    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    # ВАЖНО: НЕ используем message.reply() — оригинальное /clients было удалено
    # в cmd_clients через _safe_delete, и reply на удалённое падает с ошибкой.
    try:
        await message.bot.send_message(message.chat.id, text, reply_markup=markup)
    except Exception as e:
        logger.error("_show_credit_clients send failed chat=%s: %s", message.chat.id, e)


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
        track="crm",  # маркер CRM-track для handle_fio
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


@router.callback_query(F.data.startswith("cnewdrop:"))
async def cb_credit_newdrop(call: CallbackQuery, state: FSMContext):
    """CREDIT-флоу: создание новой кредитной анкеты юристом.
    Callback format: cnewdrop:<manager_username>
    Проверка прав: вызывающий должен быть админом workchat-bot ИЛИ работником,
    ИЛИ его username совпадает с manager_username из credit_chat."""
    manager_username = (call.data.split(":", 1)[1] or "").lstrip("@").lower()
    if not manager_username:
        await call.answer("manager_username не задан", show_alert=True)
        return
    # Проверяем что чат — credit
    if not crm_storage.is_credit_chat(call.message.chat.id):
        await call.answer("Этот чат не помечен как кредитный", show_alert=True)
        return
    await call.answer()
    await state.set_state(DropForm.waiting_fio)
    await state.update_data(
        manager_username=manager_username,
        work_chat_id=call.message.chat.id,
        menu_msg_id=call.message.message_id,
        track="credit",  # маркер CREDIT-track для handle_fio
    )
    try:
        await call.message.edit_text(
            f"<b>💳 ➕ Новая кредитная анкета</b>\n"
            f"<i>юрист: @{manager_username}</i>\n\n"
            f"Введите <b>ФИО</b> клиента полностью.\n\n"
            f"<i>⚠ Бот реагирует на ваше следующее сообщение.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data="cancel"),
            ]]),
        )
    except TelegramBadRequest:
        await call.message.reply("Введите ФИО клиента (кредит):")


@router.message(DropForm.waiting_fio, Command("cancel"))
@router.message(DropForm.waiting_fio, F.text.in_({"отмена", "Отмена", "ОТМЕНА", "cancel"}))
async def handle_fio_cancel(message: Message, state: FSMContext):
    """Явный выход из режима ожидания ФИО."""
    await state.clear()
    await ephemeral(message, "✅ Отменено. Создание анкеты прервано.")


@router.message(DropForm.waiting_fio, F.text & ~F.text.startswith("/"))
async def handle_fio(message: Message, state: FSMContext):
    data = await state.get_data()
    fio = (message.text or "").strip()
    logger.info("[handle_fio] start chat=%s user=%s fio=%r data_keys=%s", message.chat.id, (message.from_user.id if message.from_user else None), fio[:50], list(data.keys()))
    # === Простая, НЕ строгая валидация ФИО ===
    # Условия (любое нарушение → НЕ ФИО, но FSM не сбрасываем, даём ещё попытку):
    # - длина 5-100 символов
    # - 2-5 слов
    # - каждое слово только из букв (кириллица/латиница), дефисы и апострофы разрешены
    # - нет цифр, не одно слово
    import re as _re
    parts = fio.split()
    word_pat = _re.compile(r"^[А-ЯЁA-Zа-яёa-z][а-яёa-zА-ЯЁA-Z\-']*$")
    is_fio = (
        2 <= len(parts) <= 5
        and 5 <= len(fio) <= 100
        and all(word_pat.match(p) for p in parts)
        and not any(ch.isdigit() for ch in fio)
    )
    logger.info("[handle_fio] validation is_fio=%s parts=%d", is_fio, len(parts))
    if not is_fio:
        # НЕ выходим из FSM. Оставляем юзера в режиме ожидания ФИО,
        # просто говорим что введённое — не ФИО. Кнопка ◀ Отмена выше его выведет.
        await ephemeral(
            message,
            "❌ Это не похоже на ФИО. Нужно 2-5 слов из букв (например "
            "<b>Иванов Иван Иванович</b>).\n\nЧтобы выйти — нажми ◀ <b>Отмена</b> "
            "или напиши /cancel."
        )
        return
    track = data.get("track", "crm")
    logger.info("[handle_fio] track=%s", track)  # 'crm' (default) | 'credit'
    owner_id = data.get("owner_id")
    manager_username = data.get("manager_username")
    work_chat_id = data.get("work_chat_id")
    # Sanity check по track'у
    if track == "credit":
        if not manager_username:
            await message.reply("❌ Сессия истекла (credit), начни заново через /clients")
            await state.clear()
            return
    else:
        if not owner_id:
            await message.reply("❌ Сессия истекла, начни заново через /clients")
            await state.clear()
            return
    # Routing через storage.add_drop_for_chat (выберет add_crm_drop или add_credit_drop)
    logger.info("[handle_fio] calling add_drop_for_chat")
    drop_id = await crm_storage.add_drop_for_chat(
        chat_id=work_chat_id or message.chat.id,
        fio=fio,
        owner_id=owner_id,
        manager_username=manager_username,
        work_chat_id=work_chat_id,
    )
    drop = crm_storage.get_drop_any(drop_id)
    logger.info("[handle_fio] drop_id=%s found=%s", drop_id, bool(drop))
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("menu_msg_id"):
        await _safe_delete(message.bot, message.chat.id, data["menu_msg_id"])
    try:
        await _show_drop(message, drop)
        logger.info("[handle_fio] _show_drop OK")
    except Exception as _e:
        logger.exception("[handle_fio] _show_drop FAILED: %s", _e)
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
    drop = crm_storage.get_drop_any(drop_id)
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
    lks = crm_storage.list_drop_lks_any(drop_id=drop.get("drop_id", ""))
    return {
        "fio": bool(drop.get("fio")),
        "social": bool((drop.get("social") or "").strip()),
        "residence": bool((drop.get("residence") or "").strip()),
        "other_banks": bool((drop.get("other_banks") or "").strip()),
        "scan": bool(drop.get("scan_file_ids")),
        "lks": len(lks) > 0,
    }


def _drop_is_ready_to_send(drop: dict) -> bool:
    """True если можно отдать в работу — все 6 пунктов заполнены.
    Кнопка доступна для draft (первая подача) И для accepted
    (повторная подача — например после добавления нового ЛК)."""
    if drop.get("status") not in ("draft", "accepted"):
        return False
    return all(_check_drop_complete(drop).values())


async def _show_drop(message: Message, drop: dict):
    lks = crm_storage.list_drop_lks_any(drop_id=drop["drop_id"])
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
    drop = crm_storage.get_drop_any(drop_id)
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
    drop = crm_storage.get_drop_any(drop_id)
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
    drop = crm_storage.get_drop_any(drop_id)
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
    await crm_storage.update_drop_any(drop_id, **{field: value})
    drop = crm_storage.get_drop_any(drop_id)
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
    drop = crm_storage.get_drop_any(drop_id)
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
    drop = crm_storage.get_drop_any(drop_id)
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
    drop = crm_storage.get_drop_any(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        await state.clear()
        return
    await crm_storage.update_drop_any(drop_id, scan_file_ids=files)
    drop = crm_storage.get_drop_any(drop_id)
    await call.answer(f"✅ Сохранено {len(files)} фото")
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.clear()
    await _show_drop(call.message, drop)


@router.callback_query(F.data.startswith("showdoc:"))
async def cb_showdoc(call: CallbackQuery):
    """Показать/скрыть документы. Toggle через сохранение msg_ids в state.
    Повторный клик удаляет предыдущие фотки чтобы чат не засорялся."""
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_drop_any(drop_id)
    if not drop or not drop.get("scan_file_ids"):
        await call.answer("Документов нет", show_alert=True)
        return
    chat_id = call.message.chat.id
    bot = call.message.bot
    # Ключ для in-memory кэша: (chat_id, drop_id)
    key = (chat_id, drop_id)
    if not hasattr(cb_showdoc, "_open"):
        cb_showdoc._open = {}
    # Если уже открыто — закрываем (удаляем фотки)
    if key in cb_showdoc._open:
        await call.answer("Документы скрыты")
        for mid in cb_showdoc._open.pop(key, []):
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
        return
    # Иначе — открываем (запоминаем msg_ids чтоб можно было закрыть)
    await call.answer("Документы показаны (нажми ещё раз чтобы скрыть)")
    files = drop["scan_file_ids"]
    posted_ids = []
    try:
        if len(files) == 1:
            m = await bot.send_photo(chat_id, files[0])
            posted_ids.append(m.message_id)
        else:
            from aiogram.types import InputMediaPhoto
            media = [InputMediaPhoto(media=fid) for fid in files[:10]]
            sent = await bot.send_media_group(chat_id, media)
            for s in sent:
                posted_ids.append(s.message_id)
        cb_showdoc._open[key] = posted_ids
    except Exception as e:
        logger.warning("showdoc failed: %s", e)
        await ephemeral(call.message, f"❌ Не удалось показать: {e}")


# ════════════════════════════════════════════════════════════════
# ЭТАП 3 — ЛК банков
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("droplk:"))
async def cb_droplk(call: CallbackQuery):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_drop_any(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    await _show_drop_lks(call.message, drop)


async def _show_drop_lks(message: Message, drop: dict):
    lks = crm_storage.list_drop_lks_any(drop_id=drop["drop_id"])

    lines = [f"🏦 <b>ЛК банков клиента {drop.get('fio')}</b>", ""]
    if not lks:
        lines.append("<i>ЛК пока нет. Добавь первый банк.</i>")
    else:
        for lk in lks.values():
            status_e = {"new": "🆕", "pending": "⏳", "ready": "✅", "done": "🏁"}.get(lk.get("status"), "•")
            _slot = _lk_slot_tag(lk, drop)
            _slot_pfx = (f"<code>{_slot}</code> " if _slot else "")
            lines.append(
                f"{status_e} {_slot_pfx}<b>{lk.get('bank')}</b>\n"
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
    drop = crm_storage.get_drop_any(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    # Берём список банков из нашего pricing
    pricing = crm_storage.state.get("pricing") or {}
    banks = sorted(pricing.keys())
    if not banks:
        # Дефолтный список
        banks = ["АЛЬФА", "ОЗОН", "РАЙФ", "ТОЧКА", "ПСБ", "УРАЛСИБ", "ВТБ", "ЛОКО", "БКС", "ДЕЛО", "УБРИР"]
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
    drop = crm_storage.get_drop_any(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(LKForm.waiting_login)
    await state.update_data(
        drop_id=drop_id, bank=bank, menu_msg_id=call.message.message_id,
        # Промежуточные собранные поля — заполняются по шагам
        _new_login="", _new_password="", _new_number="",
        _code_word="", _new_mail="",
    )
    try:
        await call.message.edit_text(
            f"<b>🏦 Новый ЛК — {bank}</b>\n\n"
            f"<b>Шаг 1/5:</b> Введите <b>логин</b> от ЛК (или «-» если нет):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"droplk:{drop_id}"),
            ]]),
        )
    except TelegramBadRequest:
        pass


# ============================================================================
# Пошаговый ввод данных нового ЛК (5 шагов, как PRIDE PASSWORD).
# После каждого шага: удаляем и вопрос-сообщение бота, и ответ user'а,
# показываем следующий шаг как edit_message_text меню. В финале сохраняем
# собранные данные в droplk.new_login/new_password/new_number/code_word/new_mail.
# ============================================================================
async def _lk_step_progress(message: Message, state: FSMContext, field: str, prompt_next: str, next_state):
    """Обработка одного шага FSM ввода ЛК.
    Использует nested dict pattern (как FillForm) — данные хранятся в
    state["new_lk_data"] чтобы гарантированно persist между шагами FSM.
    """
    text = (message.text or "").strip()
    data = await state.get_data()
    # Nested dict pattern — копируем, мутируем, записываем обратно через update_data
    nlk = dict(data.get("new_lk_data") or {})
    nlk[field] = text
    await state.update_data(new_lk_data=nlk)
    logger.info("[LKForm] step %s=%r (data has %d fields)", field, text, len(nlk))
    # Удалить ответ user'а из чата
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    # Обновить меню-сообщение с следующим вопросом
    menu_msg_id = data.get("menu_msg_id")
    bank = data.get("bank") or ""
    drop_id = data.get("drop_id") or ""
    if menu_msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=menu_msg_id,
                text=f"<b>🏦 Новый ЛК — {bank}</b>\n\n{prompt_next}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Отмена", callback_data=f"droplk:{drop_id}"),
                ]]),
            )
        except TelegramBadRequest:
            pass
    await state.set_state(next_state)


@router.message(LKForm.waiting_login, F.text & ~F.text.startswith("/"))
async def handle_lk_login(message: Message, state: FSMContext):
    await _lk_step_progress(
        message, state, "login",
        "<b>Шаг 2/5:</b> Введите <b>пароль</b> от ЛК (или «-»):",
        LKForm.waiting_password,
    )


@router.message(LKForm.waiting_password, F.text & ~F.text.startswith("/"))
async def handle_lk_password(message: Message, state: FSMContext):
    await _lk_step_progress(
        message, state, "password",
        "<b>Шаг 3/5:</b> Введите <b>номер телефона</b> привязанный к банку (или «-»):",
        LKForm.waiting_phone,
    )


@router.message(LKForm.waiting_phone, F.text & ~F.text.startswith("/"))
async def handle_lk_phone(message: Message, state: FSMContext):
    await _lk_step_progress(
        message, state, "number",
        "<b>Шаг 4/5:</b> Введите <b>кодовое слово</b> (или «-»):",
        LKForm.waiting_code_word,
    )


@router.message(LKForm.waiting_code_word, F.text & ~F.text.startswith("/"))
async def handle_lk_code_word(message: Message, state: FSMContext):
    await _lk_step_progress(
        message, state, "code_word",
        "<b>Шаг 5/5:</b> Введите <b>почту</b> (или «-»):",
        LKForm.waiting_mail,
    )


async def _crm_drop_lk_post_save(message: Message, drop: dict, droplk_id: str, bank: str):
    """Общий post-save flow для новых ЛК:
      - Если drop принят (status=accepted) → reply в ДОСТУПЫ + автопост в ПАРОЛИ
      - Кросс-нотификация в work_chat если private chat
    Вызывается ОБЕИМИ ветками FSM: новой (5-шаговой) и старой (single value).
    """
    try:
        if drop.get("status") == "accepted":
            bot = message.bot
            try:
                admin_chat = await get_admin_chat_resolved_for(bot, drop)
                admin_msg_id = drop.get("admin_msg_id")
                if admin_chat and admin_msg_id:
                    fio = drop.get("fio") or "—"
                    notify_text = (
                        f"🏦 <b>Добавлен новый банк: {bank}</b>\n"
                        f"ФИО: <b>{fio}</b>\n"
                        f"<code>{droplk_id}</code>"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="🔕 Закрыть",
                            callback_data=f"closenewbank:{droplk_id}",
                        ),
                    ]])
                    await bot.send_message(
                        admin_chat, notify_text,
                        reply_to_message_id=admin_msg_id,
                        reply_markup=kb,
                    )
            except Exception as e:
                logger.warning("new-bank reply to ДОСТУПЫ fail: %s", e)
            try:
                pwd_chat = await get_password_chat_resolved_for(bot, drop)
                new_lk = crm_storage.get_drop_lk_any(droplk_id)
                if pwd_chat and new_lk:
                    text_p = _render_password_text(drop, new_lk)
                    msg_p = await bot.send_message(
                        pwd_chat, text_p,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(
                                text="✏️ Заполнить",
                                callback_data=f"filldrop:{droplk_id}",
                            ),
                        ]]),
                    )
                    pwd_str = str(pwd_chat).replace("-100", "").lstrip("-")
                    link_p = f"https://t.me/c/{pwd_str}/{msg_p.message_id}"
                    await crm_storage.update_drop_lk_any(
                        droplk_id,
                        msgid_pass=msg_p.message_id, link_pass=link_p,
                    )
                    logger.info(
                        "[new-bank] password template posted for %s (msg=%s)",
                        droplk_id, msg_p.message_id,
                    )
            except Exception as e:
                logger.warning("new-bank password post fail: %s", e)
    except Exception as e:
        logger.warning("new-bank handler outer fail: %s", e)
    # Кросс-нотификация
    if message.chat.type == "private":
        owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
        await _notify_work_chat(
            message.bot, owner,
            f"🏦 <b>Новый ЛК {bank}</b> у клиента <b>{drop.get('fio')}</b>\n"
            f"<i>(добавлен через ЛС CRM-бота)</i>",
        )


@router.message(LKForm.waiting_mail, F.text & ~F.text.startswith("/"))
async def handle_lk_mail(message: Message, state: FSMContext):
    """Финальный шаг — сохраняем все 5 полей в droplk."""
    text = (message.text or "").strip()
    data = await state.get_data()
    # Финальное поле — почта — добавляем в nested dict
    nlk = dict(data.get("new_lk_data") or {})
    nlk["mail"] = text
    logger.info("[LKForm] final mail=%r, collected: %s", text, list(nlk.keys()))

    drop_id = data.get("drop_id")
    bank = data.get("bank")
    drop = crm_storage.get_drop_any(drop_id) if drop_id else None
    if not drop:
        await message.reply("❌ Сессия истекла")
        await state.clear()
        return

    def _clean(v):
        s = (v or "").strip()
        return "" if s == "-" else s

    # Создаём droplk с пустым value (заполняем поля напрямую через update).
    # ВАЖНО: для credit-drop'а нет owner_id (есть manager_username) — routing-метод
    # сам подхватит правильное поле из drop'а если owner_id не передан.
    is_credit = str(drop_id or "").startswith("cdrp")
    droplk_id = await crm_storage.add_drop_lk_for_drop(
        drop_id=drop_id,
        owner_id=(None if is_credit else drop.get("owner_id")),
        bank=bank, value="",
    )
    if not droplk_id:
        await message.reply("❌ Не удалось создать ЛК (storage routing вернул пусто). Проверь логи.")
        await state.clear()
        return
    saved = await crm_storage.update_drop_lk_any(
        droplk_id,
        new_login=_clean(nlk.get("login")),
        new_password=_clean(nlk.get("password")),
        new_number=_clean(nlk.get("number")),
        code_word=_clean(nlk.get("code_word")),
        new_mail=_clean(nlk.get("mail")),
    )
    logger.info("[LKForm] saved droplk %s (status=%s) data=%s",
                droplk_id, saved, {k: _clean(nlk.get(k)) for k in ("login","password","number","code_word","mail")})

    # Удалить ответ user'а + меню
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("menu_msg_id"):
        await _safe_delete(message.bot, message.chat.id, data["menu_msg_id"])
    await state.clear()

    drop = crm_storage.get_drop_any(drop_id)
    await _send(message, f"✅ ЛК <b>{bank}</b> сохранён.")
    await _show_drop_lks(message, drop)
    # SSE
    _emit_crm_event("lk.added", {
        "droplk_id": droplk_id, "drop_id": drop_id, "bank": bank,
    })
    # Дальше вызовем тот же post-save flow что был в handle_lk_value
    await _crm_drop_lk_post_save(message, drop, droplk_id, bank)


# Legacy handler для старого waiting_value (на случай если кто-то ещё в нём)
@router.message(LKForm.waiting_value, F.text & ~F.text.startswith("/"))
async def handle_lk_value(message: Message, state: FSMContext):
    data = await state.get_data()
    drop_id = data.get("drop_id")
    bank = data.get("bank")
    value = (message.text or "").strip()
    drop = crm_storage.get_drop_any(drop_id) if drop_id else None
    if not drop:
        await message.reply("❌ Сессия истекла")
        await state.clear()
        return
    droplk_id = await crm_storage.add_drop_lk_for_drop(
        drop_id=drop_id, owner_id=drop["owner_id"],
        bank=bank, value=value,
    )
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("menu_msg_id"):
        await _safe_delete(message.bot, message.chat.id, data["menu_msg_id"])
    await state.clear()
    drop = crm_storage.get_drop_any(drop_id)
    await _send(
        message,
        f"✅ ЛК <b>{bank}</b> сохранён.",
    )
    await _show_drop_lks(message, drop)
    # SSE
    _emit_crm_event("lk.added", {
        "droplk_id": droplk_id, "drop_id": drop_id, "bank": bank,
    })

    # ─── Если drop УЖЕ принят (status=accepted) — значит это ДОПОЛНИТЕЛЬНЫЙ
    # банк к существующей анкете. Делаем:
    #  1) Reply на исходную анкету в ДОСТУПАХ (вместо повторного полного поста)
    #  2) Автопост шаблона в ПАРОЛИ для нового ЛК
    try:
        if drop.get("status") == "accepted":
            bot = message.bot
            # 1) Reply на admin_msg_id в ДОСТУПАХ
            try:
                admin_chat = await get_admin_chat_resolved_for(bot, drop)
                admin_msg_id = drop.get("admin_msg_id")
                if admin_chat and admin_msg_id:
                    new_lk = crm_storage.get_drop_lk_any(droplk_id) or {}
                    fio = drop.get("fio") or "—"
                    notify_text = (
                        f"🏦 <b>Добавлен новый банк: {bank}</b>\n"
                        f"ФИО: <b>{fio}</b>\n"
                        f"<code>{droplk_id}</code>"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="🔕 Закрыть",
                            callback_data=f"closenewbank:{droplk_id}",
                        ),
                    ]])
                    await bot.send_message(
                        admin_chat, notify_text,
                        reply_to_message_id=admin_msg_id,
                        reply_markup=kb,
                    )
            except Exception as e:
                logger.warning("new-bank reply to ДОСТУПЫ fail: %s", e)
            # 2) Автопост шаблона в ПАРОЛИ
            try:
                pwd_chat = await get_password_chat_resolved_for(bot, drop)
                new_lk = crm_storage.get_drop_lk_any(droplk_id)
                if pwd_chat and new_lk:
                    text_p = _render_password_text(drop, new_lk)
                    msg_p = await bot.send_message(
                        pwd_chat, text_p,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(
                                text="✏️ Заполнить",
                                callback_data=f"filldrop:{droplk_id}",
                            ),
                        ]]),
                    )
                    pwd_str = str(pwd_chat).replace("-100", "").lstrip("-")
                    link_p = f"https://t.me/c/{pwd_str}/{msg_p.message_id}"
                    await crm_storage.update_drop_lk_any(
                        droplk_id,
                        msgid_pass=msg_p.message_id, link_pass=link_p,
                    )
                    logger.info(
                        "[new-bank] password template posted for %s (msg=%s)",
                        droplk_id, msg_p.message_id,
                    )
            except Exception as e:
                logger.warning("new-bank password post fail: %s", e)
    except Exception as e:
        logger.warning("new-bank handler outer fail: %s", e)
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
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))
    await call.answer()
    status_e = {"new": "🆕", "pending": "⏳", "ready": "✅", "done": "🏁"}.get(lk.get("status"), "•")
    _slot = _lk_slot_tag(lk, drop or {})
    _slot_pfx = (f"<code>{_slot}</code> " if _slot else "")
    text = (
        f"{status_e} {_slot_pfx}<b>{lk.get('bank')}</b>\n"
        f"клиент: <b>{drop and drop.get('fio') or '—'}</b>\n\n"
        f"<b>Данные ЛК:</b>\n"
    )
    # Показываем поля которые заполнены через пошаговый ввод (LKForm).
    # Backward compat: legacy `value` показываем если новых полей нет.
    has_new_fields = any([
        lk.get("new_login"), lk.get("new_password"), lk.get("new_number"),
        lk.get("code_word"), lk.get("new_mail"),
    ])
    if has_new_fields:
        if lk.get("new_login"):
            text += f"<b>Логин:</b> <code>{lk.get('new_login')}</code>\n"
        if lk.get("new_password"):
            text += f"<b>Пароль:</b> <code>{lk.get('new_password')}</code>\n"
        if lk.get("new_number"):
            text += f"<b>Телефон банка:</b> <code>{lk.get('new_number')}</code>\n"
        if lk.get("code_word"):
            text += f"<b>Кодовое слово:</b> <code>{lk.get('code_word')}</code>\n"
        if lk.get("new_mail"):
            text += f"<b>Почта:</b> <code>{lk.get('new_mail')}</code>\n"
    else:
        text += f"<code>{lk.get('value') or '—'}</code>\n"
    # Сделка показывается только если она реально привязана (auto-set от AI)
    if lk.get("deal"):
        text += f"\n<b>Сделка:</b> #{lk.get('deal')}\n"
    # 🔒 БЕЗОПАСНОСТЬ: ded_ip / ded_pass — данные операционистов и сервера.
    # Они НЕ должны попадать в чаты партнёров (включая ЛС CRM-бота и work-чаты).
    # Видны только в группе «PRIDE | Пароли».
    if lk.get("ded_ip"):
        text += "\n<i>🔒 Доступ к серверу заполнен операционистами.</i>\n"
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
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("Уже удалён", show_alert=True)
        return
    drop_id = lk.get("drop_id")
    await crm_storage.delete_drop_lk_any(droplk_id)
    await call.answer("Удалено")
    drop = crm_storage.get_drop_any(drop_id) if drop_id else None
    if drop:
        await _show_drop_lks(call.message, drop)


# ════════════════════════════════════════════════════════════════
# ЭТАП 4 — Отправка в admin-чат + Принять / Отклонить
# ════════════════════════════════════════════════════════════════

async def _render_admin_text(drop: dict) -> str:
    """Текст контрольного сообщения в admin-чате."""
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    lks = crm_storage.list_drop_lks_any(drop_id=drop["drop_id"])
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
    """Кнопки для контрольного сообщения в admin-чате.
    Для accepted-дропа — SMS-flow кнопки прямо под анкетой (без tracker)."""
    status = drop.get("status")
    if status in ("draft", "pending"):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"acceptdrop:{drop['drop_id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"declinedrop:{drop['drop_id']}"),
        ]])
    if status == "accepted":
        # SMS-flow stage-aware кнопки на каждый ЛК
        lks = crm_storage.list_drop_lks_any(drop_id=drop["drop_id"])
        rows = []
        for lk in lks.values():
            bank = lk.get("bank") or "—"
            stage = lk.get("sms_stage") or ""
            label, next_label = _SMS_STAGE_LABELS.get(stage, ("?", None))
            if next_label:
                rows.append([InlineKeyboardButton(
                    text=f"[{bank}] {next_label}",
                    callback_data=f"smsadv:{lk['droplk_id']}",
                )])
            else:
                rows.append([InlineKeyboardButton(
                    text=f"[{bank}] {label}",
                    callback_data=f"smsreset:{lk['droplk_id']}",
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
    drop = crm_storage.get_drop_any(drop_id)
    if not drop:
        await call.answer("Клиент не найден", show_alert=True)
        return
    if drop.get("status") not in ("draft", "accepted"):
        await call.answer("Нельзя отправить в этом статусе", show_alert=True)
        return
    # Чек-лист обязателен
    if not _drop_is_ready_to_send(drop):
        check = _check_drop_complete(drop)
        missing = [k for k, v in check.items() if not v]
        await call.answer(
            "❌ Заполни всё: " + ", ".join(missing),
            show_alert=True,
        )
        return

    is_resubmit = drop.get("status") == "accepted"
    bot = call.message.bot
    admin_chat_id = await get_admin_chat_resolved_for(bot, drop)
    if not admin_chat_id:
        raw = get_admin_chat_id_for(drop)
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

    if is_resubmit:
        await call.answer("⏳ Отправляю обновлённую анкету...")
    else:
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

    # 2) Контрольное сообщение — НОВОЕ всегда, даже для accepted (обновление)
    if not is_resubmit:
        await crm_storage.update_drop_any(drop_id, status="pending", send_ts=time.time())
    drop = crm_storage.get_drop_any(drop_id)
    text = await _render_admin_text(drop)
    if is_resubmit:
        text = "🔄 <b>ОБНОВЛЕНИЕ АНКЕТЫ</b> (добавлены новые данные)\n\n" + text
    try:
        ctrl = await bot.send_message(admin_chat_id, text, reply_markup=_admin_keyboard(drop))
        # Перезаписываем admin_msg_id — теперь актуально новое сообщение
        await crm_storage.update_drop_any(drop_id, admin_msg_id=ctrl.message_id)
    except Exception as e:
        logger.error("dropsend ctrl msg failed: %s", e)
        if not is_resubmit:
            await crm_storage.update_drop_any(drop_id, status="draft")
        await ephemeral(call.message, f"❌ Не удалось отправить в admin-чат: {e}", ttl=15)
        return

    # 3) Апдейт партнёра
    try:
        await call.message.delete()
    except Exception:
        pass
    if is_resubmit:
        await _send(
            call.message,
            f"🔄 <b>Анкета {drop.get('fio')} обновлена и отправлена в админ-чат.</b>\n"
            f"<i>Новые данные доступны менеджерам.</i>",
        )
    else:
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
        drop = crm_storage.get_drop_any(drop_id)
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
    pwd_chat = await get_password_chat_resolved_for(bot, drop)
    if not pwd_chat:
        raw_pwd = get_password_chat_id_for(drop)
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
    await crm_storage.update_drop_any(drop_id, status="accepted", accept_ts=time.time())
    drop = crm_storage.get_drop_any(drop_id)
    lks = crm_storage.list_drop_lks_any(drop_id=drop_id)

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
            await crm_storage.update_drop_lk_any(
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
        await crm_storage.update_drop_any(drop_id, status="pending")
        return

    # ═══════ ЭТАП 7: интеграция с экосистемой PRIDE ═══════
    # 1) Карточки lk_cards в Группе 1 НЕ создаём при accept!
    #    Они создаются АССИСТЕНТОМ / CRM-ботом только после
    #    «✅ Перевязка выполнена» (per-LK, в cb_smsadv stage=perevyaz_received).
    # Это позволяет:
    #    - не плодить «фантомные» карточки до перевязки
    #    - ассистенту уточнить метод оплаты у клиента и записать его сразу в карточку
    #    - партнёру не путаться: карточка появляется когда работа реально завершена

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
        lks = list(crm_storage.list_drop_lks_any(drop_id=drop_id).values())
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
    drop = crm_storage.get_drop_any(drop_id)
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
    _slot = _lk_slot_tag(lk, drop)
    _slot_pfx = (f"<code>{_slot}</code> " if _slot else "")
    return (
        f"🔐 {_slot_pfx}<b>ЛК {lk.get('bank')}</b> · {drop.get('fio') or '—'}\n"
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
        drop = crm_storage.get_drop_any(drop_id)
        if not drop:
            await call.answer("Клиент не найден", show_alert=True)
            return
        if drop.get("status") == "brak":
            await call.answer("Уже отклонён", show_alert=True)
            return
        await crm_storage.update_drop_any(drop_id, status="brak")
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

@router.callback_query(F.data.startswith("closenewbank:"))
async def cb_close_new_bank(call: CallbackQuery):
    """Закрытие уведомления о новом банке (удаление reply-сообщения)."""
    try:
        await call.message.delete()
        await call.answer("🔕 Закрыто")
    except Exception as e:
        logger.warning("close-new-bank delete fail: %s", e)
        try:
            await call.message.edit_text("🔕 Закрыто")
        except Exception:
            pass
        await call.answer()


@router.callback_query(F.data.startswith("filldrop:"))
async def cb_filldrop(call: CallbackQuery, state: FSMContext):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await call.answer()
    await state.set_state(FillForm.waiting_new_login)
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))
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
    await crm_storage.update_drop_lk_any(
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

    lk = crm_storage.get_drop_lk_any(droplk_id)
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))

    # Обновляем сообщение в password-чате (с новыми кнопками)
    bot = message.bot
    pwd_chat = get_password_chat_id_for(drop)
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
    admin_chat = get_admin_chat_id_for(drop)
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
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await call.answer(f"✅ Добавлено в пул {pool_name}")
    # Простая запись в drop's history
    drop_id = _extract_drop_id(lk)
    drop = crm_storage.get_drop_any(drop_id) if drop_id else None
    if drop:
        new_count = int(drop.get("prolit_count") or 0) + 1
        await crm_storage.update_drop_any(drop_id, prolit_count=new_count)
        # Обновим admin message
        admin_chat = get_admin_chat_id_for(drop)
        if admin_chat and drop.get("admin_msg_id"):
            try:
                drop = crm_storage.get_drop_any(drop_id)
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
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await crm_storage.update_drop_lk_any(droplk_id, status="done")
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
        all_lks = crm_storage.list_drop_lks_any(drop_id=drop_id)
        if all_lks and all(l.get("status") == "done" for l in all_lks.values()):
            await crm_storage.update_drop_any(drop_id, status="done", done_ts=time.time())
            drop = crm_storage.get_drop_any(drop_id) or {}
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
                admin_chat_id = await get_admin_chat_resolved_for(call.message.bot, drop)
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
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await call.answer("⚠ Помечено как проблема")
    # Уведомление в admin
    admin_chat = get_admin_chat_id_for(lk)
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

# Stage = lk.sms_stage. (label_anketa, next_button_label).
# Если next_button_label = None — кнопки нет (waiting на клиента).
_SMS_STAGE_LABELS = {
    "":                ("⚪ Старт",                          "Готовность к СМС"),
    "ready_asked":     ("⏳ Запросили готовность у клиента", None),
    "ready_confirmed": ("✅ Клиент готов",                   "Запросить СМС вход"),
    "login_asked":     ("⏳ Ждём код входа от клиента",      None),
    "login_received":  ("✅ Код входа получен",              "Запросить перевяз"),
    "perevyaz_asked":  ("⏳ Ждём код перевязки от клиента",  None),
    "perevyaz_received":("✅ Код перевязки получен",         "🏁 Завершить"),
    "done":            ("🏁 Завершено",                      None),
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
    """Обновляет ОСНОВНУЮ анкету дропа в admin-чате (кнопки SMS под ней).
    Отдельного tracker-сообщения больше нет — всё в анкете."""
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        return None
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))
    if not drop:
        return None
    admin_chat = await get_admin_chat_resolved_for(bot, drop)
    if not admin_chat or not drop.get("admin_msg_id"):
        return None
    try:
        await bot.edit_message_text(
            await _render_admin_text(drop),
            chat_id=admin_chat,
            message_id=drop["admin_msg_id"],
            reply_markup=_admin_keyboard(drop),
            disable_web_page_preview=True,
        )
        return drop["admin_msg_id"]
    except Exception as e:
        logger.debug("admin anketa edit failed: %s", e)
        return None


# LEGACY компат: takecodedrop и takesmscodedrop запускают новый flow
@router.callback_query(F.data.startswith("takecodedrop:"))
async def cb_takecodedrop(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await crm_storage.update_drop_lk_any(droplk_id, sms_stage="")
    await _post_or_update_sms_tracker(call.message.bot, droplk_id)
    await call.answer("📩 SMS-flow начат")


@router.callback_query(F.data.startswith("takesmscodedrop:"))
async def cb_takesmscodedrop(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await crm_storage.update_drop_lk_any(droplk_id, sms_stage="")
    await _post_or_update_sms_tracker(call.message.bot, droplk_id)
    await call.answer("📩 SMS-flow начат")


def _client_lk_anketa(drop: dict, lk: dict) -> str:
    """Анкета ЛК для клиента (банк, ФИО, новый логин/пароль/кодовое — то что
    клиенту нужно знать для входа). Дед-данные НЕ показываем."""
    bank = lk.get("bank") or "—"
    fio = drop.get("fio") if drop else "—"
    lines = [f"ФИО: <b>{fio}</b>", f"Банк: <b>{bank}</b>"]
    if lk.get("new_login"):
        lines.append(f"Логин: <code>{lk.get('new_login')}</code>")
    if lk.get("new_password"):
        lines.append(f"Пароль: <code>{lk.get('new_password')}</code>")
    if lk.get("code_word"):
        lines.append(f"Кодовое слово: <code>{lk.get('code_word')}</code>")
    return "\n".join(lines)


def _resolve_work_chat(drop: dict = None, lk: dict = None, owner: dict = None) -> int:
    """Резолвит актуальный work_chat_id для клиента.

    Приоритет: drop.work_chat_id → lk.work_chat_id → owner.work_chat_id.
    Если drop/lk имеет свежий chat_id (созданный позже owner) — используем его,
    т.к. owner.work_chat_id может быть STALE после миграции группы в супергруппу
    (chat_id меняется при апгрейде, а в storage остался старый).
    Возвращает int или 0.
    """
    for src in (drop, lk, owner):
        if not src:
            continue
        wc = src.get("work_chat_id") if isinstance(src, dict) else None
        if wc:
            try:
                return int(wc)
            except Exception:
                continue
    return 0


def _explain_send_error(exc, owner: dict = None) -> str:
    """Превращает сырое исключение Telegram в понятное сообщение для алерта (<200 символов).

    Главные кейсы:
    - CHAT_RESTRICTED → бота кикнули / отняли права postit messages
    - CHAT_WRITE_FORBIDDEN → у бота нет прав писать (анонимка/topics/админ-онли)
    - PEER_ID_INVALID / chat not found → work_chat_id неверный
    - USER_IS_BLOCKED → клиент заблокировал бота
    - bot is not a member → бот не в чате
    """
    s = str(exc or "").lower()
    chat_id = (owner or {}).get("work_chat_id") or "?"
    username = (owner or {}).get("username") or ""
    suffix = f"\n\nЧат: {chat_id}" + (f" (@{username})" if username else "")
    if "chat_restricted" in s:
        return ("🚫 Чат ограничен.\n\nБот @PrideCONTROLE_bot не может писать в чат клиента. "
                "Сделай его админом ИЛИ убери ограничения «отправка сообщений» в правах группы." + suffix)
    if "chat_write_forbidden" in s or "have no rights to send" in s:
        return ("🚫 Нет прав на отправку.\n\nДобавь бота @PrideCONTROLE_bot в чат клиента как админа "
                "с правом «отправлять сообщения»." + suffix)
    if "peer_id_invalid" in s or "chat not found" in s:
        return ("❓ Чат не найден.\n\nWork_chat_id поставщика битый или чат удалён. "
                "Перепривяжи группу командой «Ассистент возьми этот чат под клиента @ник»." + suffix)
    if "user_is_blocked" in s or "bot was blocked" in s:
        return ("🚷 Клиент заблокировал бота.\n\nПопроси клиента разблокировать @PrideCONTROLE_bot." + suffix)
    if "bot is not a member" in s or "kicked" in s:
        return ("👋 Бот не в чате.\n\nДобавь @PrideCONTROLE_bot в чат клиента (как админа)." + suffix)
    # Fallback
    short = str(exc)[:140]
    return f"❌ Ошибка отправки:\n{short}{suffix}"


@router.callback_query(F.data.startswith("smsadv:"))
async def cb_smsadv(call: CallbackQuery, state: FSMContext):
    """Менеджер прокликивает SMS-flow в админ-чате. Коды клиент сам жмёт
    кнопку и вводит в своём work_chat."""
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))
    owner = crm_storage.get_crm_owner(drop.get("owner_id", "") if drop else "")
    bot = call.message.bot
    stage = lk.get("sms_stage") or ""
    bank = lk.get("bank") or "—"
    anketa = _client_lk_anketa(drop, lk)

    if stage == "":
        # Шаг 1: менеджер запросил готовность → клиенту в чат
        text = (
            "📩 <b>Запрос СМС-кода для перепривязки</b>\n\n"
            f"{anketa}\n\n"
            "<b>Готовы дать код?</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, готов", callback_data=f"cliready:{droplk_id}"),
        ]])
        try:
            target_chat = _resolve_work_chat(drop, lk, owner)
            await bot.send_message(target_chat, text, reply_markup=kb)
            await crm_storage.update_drop_lk_any(droplk_id, sms_stage="ready_asked")
            await call.answer("📩 Запрос отправлен клиенту")
        except Exception as e:
            # Если упало с CHAT_RESTRICTED — кладём в очередь авто-починки
            try:
                err_low = str(e).lower()
                if any(m in err_low for m in ("chat_restricted", "chat_write_forbidden", "chat not found", "kicked")):
                    await crm_storage.add_chat_fix_request(_resolve_work_chat(drop, lk, owner), reason=f"smsadv ready: {str(e)[:80]}")
            except Exception: pass
            await call.answer(_explain_send_error(e, owner), show_alert=True)

    elif stage == "ready_confirmed":
        # Шаг 2: менеджер жмёт «Запросить СМС вход» → клиенту запрос кода
        text = (
            "📩 <b>Предоставьте СМС-код для входа</b>\n\n"
            f"{anketa}\n\n"
            "<b>Введите СМС-код по кнопке ниже</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✏️ Ввести СМС код",
                callback_data=f"cligivecode:{droplk_id}:login",
            ),
        ]])
        try:
            target_chat = _resolve_work_chat(drop, lk, owner)
            await bot.send_message(target_chat, text, reply_markup=kb)
            await crm_storage.update_drop_lk_any(droplk_id, sms_stage="login_asked")
            await call.answer("📩 Запрошен код входа")
        except Exception as e:
            try:
                err_low = str(e).lower()
                if any(m in err_low for m in ("chat_restricted", "chat_write_forbidden", "chat not found", "kicked")):
                    await crm_storage.add_chat_fix_request(_resolve_work_chat(drop, lk, owner), reason=f"smsadv login: {str(e)[:80]}")
            except Exception: pass
            await call.answer(_explain_send_error(e, owner), show_alert=True)

    elif stage == "login_received":
        # Шаг 3: менеджер жмёт «Запросить перевяз» → клиенту запрос кода перевязки
        text = (
            "📩 <b>Предоставьте СМС-код для перевязки</b>\n\n"
            f"{anketa}\n\n"
            "<b>Введите СМС-код по кнопке ниже</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✏️ Ввести СМС код",
                callback_data=f"cligivecode:{droplk_id}:perevyaz",
            ),
        ]])
        try:
            target_chat = _resolve_work_chat(drop, lk, owner)
            await bot.send_message(target_chat, text, reply_markup=kb)
            await crm_storage.update_drop_lk_any(droplk_id, sms_stage="perevyaz_asked")
            await call.answer("📩 Запрошен код перевязки")
        except Exception as e:
            try:
                err_low = str(e).lower()
                if any(m in err_low for m in ("chat_restricted", "chat_write_forbidden", "chat not found", "kicked")):
                    await crm_storage.add_chat_fix_request(_resolve_work_chat(drop, lk, owner), reason=f"smsadv perevyaz: {str(e)[:80]}")
            except Exception: pass
            await call.answer(_explain_send_error(e, owner), show_alert=True)

    elif stage == "perevyaz_received":
        # Шаг 4: финал — карточка ЛК создаётся ТОЛЬКО ТЕПЕРЬ (после перевязки)
        card_id = None
        try:
            card_id = await _create_single_lk_card(drop, lk, owner)
        except Exception as e:
            logger.warning("create lk_card after perevyaz failed: %s", e)
        # Запоминаем связку
        if card_id:
            try:
                existing = list(drop.get("lk_card_ids") or [])
                if card_id not in existing:
                    existing.append(card_id)
                    await crm_storage.update_drop_any(
                        drop["drop_id"], lk_card_ids=existing,
                    )
            except Exception:
                pass
            # Эмитим userbot пост анкеты в Группу 1 ЛК
            try:
                await _queue_anketa_post_via_userbot(drop["drop_id"])
            except Exception:
                pass
        # Уведомление клиенту
        await _send_to_client_chat(
            bot, drop, owner,
            f"✅ Перевязка ЛК <b>{bank}</b> успешно выполнена.",
        )
        # Уведомление в work_chat партнёра — карточка в работе + запрос метода оплаты
        try:
            handoff = (
                f"✅ <b>ЛК {bank}</b> перевязан и в работе.\n"
                f"📋 Карточка: #{card_id or '—'}\n"
                f"💳 Метод оплаты: <b>уточняется у клиента</b>\n\n"
                f"<i>Ассистент уточнит у клиента способ оплаты и пропишет в карточке.</i>"
            )
            await _notify_work_chat(bot, owner, handoff)
        except Exception:
            pass
        await crm_storage.update_drop_lk_any(droplk_id, sms_stage="done")
        await call.answer("🏁 Карточка ЛК создана, перевязка завершена")
    else:
        await call.answer("Ждём ответа клиента...")
    await _post_or_update_sms_tracker(bot, droplk_id)


@router.callback_query(F.data.startswith("cliready:"))
async def cb_client_ready(call: CallbackQuery):
    """Клиент в своём чате нажал «Да, готов»."""
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await crm_storage.update_drop_lk_any(droplk_id, sms_stage="ready_confirmed")
    await call.answer("✅ Спасибо! Ожидайте.")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await call.message.reply("✅ Готовность подтверждена. Ждите запрос кода.")
    except Exception:
        pass
    await _post_or_update_sms_tracker(call.message.bot, droplk_id)


@router.callback_query(F.data.startswith("cligivecode:"))
async def cb_client_givecode(call: CallbackQuery, state: FSMContext):
    """Клиент жмёт «Ввести СМС код»."""
    parts = call.data.split(":")
    if len(parts) < 3:
        await call.answer()
        return
    droplk_id, sms_kind = parts[1], parts[2]
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    await state.set_state(SMSForm.waiting_code)
    await state.update_data(droplk_id=droplk_id, sms_kind=sms_kind)
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await call.message.reply(
            "📩 <b>Пришлите СМС-код следующим сообщением</b>\n"
            "<i>(только цифры)</i>"
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("smsreset:"))
async def cb_smsreset(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    await _sms_reset_flow(call.message.bot, droplk_id)
    await call.answer("🔄 Flow сброшен")


async def _sms_reset_flow(bot, droplk_id: str) -> str:
    """Сбрасывает SMS-флоу до старта. Возвращает текст результата."""
    await crm_storage.update_drop_lk_any(
        droplk_id, sms_stage="",
        sms_login_code="", sms_perevyaz_code="",
    )
    try:
        await _post_or_update_sms_tracker(bot, droplk_id)
    except Exception:
        pass
    return f"🔄 SMS-flow сброшен для {droplk_id}"


async def _sms_advance_flow(bot, droplk_id: str) -> str:
    """Программный аналог нажатия кнопки smsadv: продвигает SMS-флоу
    на следующую стадию. Используется из дашборда (без CallbackQuery)."""
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        return f"⚠️ ЛК {droplk_id} не найден"
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))
    owner = crm_storage.get_crm_owner(drop.get("owner_id", "") if drop else "")
    if not (drop and owner and owner.get("work_chat_id")):
        return f"⚠️ Нет work_chat у владельца ЛК {droplk_id}"
    stage = lk.get("sms_stage") or ""
    bank = lk.get("bank") or "—"
    anketa = _client_lk_anketa(drop, lk)
    try:
        if stage == "":
            text = (
                "📩 <b>Запрос СМС-кода для перепривязки</b>\n\n"
                f"{anketa}\n\n"
                "<b>Готовы дать код?</b>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да, готов", callback_data=f"cliready:{droplk_id}"),
            ]])
            await bot.send_message(owner["work_chat_id"], text, reply_markup=kb)
            await crm_storage.update_drop_lk_any(droplk_id, sms_stage="ready_asked")
            result = "📩 Запрос готовности отправлен клиенту"
        elif stage == "ready_confirmed":
            text = (
                "📩 <b>Предоставьте СМС-код для входа</b>\n\n"
                f"{anketa}\n\n"
                "<b>Введите СМС-код по кнопке ниже</b>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✏️ Ввести СМС код",
                    callback_data=f"cligivecode:{droplk_id}:login",
                ),
            ]])
            await bot.send_message(owner["work_chat_id"], text, reply_markup=kb)
            await crm_storage.update_drop_lk_any(droplk_id, sms_stage="login_asked")
            result = "📩 Запрошен код входа"
        elif stage == "login_received":
            text = (
                "📩 <b>Предоставьте СМС-код для перевязки</b>\n\n"
                f"{anketa}\n\n"
                "<b>Введите СМС-код по кнопке ниже</b>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✏️ Ввести СМС код",
                    callback_data=f"cligivecode:{droplk_id}:perevyaz",
                ),
            ]])
            await bot.send_message(owner["work_chat_id"], text, reply_markup=kb)
            await crm_storage.update_drop_lk_any(droplk_id, sms_stage="perevyaz_asked")
            result = "📩 Запрошен код перевязки"
        elif stage == "perevyaz_received":
            # Финал: создание карточки + завершение
            card_id = None
            try:
                card_id = await _create_single_lk_card(drop, lk, owner)
            except Exception as e:
                logger.warning("dashboard sms_advance: create card failed: %s", e)
            if card_id:
                try:
                    existing = list(drop.get("lk_card_ids") or [])
                    if card_id not in existing:
                        existing.append(card_id)
                        await crm_storage.update_drop_any(
                            drop["drop_id"], lk_card_ids=existing,
                        )
                except Exception:
                    pass
                try:
                    await _queue_anketa_post_via_userbot(drop["drop_id"])
                except Exception:
                    pass
            await _send_to_client_chat(
                bot, drop, owner,
                f"✅ Перевязка ЛК <b>{bank}</b> успешно выполнена.",
            )
            try:
                handoff = (
                    f"✅ <b>ЛК {bank}</b> перевязан и в работе.\n"
                    f"📋 Карточка: #{card_id or '—'}\n"
                    f"💳 Метод оплаты: <b>уточняется у клиента</b>\n\n"
                    f"<i>Ассистент уточнит у клиента способ оплаты и пропишет в карточке.</i>"
                )
                await _notify_work_chat(bot, owner, handoff)
            except Exception:
                pass
            await crm_storage.update_drop_lk_any(droplk_id, sms_stage="done")
            result = f"🏁 Перевязка завершена, карточка #{card_id or '—'}"
        else:
            result = f"⏳ Ждём ответа клиента (stage={stage})"
        try:
            await _post_or_update_sms_tracker(bot, droplk_id)
        except Exception:
            pass
        return result
    except Exception as e:
        logger.exception("sms_advance_flow %s failed", droplk_id)
        return f"⚠️ Ошибка: {e}"


async def _dashboard_command_worker_crm(bot):
    """Фоновая задача: каждые 5 сек опрашивает dashboard_commands и
    обрабатывает SMS-команды, которые требуют CRM-бот. Userbot пропустит
    эти команды (возвращает unknown), и они застрянут — поэтому ЭТОТ
    воркер должен опрашивать первым."""
    import re as _re
    logger.info("CRM dashboard_command_worker started")
    while True:
        try:
            await asyncio.sleep(5)
            try:
                crm_storage.reload_sync()
            except Exception:
                pass
            pending = crm_storage.get_pending_dashboard_commands() if hasattr(
                crm_storage, "get_pending_dashboard_commands"
            ) else []
            for cmd in (pending or []):
                cmd_id = cmd.get("id")
                text = (cmd.get("text") or "").strip()
                # SMS-команды + refresh tracker
                m_adv = _re.match(r"^__sms_advance\s+(\S+)\s*$", text, _re.I)
                m_rst = _re.match(r"^__sms_reset\s+(\S+)\s*$", text, _re.I)
                m_rt  = _re.match(r"^__sms_refresh_tracker\s+(\S+)\s*$", text, _re.I)
                m_pwd = _re.match(r"^__refresh_password_post\s+(\S+)\s*$", text, _re.I)
                if not (m_adv or m_rst or m_rt or m_pwd):
                    continue
                try:
                    if m_adv:
                        result = await _sms_advance_flow(bot, m_adv.group(1))
                    elif m_rst:
                        result = await _sms_reset_flow(bot, m_rst.group(1))
                    elif m_rt:
                        # Перерисуем tracker сообщение в TG-группе ДОСТУПЫ
                        await _post_or_update_sms_tracker(bot, m_rt.group(1))
                        result = f"✅ tracker refreshed for {m_rt.group(1)}"
                    else:
                        # Перерисуем PASSWORD сообщение в TG-группе ПАРОЛИ
                        droplk_id = m_pwd.group(1)
                        lk = crm_storage.get_drop_lk_any(droplk_id)
                        if not lk:
                            result = f"⚠️ lk {droplk_id} not found"
                        else:
                            drop = crm_storage.get_drop_any(_extract_drop_id(lk))
                            pwd_chat = get_password_chat_id_for(drop)
                            if pwd_chat and lk.get("msgid_pass") and drop:
                                try:
                                    await bot.edit_message_text(
                                        _render_password_text(drop, lk),
                                        chat_id=pwd_chat,
                                        message_id=lk["msgid_pass"],
                                        reply_markup=_password_filled_keyboard(droplk_id),
                                        disable_web_page_preview=True,
                                    )
                                    result = f"✅ password post refreshed for {droplk_id}"
                                except Exception as e:
                                    result = f"⚠️ password edit failed: {e}"
                            else:
                                result = f"⚠️ password post not found for {droplk_id}"
                except Exception as e:
                    result = f"⚠️ exception: {e}"
                try:
                    await crm_storage.mark_dashboard_command_done(cmd_id, result)
                except Exception as e:
                    logger.warning(
                        "mark_dashboard_command_done %s failed: %s", cmd_id, e,
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("crm dashboard worker tick failed: %s", e)
            await asyncio.sleep(10)


@router.callback_query(F.data.startswith("givemecode:"))
async def cb_givemecode(call: CallbackQuery, state: FSMContext):
    droplk_id = call.data.split(":", 1)[1]
    lk = crm_storage.get_drop_lk_any(droplk_id)
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
    """Клиент пишет код в своём work_chat — отправляется в админ-чат CRM."""
    data = await state.get_data()
    code = (message.text or "").strip()
    droplk_id = data.get("droplk_id")
    sms_kind = data.get("sms_kind") or "login"
    if not droplk_id:
        await state.clear()
        return
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await state.clear()
        return
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))
    await crm_storage.append_drop_sms_any(droplk_id, code=code)
    # Записываем в нужное поле + переключаем stage
    if sms_kind == "perevyaz":
        await crm_storage.update_drop_lk_any(
            droplk_id, sms_perevyaz_code=code,
            sms_stage="perevyaz_received",
        )
    else:
        await crm_storage.update_drop_lk_any(
            droplk_id, sms_login_code=code,
            sms_stage="login_received",
        )
    await state.clear()
    # Удаляем сообщение клиента с кодом (чтоб не светилось в истории)
    try:
        await message.delete()
    except Exception:
        pass
    # Подтверждение в чат клиента
    try:
        await message.bot.send_message(
            message.chat.id,
            f"✅ Код <b>{code}</b> отправлен на обработку. Ожидайте.",
        )
    except Exception:
        pass
    # Уведомление в админ-чат CRM с кодом + кнопки управления.
    # • ✅ Успех — код подошёл, переходим к следующему этапу (закрывает сообщение).
    # • 🔁 Запросить повтор — операционисту нужен ещё код (второй вход / повторная отправка).
    #   На повтор шлём клиенту просьбу «пришлите ещё один код».
    bank = lk.get("bank") or "—"
    fio = drop.get("fio") if drop else "—"
    kind_label = "перевязки" if sms_kind == "perevyaz" else "входа"
    try:
        admin_chat = await get_admin_chat_resolved(message.bot)
        if admin_chat:
            await message.bot.send_message(
                admin_chat,
                f"📩 <b>СМС-код для {kind_label}</b>\n\n"
                f"ФИО: <b>{fio}</b>\nБанк: <b>{bank}</b>\n\n"
                f"<b>Код:</b> <code>{code}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Успех",
                            callback_data="smsmsgclose",
                        ),
                        InlineKeyboardButton(
                            text="🔁 Запросить повтор",
                            callback_data=f"smsretry:{droplk_id}:{sms_kind}",
                        ),
                    ],
                ]),
            )
    except Exception as e:
        logger.warning("admin sms notify failed: %s", e)
    # Обновляем кнопки в анкете дропа
    await _post_or_update_sms_tracker(message.bot, droplk_id)


@router.callback_query(F.data.startswith("smsretry:"))
async def cb_smsretry(call: CallbackQuery):
    """Запросить у клиента повторный код (нужен 2-й код входа / перевяза).
    Шлёт сообщение в work_chat дропа, состояние lk.sms_stage возвращается на _asked."""
    parts = (call.data or "").split(":")
    if len(parts) < 3:
        await call.answer("Нет данных")
        return
    droplk_id = parts[1]
    sms_kind = parts[2]  # "login" или "perevyaz"
    lk = crm_storage.get_drop_lk_any(droplk_id)
    if not lk:
        await call.answer("ЛК не найден", show_alert=True)
        return
    drop = crm_storage.get_drop_any(_extract_drop_id(lk))
    if not drop:
        await call.answer("Дроп не найден", show_alert=True)
        return
    owner = crm_storage.get_crm_owner(drop.get("owner_id", ""))
    if not owner or not owner.get("work_chat_id"):
        await call.answer("Не нашёл work_chat партнёра", show_alert=True)
        return
    # Возвращаем SMS-флоу на этап «ждём код» — операционист увидит ⏳ ожидания
    new_stage = "perevyaz_asked" if sms_kind == "perevyaz" else "login_asked"
    if sms_kind == "perevyaz":
        await crm_storage.update_drop_lk_any(droplk_id, sms_stage=new_stage, sms_perevyaz_code="")
    else:
        await crm_storage.update_drop_lk_any(droplk_id, sms_stage=new_stage, sms_login_code="")
    # Сообщение клиенту в его work_chat
    bank = lk.get("bank") or "—"
    fio = drop.get("fio") or "—"
    kind_label = "перевязки" if sms_kind == "perevyaz" else "входа"
    try:
        await call.bot.send_message(
            int(owner["work_chat_id"]),
            f"🔁 <b>Нужен ещё один код {kind_label}</b>\n\n"
            f"По ЛК <b>{bank}</b> ({fio}) операционисту требуется повторный СМС-код {kind_label}. "
            f"Пришлите его одним сообщением сюда.",
        )
    except Exception as e:
        logger.warning("smsretry send to work_chat fail: %s", e)
        await call.answer("Не удалось написать клиенту: " + str(e), show_alert=True)
        return
    # Обновить кнопки в анкете дропа (sms_tracker)
    try:
        await _post_or_update_sms_tracker(call.bot, droplk_id)
    except Exception:
        pass
    # Помечаем сообщение в admin-чате как обработанное (меняем кнопки на закрытие)
    try:
        await call.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"🔁 Запрошен повтор кода {kind_label}", callback_data="noop"),
                InlineKeyboardButton(text="❌", callback_data="smsmsgclose"),
            ]]),
        )
    except Exception:
        pass
    await call.answer(f"✅ Запрос повтора отправлен клиенту")


@router.callback_query(F.data == "smsmsgclose")
async def cb_smsmsgclose(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()
    # Обновляем сообщение в admin-чате
    admin_chat = get_admin_chat_id_for(lk)
    if drop and admin_chat and drop.get("admin_msg_id"):
        try:
            drop = crm_storage.get_drop_any(_extract_drop_id(lk))
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
    drop = crm_storage.get_drop_any(drop_id)
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
    drop = crm_storage.get_drop_any(drop_id)
    old_price = int(drop.get("price_usdt") or 0) if drop else 0
    await crm_storage.update_drop_any(drop_id, price_usdt=price)
    await state.clear()
    drop = crm_storage.get_drop_any(drop_id)
    await message.reply(f"✅ Цена обновлена: <b>${price}</b>")
    admin_chat = get_admin_chat_id_for(drop)
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


async def _create_single_lk_card(drop: dict, lk: dict, owner: Optional[dict] = None) -> Optional[str]:
    """Создаёт ОДНУ карточку lk_card для конкретного ЛК (вызывается только
    после успешной перевязки этого ЛК). Возвращает card_id или None.

    Май 2026 (новая логика): метод оплаты ставится автоматически —
    либо из сохранённых client_preferences (если клиент уже работал с нами),
    либо дефолт GUARANTOR_AFTER_WORK (= гарант после отработки). Карточка
    публикуется в TG-группу ЛК сразу же — без ожидания client confirm.
    """
    if not lk:
        return None
    if owner is None:
        owner = crm_storage.get_crm_owner(drop.get("owner_id", "")) or {}
    pricing = crm_storage.state.get("pricing") or {}
    bank = (lk.get("bank") or "").upper()
    price = float(pricing.get(bank, drop.get("price_usdt", 0)) or 0)
    # Авто-выбор метода: сначала смотрим client_preferences по @username
    # поставщика; если пусто — дефолт GUARANTOR_AFTER_WORK.
    supplier_uname = (owner.get("username") or "").lstrip("@").strip()
    default_method = "GUARANTOR_AFTER_WORK"
    try:
        prefs = (crm_storage.state.get("client_preferences") or {}).get(supplier_uname.lower()) or {}
        saved_method = (prefs.get("payment_method") or "").upper()
        if saved_method:
            default_method = saved_method
    except Exception:
        pass
    try:
        card_id = await crm_storage.add_lk_card(
            bank=bank,
            fio=drop.get("fio") or "",
            supplier=owner.get("username") or "",
            price_usdt=price,
            payment_method=default_method,
            status="В_РАБОТЕ",
            work_chat_id=owner.get("work_chat_id") or 0,
            client_username=owner.get("username") or "",
            created_by="crm_bot:after_perevyaz",
            deal_id=lk.get("deal") or "",
        )
        logger.info(
            "post-perevyaz lk_card: drop=%s crm_lk=%s → lk_card=%s",
            drop.get("drop_id"), lk.get("droplk_id"), card_id,
        )
        return card_id
    except Exception as e:
        logger.warning("create single lk_card failed: %s", e)
        return None


async def _create_lk_cards_from_crm_drop(drop: dict) -> list:
    """Создаёт записи в storage.lk_cards для каждого CRM ЛК этого дропа.
    Возвращает list created lk_card_ids.

    Это та же таблица lk_cards что использует userbot/web/группа 1 ЛК PRIDE.
    Связь сохраняется в crm_drops[d].lk_card_ids.

    ⚠️ DEPRECATED: эта функция оставлена для обратной совместимости и для
    миграции уже принятых дропов. В НОВОМ flow карточки создаются
    ПЕР-ЛК через _create_single_lk_card() только после перевязки."""
    lks = crm_storage.list_drop_lks_any(drop_id=drop["drop_id"])
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
    lk = crm_storage.get_drop_lk_any(droplk_id)
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
    await crm_storage.update_drop_lk_any(droplk_id, value=value)
    await state.clear()
    lk = crm_storage.get_drop_lk_any(droplk_id)
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
    await crm_storage.update_drop_lk_any(droplk_id, deal=deal)
    await state.clear()
    lk = crm_storage.get_drop_lk_any(droplk_id)
    await message.reply(f"✅ Сделка ЛК {lk.get('bank')}: <b>{deal or '—'}</b>")


@router.callback_query(F.data.startswith("dropeditfio:"))
async def cb_dropeditfio(call: CallbackQuery, state: FSMContext):
    drop_id = call.data.split(":", 1)[1]
    drop = crm_storage.get_drop_any(drop_id)
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
    await crm_storage.update_drop_any(drop_id, fio=fio)
    await state.clear()
    drop = crm_storage.get_drop_any(drop_id)
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
    lks = crm_storage.list_drop_lks_any()
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
        lks_cnt = len(crm_storage.list_drop_lks_any(drop_id=d["drop_id"]))
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
            "bank":   "Пришли название банка (например: <code>Альфа</code>, <code>Сбер</code>, <code>Озон</code>, <code>ПСБ</code>).",
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
        lks = crm_storage.list_drop_lks_any()
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
    lks = crm_storage.list_drop_lks_any()
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
# PAYOUT BUTTONS — inline-кнопки на карточке ЛК в Группе 1 ЛК
# Постятся reply'ем от CRM-бота когда status=ОТРАБОТАН
# ════════════════════════════════════════════════════════════════

class PayoutFSM(StatesGroup):
    waiting_hash = State()     # ждём TronScan хеш
    waiting_deal_id = State()  # ждём номер сделки
    waiting_amount = State()   # ждём сумму пополнения


def _payout_buttons_keyboard(card: dict) -> Optional[InlineKeyboardMarkup]:
    """Возвращает клавиатуру для карточки в зависимости от метода оплаты."""
    method = (card.get("payment_method") or "").upper()
    card_id = card.get("card_id") or ""
    if not card_id:
        return None
    rows = []
    if method == "USDT_TRC20":
        rows.append([InlineKeyboardButton(
            text="💸 Ввести TronScan хеш",
            callback_data=f"po_usdt:{card_id}",
        )])
    elif method in ("GUARANTOR_AFTER_WORK", "GUARANTOR_AFTER"):
        # Сначала номер сделки от клиента, потом сумма пополнения
        if not (card.get("deal_id") or "").strip():
            rows.append([InlineKeyboardButton(
                text="📝 Номер сделки от клиента",
                callback_data=f"po_deal:{card_id}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                text="🤝 Сделка пополнена (сумма)",
                callback_data=f"po_fund:{card_id}",
            )])
            rows.append([InlineKeyboardButton(
                text="✅ Отпустить сделку",
                callback_data=f"po_release:{card_id}",
            )])
    elif method == "GUARANTOR_BEFORE":
        rows.append([InlineKeyboardButton(
            text="✅ Отпустить сделку",
            callback_data=f"po_release:{card_id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _post_payout_buttons(bot, card: dict) -> Optional[int]:
    """Постит сообщение с кнопками выплаты в lk_group_id reply'ем на анкету.
    Возвращает msg_id или None."""
    lk_group = crm_storage.get_lk_group_id()
    if not lk_group:
        return None
    card_id = card.get("card_id") or ""
    if not card_id:
        return None
    kb = _payout_buttons_keyboard(card)
    if not kb:
        return None
    bank = card.get("bank") or "—"
    fio = card.get("fio") or "—"
    method = (card.get("payment_method") or "").upper()
    price = card.get("price_usdt") or 0
    method_label = {
        "USDT_TRC20": "USDT TRC20",
        "GUARANTOR_AFTER_WORK": "гарант после отработки",
        "GUARANTOR_AFTER": "гарант после отработки",
        "GUARANTOR_BEFORE": "гарант (пополнено)",
    }.get(method, method)
    text = (
        f"💰 <b>Готово к выплате</b> · #{card_id}\n"
        f"<b>{bank}</b> · {fio}\n"
        f"Сумма: <b>{price} USDT</b>\n"
        f"Метод: {method_label}"
    )
    reply_to = card.get("lk_group_msg_id") or None
    try:
        sent = await bot.send_message(
            lk_group, text, reply_markup=kb,
            reply_to_message_id=reply_to,
            disable_notification=True,
        )
        return sent.message_id
    except Exception as e:
        err = str(e).lower()
        # При перманентных ошибках (нет чата / нет прав / бот не в группе) —
        # помечаем карточку чтобы цикл не ретраил каждую минуту. Это специальное
        # значение -1 в payout_buttons_msg_id (loop пропускает любое truthy).
        permanent = any(m in err for m in (
            "chat not found", "chat_restricted", "chat_write_forbidden",
            "peer_id_invalid", "user_is_blocked", "bot is not a member",
            "kicked", "have no rights",
        ))
        if permanent:
            try:
                await crm_storage.update_lk_card(
                    card_id, payout_buttons_msg_id=-1,
                    payout_buttons_error=str(e)[:200],
                )
                logger.warning(
                    "post_payout_buttons #%s — перманентная ошибка, помечено -1: %s",
                    card_id, e,
                )
            except Exception:
                logger.warning("post_payout_buttons #%s failed (mark error): %s", card_id, e)
        else:
            logger.warning("post_payout_buttons #%s failed: %s", card_id, e)
        return None


async def _payout_buttons_loop(bot):
    """Раз в минуту проверяет lk_cards и постит кнопки выплат на каждый ЛК
    в статусе ОТРАБОТАН у которого ещё нет payout_buttons_msg_id."""
    await asyncio.sleep(30)
    while True:
        try:
            cards = crm_storage.list_lk_cards(status="ОТРАБОТАН") or {}
            for cid, c in cards.items():
                if c.get("payout_buttons_msg_id"):
                    continue
                if not (c.get("payment_method") or ""):
                    continue
                msg_id = await _post_payout_buttons(bot, c)
                if msg_id:
                    try:
                        await crm_storage.update_lk_card(
                            cid, payout_buttons_msg_id=msg_id,
                        )
                    except Exception:
                        pass
                    logger.info("payout buttons posted card=%s msg=%s", cid, msg_id)
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("payout_buttons_loop tick: %s", e)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return


# Callback handlers
@router.callback_query(F.data.startswith("po_usdt:"))
async def cb_po_usdt(call: CallbackQuery, state: FSMContext):
    card_id = call.data.split(":", 1)[1]
    if not is_owner(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    await state.set_state(PayoutFSM.waiting_hash)
    await state.update_data(card_id=card_id, source_msg_id=call.message.message_id)
    await call.answer()
    await call.message.reply(
        f"💸 <b>Ввести TronScan-хеш для {card_id}</b>\n\n"
        f"Пришли хеш транзакции одним сообщением:"
    )


@router.callback_query(F.data.startswith("po_deal:"))
async def cb_po_deal(call: CallbackQuery, state: FSMContext):
    card_id = call.data.split(":", 1)[1]
    if not is_owner(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    await state.set_state(PayoutFSM.waiting_deal_id)
    await state.update_data(card_id=card_id, source_msg_id=call.message.message_id)
    await call.answer()
    await call.message.reply(
        f"📝 <b>Номер сделки для {card_id}</b>\n\n"
        f"Пришли номер сделки от клиента одним сообщением:"
    )


@router.callback_query(F.data.startswith("po_fund:"))
async def cb_po_fund(call: CallbackQuery, state: FSMContext):
    card_id = call.data.split(":", 1)[1]
    if not is_owner(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    await state.set_state(PayoutFSM.waiting_amount)
    await state.update_data(card_id=card_id, source_msg_id=call.message.message_id)
    await call.answer()
    await call.message.reply(
        f"🤝 <b>Сумма пополнения для {card_id}</b>\n\n"
        f"Пришли сумму USDT (например 400):"
    )


@router.callback_query(F.data.startswith("po_release:"))
async def cb_po_release(call: CallbackQuery):
    card_id = call.data.split(":", 1)[1]
    if not is_owner(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    await call.answer("⏳ Отпускаю...")
    # Через очередь dashboard_commands — юзербот закроет + уведомит клиента
    try:
        await crm_storage.enqueue_dashboard_command(
            f"отпущено #{card_id}",
            source="crm_bot:po_release",
        )
        await call.message.edit_text(
            f"✅ Сделка #{card_id} <b>отпущена</b> — клиент уведомлён.",
            reply_markup=None,
        )
    except Exception as e:
        await call.message.reply(f"❌ Ошибка: {e}")


@router.message(PayoutFSM.waiting_hash, F.text & ~F.text.startswith("/"))
async def fsm_payout_hash(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    data = await state.get_data()
    card_id = data.get("card_id")
    src_msg = data.get("source_msg_id")
    tx_hash = (message.text or "").strip()
    if len(tx_hash) < 16:
        await message.reply("❌ Хеш слишком короткий (минимум 16 символов)")
        return
    await state.clear()
    try:
        await crm_storage.enqueue_dashboard_command(
            f"выплачено #{card_id} {tx_hash}",
            source="crm_bot:po_usdt",
        )
        await message.reply(
            f"✅ Хеш сохранён, клиент уведомлён.\n<code>{tx_hash[:32]}...</code>"
        )
        if src_msg:
            try:
                await message.bot.edit_message_text(
                    f"✅ #{card_id} выплачено · хеш: <code>{tx_hash[:32]}...</code>",
                    chat_id=message.chat.id, message_id=src_msg,
                    reply_markup=None,
                )
            except Exception:
                pass
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(PayoutFSM.waiting_deal_id, F.text & ~F.text.startswith("/"))
async def fsm_payout_deal(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    data = await state.get_data()
    card_id = data.get("card_id")
    deal_id = (message.text or "").strip().lstrip("#")
    if not deal_id:
        await message.reply("❌ Пустой номер")
        return
    await state.clear()
    try:
        await crm_storage.update_lk_card(card_id, deal_id=deal_id)
        # Обновим payout очередь
        match = crm_storage.find_payout_by_card(card_id, queue="fund_release")
        if match:
            q, item = match
            await crm_storage.update_payout(q, item["id"], deal_id=deal_id)
        # Перепостим кнопки — теперь будут «Сделка пополнена» и «Отпустить»
        await message.reply(
            f"✅ Номер сделки #{deal_id} сохранён для {card_id}.\n"
            f"Теперь жми «🤝 Сделка пополнена (сумма)» после факта пополнения."
        )
        card = crm_storage.get_lk_card(card_id)
        if card:
            # Удаляем старое сообщение с кнопками и постим новое
            old_msg = card.get("payout_buttons_msg_id")
            lk_group = crm_storage.get_lk_group_id()
            if old_msg and lk_group:
                try:
                    await message.bot.delete_message(lk_group, old_msg)
                except Exception:
                    pass
            new_msg = await _post_payout_buttons(message.bot, card)
            if new_msg:
                await crm_storage.update_lk_card(card_id, payout_buttons_msg_id=new_msg)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(PayoutFSM.waiting_amount, F.text & ~F.text.startswith("/"))
async def fsm_payout_amount(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    data = await state.get_data()
    card_id = data.get("card_id")
    try:
        amount = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        await message.reply("❌ Нужна сумма числом (например 400 или 400.5)")
        return
    if amount <= 0:
        await message.reply("❌ Сумма должна быть > 0")
        return
    card = crm_storage.get_lk_card(card_id) or {}
    deal_id = (card.get("deal_id") or "").lstrip("#").strip()
    await state.clear()
    if not deal_id:
        await message.reply("❌ У карточки нет deal_id — сначала сохраните номер сделки.")
        return
    try:
        await crm_storage.enqueue_dashboard_command(
            f"сделка #{deal_id} пополнена {amount}",
            source="crm_bot:po_fund",
        )
        await message.reply(
            f"✅ Сделка #{deal_id} помечена как пополненная на {amount} USDT.\n"
            f"Теперь после отработки жми «✅ Отпустить сделку»."
        )
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


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
        lks = list(crm_storage.list_drop_lks_any(drop_id=drop_id).values())
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
            admin_chat_id = await get_admin_chat_resolved_for(bot, drop)
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
            await crm_storage.update_drop_any(drop_id, last_remind_ts=now)
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
        logger.warning("SimbaBySnep load failed: %s", e)
        _simba_emoji_cache = []


def _pick_simba() -> dict:
    if not _simba_emoji_cache:
        return {}
    import random
    return random.choice(_simba_emoji_cache)


def _decor(text: str) -> str:
    if not text:
        return text
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


# === ENTRYPOINT ===

async def run_crm_bot():
    if not CRM_BOT_TOKEN:
        logger.warning("CRM bot token not set")
        return
    logger.info(
        "CRM bot init. Owners=%d Drops=%d LK=%d",
        len(crm_storage.list_crm_owners()),
        len(crm_storage.list_crm_drops()),
        len(crm_storage.list_drop_lks_any()),
    )
    bot = Bot(
        token=CRM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    _fsm_storage = AsyncPersistentFSMStorage(
        os.path.join(
            os.path.dirname(os.environ.get('STORAGE_PATH', '/app/data/state.json')),
            'crm_fsm.json',
        ),
        flush_interval=2.0,
    )
    dp = Dispatcher(storage=_fsm_storage, fsm_strategy=FSMStrategy.CHAT)
    dp.include_router(router)

    try:
        me = await bot.get_me()
        logger.info("CRM bot online: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.error("getMe failed: %s", e)
        await bot.session.close()
        return
    try:
        await _load_simba_emoji_pack(bot)
    except Exception:
        pass
    reminder_task = None
    payout_task = None
    dashboard_worker_task = None
    # Payment-due reminders ОТКЛЮЧЕНЫ по умолчанию. Чтобы включить —
    # выставь env CRM_REMINDERS_ENABLED=1.
    if os.getenv("CRM_REMINDERS_ENABLED", "0").lower() in ("1", "true", "yes", "on"):
        try:
            reminder_task = asyncio.create_task(_payment_reminder_loop(bot))
            logger.info("CRM payment reminders ENABLED")
        except Exception as e:
            logger.warning("reminder start failed: %s", e)
    else:
        logger.info("CRM payment reminders DISABLED (env CRM_REMINDERS_ENABLED unset)")
    try:
        payout_task = asyncio.create_task(_payout_buttons_loop(bot))
        logger.info("CRM payout buttons loop started")
    except Exception as e:
        logger.warning("payout buttons loop start failed: %s", e)
    try:
        dashboard_worker_task = asyncio.create_task(_dashboard_command_worker_crm(bot))
        logger.info("CRM dashboard_command_worker started")
    except Exception as e:
        logger.warning("crm dashboard_worker start failed: %s", e)
    try:
        await dp.start_polling(bot, polling_timeout=30)
    except Exception as e:
        logger.error("polling crashed: %s", e)
    finally:
        if reminder_task and not reminder_task.done():
            reminder_task.cancel()
        if dashboard_worker_task and not dashboard_worker_task.done():
            dashboard_worker_task.cancel()
        try:
            await _fsm_storage.close()
        except Exception:
            pass
        try:
            await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    async def _standalone():
        await crm_storage.load()
        await run_crm_bot()
    asyncio.run(_standalone())


# ════════════════════════════════════════════════════════════════
# IDEAS INBOX — команды управления группой идей
# ════════════════════════════════════════════════════════════════

@router.message(Command("ideas_set"))
async def cmd_ideas_set(message: Message):
    """Назначить текущую группу как ideas-inbox. Только для владельцев."""
    if not is_owner(message.from_user.id):
        return
    if message.chat.type == "private":
        await message.reply(
            "Команду вызывай <b>в группе</b> для идей,\n"
            "или укажи ID вручную: <code>/ideas_setid -1003995887450</code>"
        )
        return
    await crm_storage.set_ideas_chat_id(message.chat.id)
    await message.reply(
        f"✅ Эта группа теперь <b>Ideas Inbox</b> (id <code>{message.chat.id}</code>).\n\n"
        f"Все сообщения отсюда будут сохраняться как идеи/баги.\n"
        f"Команды (пиши в ЛС @PrideCONTROLE_bot):\n"
        f"• <code>/ideas</code> — список нерешённых\n"
        f"• <code>/ideas_all</code> — все включая закрытые\n"
        f"• <code>/ideas_done N</code> — пометить #N как закрытую\n"
        f"• <code>/ideas_clean</code> — удалить все закрытые"
    )


@router.message(Command("ideas_setid"))
async def cmd_ideas_setid(message: Message):
    """Установить ID ideas-чата вручную: /ideas_setid -1003995887450 или 3995887450."""
    if not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "Использование: <code>/ideas_setid -1003995887450</code>\n"
            "(можно с -100, можно без — будет нормализовано)"
        )
        return
    raw = parts[1].strip()
    try:
        n = int(raw)
    except ValueError:
        await message.reply("❌ Нужен числовой ID")
        return
    # Если пришло без минуса (3995887450) — добавляем -100
    if n > 0 and n < 10**12:
        # Это короткая форма (без префикса) — превращаем в -100xxx
        normalized = -1000000000000 - n
    else:
        normalized = n
    await crm_storage.set_ideas_chat_id(normalized)
    await message.reply(
        f"✅ Ideas Inbox установлен: <code>{normalized}</code>\n\n"
        f"⚠️ Убедись что юзербот (под аккаунтом SIMBA) добавлен в эту группу — "
        f"иначе сообщения не будут сохраняться."
    )


@router.message(Command("ideas_status"))
async def cmd_ideas_status(message: Message):
    """Проверить настройку ideas-чата."""
    if not is_owner(message.from_user.id):
        return
    cid = crm_storage.get_ideas_chat_id()
    count_total = len(crm_storage.list_ideas(only_unresolved=False))
    count_open = len(crm_storage.list_ideas(only_unresolved=True))
    if not cid:
        await message.reply("❌ Ideas-чат не настроен. Используй /ideas_set в группе или /ideas_setid &lt;id&gt;.")
        return
    await message.reply(
        f"📋 <b>Ideas Inbox</b>\n\n"
        f"Chat ID: <code>{cid}</code>\n"
        f"Всего идей: <b>{count_total}</b>\n"
        f"Открытых: <b>{count_open}</b>\n\n"
        f"<i>Если идеи не сохраняются — проверь что юзербот в группе.</i>"
    )




@router.message(Command("ideas"))
async def cmd_ideas(message: Message):
    if not is_owner(message.from_user.id):
        return
    items = crm_storage.list_ideas(only_unresolved=True)
    if not items:
        await message.reply("✅ Нет нерешённых идей.")
        return
    items.sort(key=lambda i: -(i.get("ts") or 0))
    lines = [f"💡 <b>Нерешённых идей: {len(items)}</b>\n"]
    for i in items[:30]:
        is_bug = i.get("kind") == "bug"
        kind_icon = "🐛" if is_bug else "💡"
        author = i.get("author") or "?"
        text = (i.get("text") or "")[:200]
        lines.append(f"\n<b>#{i['id']}</b> {kind_icon} <i>{author}</i>\n  {text}")
    if len(items) > 30:
        lines.append(f"\n<i>… и ещё {len(items) - 30}</i>")
    await message.reply("\n".join(lines))


@router.message(Command("ideas_all"))
async def cmd_ideas_all(message: Message):
    if not is_owner(message.from_user.id):
        return
    items = crm_storage.list_ideas(only_unresolved=False)
    if not items:
        await message.reply("Пусто.")
        return
    items.sort(key=lambda i: -(i.get("ts") or 0))
    lines = [f"📋 <b>Всего идей: {len(items)}</b>\n"]
    for i in items[:30]:
        is_bug = i.get("kind") == "bug"
        kind_icon = "🐛" if is_bug else "💡"
        status = "✅" if i.get("resolved") else "⏳"
        text = (i.get("text") or "")[:150]
        author = i.get("author") or "?"
        lines.append(f"\n<b>#{i['id']}</b> {status} {kind_icon} {author}\n  {text}")

    await message.reply("\n".join(lines))


@router.message(Command("ideas_done"))
async def cmd_ideas_done(message: Message):
    if not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Использование: <code>/ideas_done N</code>")
        return
    idea_id = int(parts[1])
    idea_id = int(parts[1])
    ok = await crm_storage.mark_idea_resolved(idea_id, resolved=True)
    if ok:
        await message.reply(f"✅ Идея #{idea_id} закрыта.")
    else:
        await message.reply(f"❌ Идея #{idea_id} не найдена.")


@router.message(Command("ideas_clean"))
async def cmd_ideas_clean(message: Message):
    if not is_owner(message.from_user.id):
        return
    n = await crm_storage.clear_resolved_ideas()
    await message.reply(f"🧹 Удалено закрытых идей: <b>{n}</b>")

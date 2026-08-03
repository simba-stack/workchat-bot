"""
sell_wizard_crm.py — визард продажи ЛК на aiogram @PrideCONTROLE_bot.

Заменяет sell_wizard.py (Telethon), потому что юзербот НЕ рендерит
inline callback buttons. Здесь весь визард работает от bot-аккаунта:

  1) Клиент вошёл в managed_chat → invite-бот делает drop (Волна D).
  2) Асик шлёт welcome + reply_ip (цены).
  3) Асик через dashboard-command __send_sell_button просит CRM-бота
     показать в managed_chat inline-кнопку [🛒 Начать оформление].
  4) Клиент жмёт → callback sw:start → визард ведёт клиента по шагам:
       материал (ИП / Дебет-stub)
       → банк (АЛЬФА / ОЗОН / РАЙФ)
       → verification (инструкция per bank, буфер uploads)
       → payment (Гарант / USDT)
       → open_lk_form (существующая FSM waiting_login)

Все state в storage.client_sell_flow[chat_id] — общий формат с Волной A.

Регистрируется из crm_bot.py:
    import sell_wizard_crm
    sell_wizard_crm.set_dependencies(crm_storage)
    dp.include_router(sell_wizard_crm.router)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

logger = logging.getLogger(__name__)

router = Router(name="sell_wizard_crm")

WIZARD_TIMEOUT_SEC = 60 * 60
ALLOWED_BANKS_IP = ("ALFA", "OZON", "RAIF")
BANK_TITLES = {"ALFA": "АЛЬФА", "OZON": "ОЗОН", "RAIF": "РАЙФ"}
VERIFICATION_SCRIPTED_KEY = {
    "ALFA": "verification_alfa",
    "OZON": "verification_ozon",
    "RAIF": "verification_raif",
}
METHOD_LABELS = {
    "GUARANTOR_BEFORE_WORK": "🤝 Гарант в Continental",
    "USDT_TRC20": "💸 USDT TRC20",
}

_storage = None


def set_dependencies(storage_ref):
    global _storage
    _storage = storage_ref


class SellWizardForm(StatesGroup):
    waiting_upload = State()       # клиент шлёт пруфы в managed_chat (legacy)
    waiting_deal_number = State()  # клиент шлёт номер сделки Continental
    # SIMBA (авг 2026): разделённые state для каждого типа пруфа —
    # клиент нажимает конкретную кнопку и шлёт ТОЛЬКО этот тип, без путаницы.
    waiting_screenshot = State()   # ждём фото/картинку
    waiting_video = State()        # ждём видео (только для АЛЬФА)
    waiting_inn = State()          # ждём ИНН (текст с цифрами)


# ─────────────────────── STATE HELPERS ───────────────────────

def get_flow(chat_id) -> dict:
    return _storage.get_sell_flow(chat_id)


async def set_flow(chat_id, **patch) -> dict:
    return await _storage.set_sell_flow(chat_id, **patch)


async def clear_flow(chat_id) -> None:
    await _storage.clear_sell_flow(chat_id)


def _is_stale(flow: dict) -> bool:
    try:
        return (time.time() - float(flow.get("updated_ts") or 0)) > WIZARD_TIMEOUT_SEC
    except Exception:
        return False


# AUDIT #3 HIGH-5 (авг 2026): текстовые команды отмены wizard.
# Раньше единственный выход — кнопка sw:x или час ожидания (WIZARD_TIMEOUT_SEC).
# Клиент внутри waiting_screenshot писал «отмена» → бот отвечал «❌ Нужен именно
# скриншот». Теперь ловим отмену на любом waiting_* этапе.
_CANCEL_RE = re.compile(
    r"^\s*(?:отмена|отмен(?:и|ить|яю)|стоп|выход|выйти|назад|"
    r"передумал\w*|не\s+буд[уу])\s*[!.?]*\s*$",
    re.IGNORECASE,
)


def _is_cancel_text(text: str) -> bool:
    if not text:
        return False
    return bool(_CANCEL_RE.match(text.strip()))


async def _maybe_wizard_cancel(message: Message, state: FSMContext) -> bool:
    """Если сообщение — команда отмены, сбрасывает FSM+flow и отвечает
    подтверждением. Возвращает True если отмена сработала."""
    if not _is_cancel_text(message.text or ""):
        return False
    try:
        await state.clear()
    except Exception:
        pass
    try:
        await clear_flow(message.chat.id)
    except Exception:
        pass
    try:
        await message.reply(
            "❌ Оформление отменено.\n\n"
            "Когда снова захотите — напишите:\n"
            "<code>Ассистент, хочу сдать РС</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    logger.info("[sell_wizard_crm] cancel by text chat=%s", message.chat.id)
    return True


def _scripted(key: str, default: str = "", **placeholders) -> str:
    data = _storage.get_scripted_text(key) or {}
    text = (data.get("text") or default or "").strip()
    for k, v in placeholders.items():
        text = text.replace("{" + k + "}", str(v if v is not None else ""))
    return text


# ─────────────────────── KEYBOARDS ───────────────────────

def _kb_start_button():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Начать оформление", callback_data="sw:start"),
    ]])


def _kb_material():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 ИП / ООО", callback_data="sw:mat:IP")],
        [InlineKeyboardButton(text="💳 Дебет", callback_data="sw:mat:DEBET")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sw:x")],
    ])


def _kb_bank_ip():
    rows = []
    row = []
    for bank_key in ALLOWED_BANKS_IP:
        title = BANK_TITLES[bank_key]
        price = 0
        try:
            price = _storage.resolve_lk_price(title, 0)
        except Exception:
            pass
        label = f"{title} · {int(price)}$" if price > 0 else title
        row.append(InlineKeyboardButton(text=label, callback_data=f"sw:b:{bank_key}"))
    rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="sw:back:material"),
                 InlineKeyboardButton(text="❌ Отмена", callback_data="sw:x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_debet_stub():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="sw:back:material"),
    ]])


def _kb_verification(uploads: list, bank_key: str = ""):
    """SIMBA (авг 2026): разделённые кнопки для каждого типа пруфа.
    Требование ВИДЕО берётся из verification_config.allow_video для банка —
    (по дефолту True для АЛЬФА, False для остальных).
    """
    types_present = {u.get("type") for u in (uploads or []) if u.get("type")}
    # bank_key = "ALFA"/"OZON"/"RAIF" (латиница!) — не путать с BANK_TITLES ("АЛЬФА"/...)
    bank_title = BANK_TITLES.get((bank_key or "").upper(), (bank_key or "").upper())
    # Читаем конфиг (allow_video) — SIMBA настраивает через админку
    _cfg = _storage.get_verification_config(bank_title) or {}
    # Требуем видео если явно allow_video=True для этого банка (по дефолту — только для АЛЬФА)
    require_video = bool(_cfg.get("allow_video", (bank_key or "").upper() == "ALFA"))

    def _btn(icon, label, present_key, cb):
        prefix = "✅ " if present_key in types_present else ""
        return InlineKeyboardButton(text=f"{prefix}{icon} {label}", callback_data=cb)

    rows = []
    if require_video:
        rows.append([_btn("🎥", "Загрузить видео", "video", "sw:up:video")])
    rows.append([_btn("📷", "Загрузить скриншот", "screenshot", "sw:up:screen")])
    rows.append([_btn("✏️", "Вписать ИНН", "inn", "sw:up:inn")])

    # Обязательные типы для отправки:
    required = {"screenshot", "inn"}
    if require_video:
        required.add("video")
    ready = required.issubset(types_present)
    send_lbl = (
        f"📤 Отправить на проверку ({len(uploads or [])})"
        if ready else f"⏳ Загрузите все пруфы ({len(types_present)}/{len(required)})"
    )
    rows.append([InlineKeyboardButton(
        text=send_lbl,
        callback_data="sw:sendcheck" if ready else "sw:noop",
    )])
    rows.append([
        InlineKeyboardButton(text="◀️ К банкам", callback_data="sw:back:bank"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="sw:x"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_verification_review(client_chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Пропустить на вяз",
                              callback_data=f"sw:vok:{client_chat_id}")],
        [InlineKeyboardButton(text="❌ Отклонить",
                              callback_data=f"sw:vno:{client_chat_id}")],
    ])


def _kb_payment():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤝 Гарант в Continental", callback_data="sw:pay:GUAR")],
        [InlineKeyboardButton(text="💸 USDT TRC20 (после работы)", callback_data="sw:pay:USDT")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sw:x")],
    ])


def _kb_guarantor_created():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создал сделку — ввести номер",
                              callback_data="sw:dealready")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sw:x")],
    ])


def _kb_guarantor_review(client_chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💰 Пополнено", callback_data=f"sw:gok:{client_chat_id}"),
    ]])


# ─────────────────────── RENDERERS ───────────────────────

async def _render_material(bot, chat_id):
    text = _scripted("sell_material_prompt", "Какой материал сдаёте?")
    msg = await bot.send_message(chat_id, text, parse_mode="HTML",
                                 reply_markup=_kb_material())
    await set_flow(chat_id, step="material", wizard_msg_id=int(msg.message_id))


async def _render_bank(bot, chat_id, edit_call: Optional[CallbackQuery] = None):
    text = _scripted("sell_bank_prompt", "Выберите банк:")
    kb = _kb_bank_ip()
    if edit_call:
        try:
            await edit_call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    await set_flow(chat_id, step="bank", wizard_msg_id=int(msg.message_id))


async def _render_debet_stub(edit_call: CallbackQuery):
    text = _scripted("sell_debet_stub",
                     "🚧 По дебету дорабатываются технические моменты. Следите за новостями.")
    try:
        await edit_call.message.edit_text(text, parse_mode="HTML", reply_markup=_kb_debet_stub())
    except Exception:
        pass


async def _render_verification(bot, chat_id, edit_call: Optional[CallbackQuery] = None):
    flow = get_flow(chat_id)
    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    price = float(flow.get("price") or 0)
    uploads = flow.get("uploads") or []

    cfg = _storage.get_verification_config(bank_title) or {}
    text = (cfg.get("text") or "").strip()
    if not text:
        scripted_key = VERIFICATION_SCRIPTED_KEY.get(bank_key)
        if scripted_key:
            text = _scripted(scripted_key, "")
    if not text:
        text = f"🛡 Проверка ЛК {bank_title}\n\nПришлите пруф в чат и жмите «📤 На проверку»."

    header = f"💰 Цена: <b>{int(price)}$</b>\n\n" if price > 0 else ""
    # SIMBA (авг 2026): считаем по типам пруфов, показываем чекбоксы.
    # bank_key = "ALFA"/"OZON"/"RAIF" (латиница). Требование видео берётся
    # из verification_config.allow_video (админка) — дефолт True для ALFA.
    types_present = {u.get("type") for u in (uploads or []) if u.get("type")}
    require_video = bool((cfg or {}).get("allow_video", bank_key == "ALFA"))
    footer_lines = ["", "📋 <b>Что нужно предоставить:</b>"]
    if require_video:
        footer_lines.append(
            f"{'✅' if 'video' in types_present else '⬜'} 🎥 Видео"
        )
    footer_lines.append(
        f"{'✅' if 'screenshot' in types_present else '⬜'} 📷 Скриншот"
    )
    footer_lines.append(
        f"{'✅' if 'inn' in types_present else '⬜'} ✏️ ИНН"
    )
    footer_lines.append(
        "\n👉 Жмите кнопку под нужным типом, затем присылайте одним сообщением. "
        "Когда все ✅ — жмите <b>«📤 Отправить на проверку»</b>."
    )
    footer = "\n".join(footer_lines)

    full = header + text + footer
    kb = _kb_verification(uploads, bank_key)
    if edit_call:
        try:
            await edit_call.message.edit_text(full, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    msg = await bot.send_message(chat_id, full, parse_mode="HTML", reply_markup=kb)
    await set_flow(chat_id, step="verification_upload",
                   wizard_msg_id=int(msg.message_id))


async def _render_payment(bot, chat_id):
    approve = _scripted("verification_approved", "✅ Проверка пройдена!")
    override = _storage.get_payment_text("after_verification")
    if override:
        prompt = override
    else:
        prompt = _scripted("payment_prompt", "Как удобнее получить оплату?")
    text = approve + "\n\n" + prompt
    msg = await bot.send_message(chat_id, text, parse_mode="HTML",
                                 reply_markup=_kb_payment())
    await set_flow(chat_id, step="payment_choice", wizard_msg_id=int(msg.message_id))


async def _render_guarantor_instruction(bot, chat_id, edit_call: Optional[CallbackQuery] = None):
    flow = get_flow(chat_id)
    price = float(flow.get("price") or 0)
    override = _storage.get_payment_text("guarantor")
    if override:
        text = override.replace("{price}", str(int(price)))
    else:
        text = _scripted("payment_guarantor_instruction",
                        "Создайте сделку в @PRIDE_BUHGALTERIA и жмите «Создал сделку».",
                        price=int(price))
    kb = _kb_guarantor_created()
    if edit_call:
        try:
            await edit_call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            await set_flow(chat_id, step="guarantor_wait_deal_ready")
            return
        except Exception:
            pass
    msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    await set_flow(chat_id, step="guarantor_wait_deal_ready",
                   wizard_msg_id=int(msg.message_id))


# ─────────────────────── PUBLIC ENTRY ───────────────────────

# AUDIT #2 MED-8 (авг 2026): idempotency-guard для send_start_button.
# Раньше повторный dashboard-command __send_sell_button дублировал
# закреплённую кнопку старта визарда в чате клиента (клиент видел 2 одинаковых).
# Ключ = chat_id + минутный bucket → в течение 60 сек второй вызов no-op.
_START_BUTTON_SENT_TS: dict = {}  # chat_id -> unix ts

async def send_start_button(bot, chat_id):
    """Асик после reply_ip → dashboard-command __send_sell_button → мы шлём.
    SIMBA (авг 2026): сообщение с кнопкой «🛒 Начать оформление» ПИНИМ,
    чтобы клиент видел его в закрепе — не терялось в чате."""
    import time as _time
    now = _time.time()
    last_ts = _START_BUTTON_SENT_TS.get(int(chat_id), 0)
    if now - last_ts < 60:
        logger.info(
            "[sell_wizard_crm] start-button idempotent skip chat=%s "
            "(last sent %ds ago)", chat_id, int(now - last_ts),
        )
        return
    _sd = _storage.get_scripted_text("sell_start_button") or {}
    prompt = (_sd.get("text") or
              "Когда будете готовы оформить продажу ЛК — жмите кнопку ниже 👇")
    try:
        sent = await bot.send_message(chat_id, prompt, parse_mode="HTML",
                                       reply_markup=_kb_start_button())
        _START_BUTTON_SENT_TS[int(chat_id)] = now
        # Периодически чистим таблицу от старых записей (>1 час)
        if len(_START_BUTTON_SENT_TS) > 200:
            _cutoff = now - 3600
            for _k in list(_START_BUTTON_SENT_TS.keys()):
                if _START_BUTTON_SENT_TS[_k] < _cutoff:
                    _START_BUTTON_SENT_TS.pop(_k, None)
        logger.info("[sell_wizard_crm] start-button sent to chat=%s", chat_id)
        # Пинним для видимости — disable_notification чтобы без пуша
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except Exception as _pe:
            logger.warning("[sell_wizard_crm] pin start-button failed chat=%s: %s", chat_id, _pe)
    except Exception as e:
        logger.warning("[sell_wizard_crm] send start-button failed chat=%s: %s", chat_id, e)


async def start_wizard(bot, chat_id):
    """Открывает первый экран (материал) — вызывается из sw:start callback
    или из dashboard-command __start_sell_wizard (fallback).
    RETRY: до 3 попыток с backoff — CRM-бот мог только-что вступить в чат
    (Telegram может задержать membership) или сработать rate-limit."""
    import asyncio as _asyncio
    await set_flow(chat_id, step="material", material="", bank="", price=0,
                   method="", uploads=[], deal_number="",
                   verification_msg_id=0, guarantor_msg_id=0)
    last_err = None
    for attempt in range(1, 4):
        try:
            await _render_material(bot, chat_id)
            logger.info(
                "[sell_wizard_crm] wizard opened chat=%s attempt=%d", chat_id, attempt,
            )
            return
        except Exception as e:
            last_err = e
            logger.warning(
                "[sell_wizard_crm] wizard render failed chat=%s attempt=%d: %s",
                chat_id, attempt, e,
            )
            # Backoff: 2s, 4s
            if attempt < 3:
                await _asyncio.sleep(attempt * 2)
    # Все 3 попытки провалились — логируем как ошибку. Клиент увидит молчание,
    # но mega-priority в userbot повторит enqueue при следующем «Ассистент, хочу сдать РС».
    logger.error(
        "[sell_wizard_crm] wizard FAILED after 3 attempts chat=%s last_err=%s",
        chat_id, last_err,
    )
    raise last_err if last_err else RuntimeError("wizard render failed")


# ─────────────────────── VERIFICATION FORWARD ───────────────────────

async def _forward_to_verification(bot, chat_id):
    flow = get_flow(chat_id)
    uploads = flow.get("uploads") or []
    if not uploads:
        return False, "нет пруфов"
    vgroup = _storage.get_verification_group_id()
    if not vgroup:
        return False, "verification_group_id не задан"

    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    price = float(flow.get("price") or 0)
    chat_info = _storage.get_chat_info(chat_id) or {}
    client_uname = (chat_info.get("client_username") or "").lstrip("@")
    client_id = chat_info.get("client_id") or 0
    tag = f"@{client_uname}" if client_uname else f"client_id={client_id}"

    header = (
        f"🛡 <b>Проверка ЛК {bank_title}</b>\n\n"
        f"Клиент: {tag}\n"
        f"Chat: <code>{chat_id}</code>\n"
        f"Цена: <b>{int(price)}$</b>\n"
        f"Пруфов: <b>{len(uploads)}</b>"
    )
    try:
        await bot.send_message(vgroup, header, parse_mode="HTML")
    except Exception as e:
        return False, f"header: {e}"

    # Форвардим каждый upload (msg_id хранится в буфере)
    for u in uploads:
        mid = u.get("msg_id")
        if not mid:
            # text-only upload
            cap = u.get("caption") or ""
            if cap:
                try:
                    await bot.send_message(vgroup, f"📝 <i>Клиент:</i> {cap}", parse_mode="HTML")
                except Exception:
                    pass
            continue
        try:
            await bot.forward_message(chat_id=vgroup, from_chat_id=chat_id,
                                      message_id=int(mid))
        except Exception as e:
            logger.warning("[sell_wizard_crm] forward msg=%s failed: %s", mid, e)

    try:
        review_msg = await bot.send_message(vgroup, "👇 Решение по проверке:",
                                             reply_markup=_kb_verification_review(chat_id))
        await set_flow(chat_id, step="verification_pending",
                       verification_msg_id=int(review_msg.message_id))
    except Exception as e:
        return False, f"review kb: {e}"
    return True, ""


async def _forward_deal_to_guarantor(bot, chat_id, deal_number):
    ggroup = _storage.get_guarantor_group_id()
    if not ggroup:
        await bot.send_message(chat_id,
                               f"⚠️ Номер сделки #{deal_number} принят, но группа бухгалтерии не настроена.")
        return
    flow = get_flow(chat_id)
    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    price = float(flow.get("price") or 0)
    chat_info = _storage.get_chat_info(chat_id) or {}
    client_uname = (chat_info.get("client_username") or "").lstrip("@")
    tag = f"@{client_uname}" if client_uname else f"client_id={chat_info.get('client_id')}"

    text = (
        f"🤝 <b>Сделка на пополнение гаранта</b>\n\n"
        f"Клиент: {tag}\n"
        f"Chat: <code>{chat_id}</code>\n"
        f"Банк: <b>{bank_title}</b>\n"
        f"Сумма: <b>{int(price)}$</b>\n"
        f"Номер сделки: <b>#{deal_number}</b>"
    )
    try:
        msg = await bot.send_message(ggroup, text, parse_mode="HTML",
                                     reply_markup=_kb_guarantor_review(chat_id))
        await set_flow(chat_id, step="guarantor_wait_deposit",
                       guarantor_msg_id=int(msg.message_id))
    except Exception as e:
        logger.exception("[sell_wizard_crm] guarantor forward failed: %s", e)
        return

    ack = _scripted("payment_guarantor_deal_received",
                    f"✅ Номер сделки №{deal_number} принят. Ждём пополнения.",
                    deal_number=deal_number)
    try:
        await bot.send_message(chat_id, ack, parse_mode="HTML")
    except Exception:
        pass


# ─────────────────────── LK-FORM TRIGGER ───────────────────────

async def _trigger_lk_form(bot, chat_id):
    """После successful payment path — просим CRM показать заполнение ЛК.
    ДВОЙНАЯ ГАРАНТИЯ:
      1) СИНХРОННО вызываем _open_lk_form_for_client — клиент видит форму
         СРАЗУ без ожидания 5-сек polling.
      2) ЕЩЁ enqueue команду в очередь как fallback (на случай если
         синхронный вызов упал — retry через worker).
    Раньше был только enqueue → клиент ждал 5 сек, а если worker падал —
    молчание навсегда."""
    flow = get_flow(chat_id)
    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    method = flow.get("method") or ""
    chat_info = _storage.get_chat_info(chat_id) or {}
    client_id = chat_info.get("client_id") or 0
    client_uname = chat_info.get("client_username") or ""
    params = {
        "chat": str(int(chat_id)),
        "client": str(int(client_id or 0)),
        "username": client_uname or "",
        "bank": bank_title,
        "price": str(float(flow.get("price") or 0)),
        "method": method,
        "deal": flow.get("deal_number") or "",
    }
    # (1) СИНХРОННЫЙ путь — крайняя надёжность.
    sync_ok = False
    try:
        # Импорт локально, чтобы избежать circular import при загрузке модуля.
        from crm_bot import _open_lk_form_for_client as _openlk
        result = await _openlk(bot, params)
        logger.info("[sell_wizard_crm] LK-form SYNC opened chat=%s → %s", chat_id, result)
        # Даже если result начинается с ⚠️ — считаем что клиенту уже отправлено сообщение
        sync_ok = True
    except Exception as e:
        logger.warning("[sell_wizard_crm] LK-form SYNC failed chat=%s: %s — падаем в fallback", chat_id, e)

    # (2) FALLBACK через очередь — если синхронный вызов упал.
    if not sync_ok:
        cmd = (
            f"__open_lk_form|chat={int(chat_id)}"
            f"|client={int(client_id or 0)}"
            f"|username={client_uname or ''}"
            f"|bank={bank_title}"
            f"|price={float(flow.get('price') or 0)}"
            f"|method={method}"
            f"|deal={flow.get('deal_number') or ''}"
        )
        try:
            await _storage.enqueue_dashboard_command(cmd, source="sell_wizard_crm")
            logger.info("[sell_wizard_crm] LK-form fallback enqueued chat=%s", chat_id)
        except Exception as e:
            logger.warning("[sell_wizard_crm] LK-form fallback enqueue failed: %s", e)
        # Плюс — сообщаем клиенту что что-то пошло не так, оператор подключится
        try:
            await bot.send_message(
                int(chat_id),
                "⏳ Обрабатываю данные, форма ЛК откроется через пару секунд. Если не появится — оператор подключится.",
            )
        except Exception:
            pass
    await set_flow(chat_id, step="done")


# ─────────────────────── CALLBACK HANDLERS ───────────────────────

# AUDIT #3 CRIT-2 (авг 2026): idempotency для sw:start.
# MED-9 закрыл dedup только для dashboard-команд __start_sell_wizard,
# но inline-кнопка sw:start шла напрямую в start_wizard — двойной клик
# показывал 2 экрана «Какой материал сдаёте?». Guard: chat_id + 10s.
_SW_START_PROCESSING: dict = {}  # chat_id -> unix ts

@router.callback_query(F.data == "sw:start")
async def cb_start(call: CallbackQuery, state: FSMContext):
    import time as _time
    _cid = int(call.message.chat.id)
    _now = _time.time()
    _last = _SW_START_PROCESSING.get(_cid, 0)
    if _now - _last < 10:
        await call.answer("Уже открываю визард…", show_alert=False)
        return
    _SW_START_PROCESSING[_cid] = _now
    # Периодическая чистка старых записей
    if len(_SW_START_PROCESSING) > 200:
        _cutoff = _now - 3600
        for _k in list(_SW_START_PROCESSING.keys()):
            if _SW_START_PROCESSING[_k] < _cutoff:
                _SW_START_PROCESSING.pop(_k, None)
    try:
        await start_wizard(call.bot, call.message.chat.id)
    except Exception:
        # если start_wizard упал, снимем guard чтобы клиент мог повторить
        _SW_START_PROCESSING.pop(_cid, None)
        raise
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Начинаем оформление")


@router.callback_query(F.data == "sw:x")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await clear_flow(call.message.chat.id)
    try:
        await call.message.edit_text("❌ Оформление отменено. Напишите «продать» чтобы начать заново.")
    except Exception:
        pass
    await call.answer("Отменено")


# SIMBA (авг 2026): 3 разделённые кнопки загрузки пруфов — каждая жмётся
# отдельно, клиент шлёт ровно этот тип. Нет мешанины «фото+видео+текст».
@router.callback_query(F.data == "sw:up:screen")
async def cb_upload_screenshot(call: CallbackQuery, state: FSMContext):
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True); return
    await state.set_state(SellWizardForm.waiting_screenshot)
    await state.update_data(sw_chat_id=call.message.chat.id)
    await call.answer("📷 Пришлите скриншот одним сообщением", show_alert=False)


@router.callback_query(F.data == "sw:up:video")
async def cb_upload_video(call: CallbackQuery, state: FSMContext):
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True); return
    await state.set_state(SellWizardForm.waiting_video)
    await state.update_data(sw_chat_id=call.message.chat.id)
    await call.answer("🎥 Пришлите видео одним сообщением", show_alert=False)


@router.callback_query(F.data == "sw:up:inn")
async def cb_upload_inn(call: CallbackQuery, state: FSMContext):
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True); return
    await state.set_state(SellWizardForm.waiting_inn)
    await state.update_data(sw_chat_id=call.message.chat.id)
    await call.answer("✏️ Пришлите ИНН цифрами (10 или 12 знаков)", show_alert=False)


@router.callback_query(F.data == "sw:noop")
async def cb_noop(call: CallbackQuery):
    """Заглушка для disabled кнопки «Загрузите все пруфы» — silent toast."""
    await call.answer("Сначала загрузите все обязательные пруфы", show_alert=False)


@router.callback_query(F.data.startswith("sw:mat:"))
async def cb_material(call: CallbackQuery, state: FSMContext):
    mat = call.data.split(":", 2)[2]
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла.", show_alert=True)
        await clear_flow(call.message.chat.id)
        return
    await set_flow(call.message.chat.id, material=mat)
    if mat == "DEBET":
        await set_flow(call.message.chat.id, step="debet_stub")
        await _render_debet_stub(call)
    elif mat == "IP":
        await set_flow(call.message.chat.id, step="bank")
        await _render_bank(call.bot, call.message.chat.id, edit_call=call)
    await call.answer()


@router.callback_query(F.data.startswith("sw:b:"))
async def cb_bank(call: CallbackQuery, state: FSMContext):
    bank_key = call.data.split(":", 2)[2]
    if bank_key not in ALLOWED_BANKS_IP:
        await call.answer("Банк недоступен", show_alert=True)
        return
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True)
        return
    bank_title = BANK_TITLES[bank_key]
    price = 0
    try:
        price = _storage.resolve_lk_price(bank_title, 0)
    except Exception:
        pass
    await set_flow(call.message.chat.id, bank=bank_key, price=float(price),
                   step="verification_upload", uploads=[])
    await state.set_state(SellWizardForm.waiting_upload)
    await state.update_data(sw_chat_id=call.message.chat.id)
    await _render_verification(call.bot, call.message.chat.id, edit_call=call)
    await call.answer()


@router.callback_query(F.data.startswith("sw:back:"))
async def cb_back(call: CallbackQuery, state: FSMContext):
    target = call.data.split(":", 2)[2]
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True)
        return
    if target == "material":
        await state.clear()
        await set_flow(call.message.chat.id, step="material")
        try:
            await call.message.edit_text(
                _scripted("sell_material_prompt", "Какой материал сдаёте?"),
                parse_mode="HTML", reply_markup=_kb_material(),
            )
        except Exception:
            pass
    elif target == "bank":
        await state.clear()
        await set_flow(call.message.chat.id, step="bank", uploads=[])
        await _render_bank(call.bot, call.message.chat.id, edit_call=call)
    await call.answer()


@router.callback_query(F.data == "sw:sendcheck")
async def cb_sendcheck(call: CallbackQuery, state: FSMContext):
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True)
        return
    uploads = flow.get("uploads") or []
    if not uploads:
        await call.answer("Нет вложений! Пришлите фото/видео/текст в чат.", show_alert=True)
        return
    ok, err = await _forward_to_verification(call.bot, call.message.chat.id)
    if not ok:
        await call.answer(f"Ошибка: {err}", show_alert=True)
        return
    # SIMBA (авг 2026): чистим клиентские upload-сообщения из managed_chat —
    # они уже форварднуты в verification-группу, в чате клиента остаются
    # только «Загружено N пруфов» + служебные меню.
    chat_id = call.message.chat.id
    try:
        _cnt = 0
        for u in uploads:
            mid = u.get("msg_id")
            if not mid:
                continue
            try:
                await call.bot.delete_message(chat_id, int(mid))
                _cnt += 1
            except Exception:
                pass
        logger.info("[sell_wizard_crm] cleanup: deleted %d upload msgs from chat=%s", _cnt, chat_id)
    except Exception as _de:
        logger.warning("[sell_wizard_crm] cleanup uploads failed: %s", _de)
    waiting = _scripted("verification_awaiting",
                        "⏳ Пруфы на проверке. Ожидайте.")
    try:
        await call.message.edit_text(waiting, parse_mode="HTML")
    except Exception:
        pass
    # Сводка «сколько чего загружено» — одно чистое сообщение
    try:
        types_present = {}
        for u in uploads:
            t = u.get("type") or "?"
            types_present[t] = types_present.get(t, 0) + 1
        summary_parts = []
        for k, lbl in (("screenshot", "📷 скриншот"), ("video", "🎥 видео"),
                       ("inn", "✏️ ИНН")):
            if types_present.get(k):
                summary_parts.append(f"{lbl}×{types_present[k]}")
        if summary_parts:
            await call.bot.send_message(
                chat_id,
                f"📎 <b>Загружено на проверку:</b> {', '.join(summary_parts)}",
                parse_mode="HTML",
            )
    except Exception:
        pass
    await state.clear()
    await call.answer("Отправлено на проверку")


@router.callback_query(F.data.startswith("sw:vok:"))
async def cb_verif_ok(call: CallbackQuery, state: FSMContext):
    try:
        cid = int(call.data.split(":", 2)[2])
    except Exception:
        await call.answer("Bad chat id", show_alert=True)
        return
    flow = _storage.get_sell_flow(cid)
    if not flow:
        await call.answer("Заявка неактивна", show_alert=True)
        return
    await set_flow(cid, step="verification_approved")
    try:
        await call.message.edit_text(f"✅ <b>Пропущено на вяз</b> (chat {cid})",
                                     parse_mode="HTML")
    except Exception:
        pass
    await call.answer("Клиент уведомлён")
    await _render_payment(call.bot, cid)


@router.callback_query(F.data.startswith("sw:vno:"))
async def cb_verif_no(call: CallbackQuery, state: FSMContext):
    try:
        cid = int(call.data.split(":", 2)[2])
    except Exception:
        await call.answer("Bad chat id", show_alert=True)
        return
    reason = "требуется дополнительная проверка, свяжитесь с оператором"
    try:
        await call.message.edit_text(
            f"❌ <b>Отклонено</b> (chat {cid})\nПричина: {reason}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    text = _scripted("verification_rejected",
                     "❌ Проверка не пройдена. Причина: {reason}",
                     reason=reason)
    try:
        await call.bot.send_message(cid, text, parse_mode="HTML")
    except Exception:
        pass
    await clear_flow(cid)
    await call.answer("Клиент уведомлён")


@router.callback_query(F.data.startswith("sw:pay:"))
async def cb_payment(call: CallbackQuery, state: FSMContext):
    short = call.data.split(":", 2)[2]
    method = "GUARANTOR_BEFORE_WORK" if short == "GUAR" else "USDT_TRC20" if short == "USDT" else ""
    if not method:
        await call.answer("Bad method", show_alert=True)
        return
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True)
        return
    await set_flow(call.message.chat.id, method=method)
    if method == "GUARANTOR_BEFORE_WORK":
        await _render_guarantor_instruction(call.bot, call.message.chat.id, edit_call=call)
        await call.answer()
        return
    # USDT
    override = _storage.get_payment_text("usdt")
    text = override or _scripted("payment_usdt_prompt",
                                 "💸 USDT TRC20 — переходим к заполнению ЛК.")
    try:
        await call.message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass
    await call.answer("Переходим к заполнению ЛК")
    await _trigger_lk_form(call.bot, call.message.chat.id)


@router.callback_query(F.data == "sw:dealready")
async def cb_dealready(call: CallbackQuery, state: FSMContext):
    flow = get_flow(call.message.chat.id)
    if not flow or _is_stale(flow):
        await call.answer("Сессия истекла", show_alert=True)
        return
    await set_flow(call.message.chat.id, step="guarantor_wait_deal_number")
    await state.set_state(SellWizardForm.waiting_deal_number)
    await state.update_data(sw_chat_id=call.message.chat.id)
    text = _scripted("payment_guarantor_ask_deal",
                     "Пришлите номер сделки цифрами:")
    try:
        await call.message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass
    await call.answer("Жду номер сделки")


@router.callback_query(F.data.startswith("sw:gok:"))
async def cb_guar_deposited(call: CallbackQuery, state: FSMContext):
    try:
        cid = int(call.data.split(":", 2)[2])
    except Exception:
        await call.answer("Bad chat id", show_alert=True)
        return
    flow = _storage.get_sell_flow(cid)
    if not flow:
        await call.answer("Заявка неактивна", show_alert=True)
        return
    deal_number = flow.get("deal_number") or "?"
    try:
        await call.message.edit_text(
            f"💰 <b>Средства внесены</b> (сделка №{deal_number}, chat {cid})",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await call.answer("Клиент уведомлён")
    text = _scripted("payment_guarantor_deposited",
                     "💰 Сделка №{deal_number} пополнена!",
                     deal_number=deal_number)
    try:
        await call.bot.send_message(cid, text, parse_mode="HTML")
    except Exception:
        pass
    await _trigger_lk_form(call.bot, cid)


# ─────────────────────── MESSAGE HANDLERS ───────────────────────

@router.message(SellWizardForm.waiting_upload)
async def msg_upload(message: Message, state: FSMContext):
    """AUDIT #2 HIGH-10 (авг 2026): legacy waiting_upload — если клиент шлёт
    файл ДО нажатия кнопки screenshot/video/inn, мы больше не молча пишем
    type='photo'/'document' (это не проходит checklist). Вместо этого:

      * фото/документ → авто-мапим в 'screenshot' если ещё не загружено
      * видео → авто-мапим в 'video'
      * текст 10-12 цифр → авто-мапим в 'inn'
      * иначе → мягкий reply «нажмите кнопку выше» + ре-рендер меню.
    """
    data = await state.get_data()
    if int(data.get("sw_chat_id") or 0) != int(message.chat.id):
        return
    if await _maybe_wizard_cancel(message, state):
        return
    flow = get_flow(message.chat.id)
    if not flow or _is_stale(flow) or flow.get("step") != "verification_upload":
        return
    existing_types = {(u.get("type") or "").lower() for u in (flow.get("uploads") or [])}
    upload = None
    if message.photo or message.document:
        # первый скриншот/пруф → screenshot; повторный — тоже screenshot
        # (можно бесконечно догружать; checklist проверяет ПО ТИПАМ, не по кол-ву)
        upload = {"type": "screenshot", "msg_id": int(message.message_id),
                  "caption": (message.caption or "")[:200]}
    elif message.video:
        upload = {"type": "video", "msg_id": int(message.message_id),
                  "caption": (message.caption or "")[:200]}
    elif (message.text or "").strip():
        _t = message.text.strip()
        # Если похоже на ИНН (10-12 цифр) и inn ещё не сдан — мапим в inn
        if _t.isdigit() and 10 <= len(_t) <= 12 and "inn" not in existing_types:
            upload = {"type": "inn", "msg_id": int(message.message_id),
                      "caption": _t[:20]}
        else:
            # Подсказка — не молчим
            try:
                await message.reply(
                    "ℹ️ Пожалуйста, нажмите кнопку в меню выше "
                    "(📷 Скриншот / 🎥 Видео / ✏️ ИНН) перед загрузкой."
                )
            except Exception:
                pass
            return
    if not upload:
        return
    new_flow = await _storage.sell_flow_append_upload(message.chat.id, upload)
    logger.info("[sell_wizard_crm] legacy upload+ mapped chat=%s type=%s total=%d",
                message.chat.id, upload["type"], len(new_flow.get("uploads") or []))
    try:
        await message.react([{"type": "emoji", "emoji": "👍"}])
    except Exception:
        pass
    # Ре-рендерим checklist сразу — клиент видит новую галочку
    try:
        await _rerender_verification_after_upload(message.bot, message.chat.id)
    except Exception:
        pass


# SIMBA (авг 2026): message-handlers для 3 отдельных типов пруфов.
# Каждый принимает СТРОГО свой тип, иначе клиент получает пояснение
# что не так и что делать.

@router.message(SellWizardForm.waiting_screenshot)
async def msg_upload_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    if int(data.get("sw_chat_id") or 0) != int(message.chat.id):
        return
    if await _maybe_wizard_cancel(message, state):
        return
    flow = get_flow(message.chat.id)
    if not flow or _is_stale(flow):
        return
    if not (message.photo or message.document):
        try:
            await message.reply("❌ Нужен именно скриншот (фото или картинка-документ). Попробуйте ещё раз.")
        except Exception:
            pass
        return
    upload = {"type": "screenshot", "msg_id": int(message.message_id),
              "caption": (message.caption or "")[:200]}
    await _storage.sell_flow_append_upload(message.chat.id, upload)
    logger.info("[sell_wizard_crm] screenshot+ chat=%s", message.chat.id)
    try: await message.react([{"type": "emoji", "emoji": "👍"}])
    except Exception: pass
    await state.clear()  # Выходим из state — клиент возвращается к меню кнопок
    # Ре-рендерим меню с обновлёнными чекбоксами (edit прежнего wizard_msg_id)
    try:
        await _rerender_verification_after_upload(message.bot, message.chat.id)
    except Exception:
        pass


@router.message(SellWizardForm.waiting_video)
async def msg_upload_video(message: Message, state: FSMContext):
    data = await state.get_data()
    if int(data.get("sw_chat_id") or 0) != int(message.chat.id):
        return
    if await _maybe_wizard_cancel(message, state):
        return
    flow = get_flow(message.chat.id)
    if not flow or _is_stale(flow):
        return
    if not message.video:
        try:
            await message.reply("❌ Нужно видео (не фото и не текст). Попробуйте ещё раз.")
        except Exception:
            pass
        return
    upload = {"type": "video", "msg_id": int(message.message_id),
              "caption": (message.caption or "")[:200]}
    await _storage.sell_flow_append_upload(message.chat.id, upload)
    logger.info("[sell_wizard_crm] video+ chat=%s", message.chat.id)
    try: await message.react([{"type": "emoji", "emoji": "👍"}])
    except Exception: pass
    await state.clear()
    try:
        await _rerender_verification_after_upload(message.bot, message.chat.id)
    except Exception:
        pass


@router.message(SellWizardForm.waiting_inn)
async def msg_upload_inn(message: Message, state: FSMContext):
    data = await state.get_data()
    if int(data.get("sw_chat_id") or 0) != int(message.chat.id):
        return
    if await _maybe_wizard_cancel(message, state):
        return
    flow = get_flow(message.chat.id)
    if not flow or _is_stale(flow):
        return
    text = (message.text or "").strip()
    # ИНН = 10 или 12 цифр
    m = re.match(r"^\s*(\d{10}|\d{12})\s*$", text)
    if not m:
        try:
            await message.reply(
                "❌ ИНН должен быть 10 (для ИП) или 12 цифр (для физлица), "
                "без пробелов и других символов. Попробуйте ещё раз."
            )
        except Exception:
            pass
        return
    inn = m.group(1)
    upload = {"type": "inn", "msg_id": int(message.message_id), "caption": inn}
    await _storage.sell_flow_append_upload(message.chat.id, upload)
    logger.info("[sell_wizard_crm] inn+ chat=%s inn=%s", message.chat.id, inn)
    try: await message.react([{"type": "emoji", "emoji": "👍"}])
    except Exception: pass
    await state.clear()
    try:
        await _rerender_verification_after_upload(message.bot, message.chat.id)
    except Exception:
        pass


async def _rerender_verification_after_upload(bot, chat_id):
    """После загрузки пруфа — обновляем существующее wizard-сообщение с
    новыми чекбоксами (без создания нового меню)."""
    flow = get_flow(chat_id)
    wizard_msg_id = int(flow.get("wizard_msg_id") or 0)
    if not wizard_msg_id:
        # fallback — просто отрендерим заново новым сообщением
        await _render_verification(bot, chat_id)
        return
    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    price = float(flow.get("price") or 0)
    uploads = flow.get("uploads") or []
    cfg = _storage.get_verification_config(bank_title) or {}
    text = (cfg.get("text") or "").strip()
    if not text:
        scripted_key = VERIFICATION_SCRIPTED_KEY.get(bank_key)
        if scripted_key:
            text = _scripted(scripted_key, "")
    if not text:
        text = f"🛡 Проверка ЛК {bank_title}"
    header = f"💰 Цена: <b>{int(price)}$</b>\n\n" if price > 0 else ""
    types_present = {u.get("type") for u in uploads if u.get("type")}
    require_video = bool((cfg or {}).get("allow_video", bank_key == "ALFA"))
    footer_lines = ["", "📋 <b>Что нужно предоставить:</b>"]
    if require_video:
        footer_lines.append(f"{'✅' if 'video' in types_present else '⬜'} 🎥 Видео")
    footer_lines.append(f"{'✅' if 'screenshot' in types_present else '⬜'} 📷 Скриншот")
    footer_lines.append(f"{'✅' if 'inn' in types_present else '⬜'} ✏️ ИНН")
    footer_lines.append("\n👉 Жмите кнопку, затем присылайте одним сообщением.")
    full = header + text + "\n".join(footer_lines)
    kb = _kb_verification(uploads, bank_key)
    try:
        await bot.edit_message_text(full, chat_id=chat_id, message_id=wizard_msg_id,
                                     parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.warning("[sell_wizard_crm] rerender edit failed chat=%s: %s", chat_id, e)


@router.message(SellWizardForm.waiting_deal_number, F.text)
async def msg_deal_number(message: Message, state: FSMContext):
    data = await state.get_data()
    if int(data.get("sw_chat_id") or 0) != int(message.chat.id):
        return
    if await _maybe_wizard_cancel(message, state):
        return
    m = re.match(r"^\s*#?\s*(\d{3,10})\s*$", message.text or "")
    if not m:
        return
    deal_number = m.group(1)
    await set_flow(message.chat.id, deal_number=deal_number)
    await state.clear()
    await _forward_deal_to_guarantor(message.bot, message.chat.id, deal_number)

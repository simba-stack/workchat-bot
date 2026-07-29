"""
crm_admin.py — админка ЦРМ v2 (/crm_admin в ЛС @PrideCONTROLE_bot).

Owner-only меню:
  📋 Прайс           — set/delete по банкам (storage.pricing)
  🛡 Проверки        — инструкция + типы вложений per bank
  💳 Оплата          — тексты after_verification / guarantor / usdt
  👥 Группы          — verification_group_id / guarantor_group_id

Callback prefix: ca:*  (crm-admin)
FSM state class:  CRMAdminForm

Подключение в crm_bot.py:
    import crm_admin
    dp.include_router(crm_admin.router)
"""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

logger = logging.getLogger(__name__)

router = Router(name="crm_admin_v2")

# Разрешённые банки для проверок в визарде (см. sell_wizard.ALLOWED_BANKS_IP)
BANKS_FOR_VERIFICATION = ["АЛЬФА", "ОЗОН", "РАЙФ"]

PAYMENT_TEXT_LABELS = {
    "after_verification": "После проверки (перед выбором оплаты)",
    "guarantor": "Инструкция по гаранту",
    "usdt": "Инструкция по USDT",
}


# storage будет установлен через set_storage() из crm_bot.py при регистрации
_storage = None
_is_owner_fn = None


def set_dependencies(storage_ref, is_owner_fn):
    """Вызывается из crm_bot.py при импорте — прокидываем зависимости."""
    global _storage, _is_owner_fn
    _storage = storage_ref
    _is_owner_fn = is_owner_fn


class CRMAdminForm(StatesGroup):
    waiting_pricing_price = State()      # ввод цены после выбора банка
    waiting_verification_text = State()  # ввод текста инструкции проверки
    waiting_payment_text = State()       # ввод текста оплаты
    waiting_group_id = State()            # ввод chat_id группы


# ═══════════════════════════ HELPERS ═══════════════════════════

def _kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Прайс", callback_data="ca:pricing")],
        [InlineKeyboardButton(text="🛡 Проверки", callback_data="ca:verif")],
        [InlineKeyboardButton(text="💳 Оплата", callback_data="ca:pay")],
        [InlineKeyboardButton(text="👥 Группы (проверка/бухгалтерия)",
                              callback_data="ca:groups")],
    ])


def _kb_back(target: str = "main"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ca:back:{target}")],
    ])


def _require_owner(user_id: int) -> bool:
    if not _is_owner_fn:
        return False
    try:
        return bool(_is_owner_fn(int(user_id)))
    except Exception:
        return False


# ═══════════════════════════ ENTRY: /crm_admin ═══════════════════════════

@router.message(Command("crm_admin"))
async def cmd_crm_admin(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    if not _require_owner(message.from_user.id):
        await message.reply("⛔ Только для владельца PRIDE.")
        return
    await state.clear()
    await message.answer(
        "⚙️ <b>ЦРМ v2 · Админка</b>\n\n"
        "Выберите раздел для редактирования:",
        parse_mode="HTML", reply_markup=_kb_main(),
    )


@router.callback_query(F.data == "ca:back:main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔ Только для владельца.", show_alert=True); return
    await state.clear()
    try:
        await call.message.edit_text(
            "⚙️ <b>ЦРМ v2 · Админка</b>\n\nВыберите раздел:",
            parse_mode="HTML", reply_markup=_kb_main(),
        )
    except Exception:
        pass
    await call.answer()


# ═══════════════════════════ PRICING ═══════════════════════════

@router.callback_query(F.data == "ca:pricing")
async def cb_pricing(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    await state.clear()
    prices = _storage.list_pricing() or {}
    lines = ["📋 <b>Прайс ЛК</b>\n"]
    if prices:
        for bank, price in sorted(prices.items()):
            lines.append(f"• <b>{bank}</b> — <code>{float(price):g}$</code>")
    else:
        lines.append("<i>Прайс пуст.</i>")
    lines.append("\nВыберите банк для установки цены:")
    kb_rows = []
    row = []
    for bank in BANKS_FOR_VERIFICATION:
        row.append(InlineKeyboardButton(text=f"💵 {bank}",
                                         callback_data=f"ca:pricing:set:{bank}"))
    kb_rows.append(row)
    # Дополнительные банки из storage (не из BANKS_FOR_VERIFICATION)
    extra_row = []
    for bank in sorted(prices.keys()):
        if bank not in BANKS_FOR_VERIFICATION:
            extra_row.append(InlineKeyboardButton(text=f"💵 {bank}",
                                                    callback_data=f"ca:pricing:set:{bank}"))
    if extra_row:
        # разбиваем по 3
        chunk = []
        for b in extra_row:
            chunk.append(b)
            if len(chunk) == 3:
                kb_rows.append(chunk); chunk = []
        if chunk:
            kb_rows.append(chunk)
    kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ca:back:main")])
    try:
        await call.message.edit_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("ca:pricing:set:"))
async def cb_pricing_set(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    bank = call.data.split(":", 3)[3]
    await state.set_state(CRMAdminForm.waiting_pricing_price)
    await state.update_data(pricing_bank=bank)
    cur = _storage.get_pricing(bank)
    cur_line = f"\nТекущая цена: <b>{float(cur):g}$</b>" if cur else ""
    try:
        await call.message.edit_text(
            f"💵 <b>Цена банка {bank}</b>{cur_line}\n\n"
            f"Пришлите новую цену числом (например: <code>450</code>).\n"
            f"«0» / «удали» — снять цену.",
            parse_mode="HTML", reply_markup=_kb_back("pricing"),
        )
    except Exception:
        pass
    await call.answer()


@router.message(CRMAdminForm.waiting_pricing_price, F.text)
async def msg_pricing_price(message: Message, state: FSMContext):
    if not _require_owner(message.from_user.id):
        return
    data = await state.get_data()
    bank = data.get("pricing_bank") or ""
    text = (message.text or "").strip().lower()
    if text in ("удали", "delete", "-", "0", ""):
        ok = await _storage.remove_pricing(bank)
        await message.answer(f"🗑 Цена {bank} " + ("снята." if ok else "и так не была задана."))
    else:
        try:
            price = float(text.replace(",", ".").replace("$", "").strip())
        except Exception:
            await message.answer("⚠️ Не понял. Пришлите число, например: 450")
            return
        await _storage.set_pricing(bank, price)
        await message.answer(f"✅ Цена <b>{bank}</b> = <b>{price:g}$</b>",
                             parse_mode="HTML")
    await state.clear()
    await message.answer("⬇️", reply_markup=_kb_main())


@router.callback_query(F.data == "ca:back:pricing")
async def cb_back_pricing(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_pricing(call, state)


# ═══════════════════════════ VERIFICATION ═══════════════════════════

@router.callback_query(F.data == "ca:verif")
async def cb_verif(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    await state.clear()
    text = (
        "🛡 <b>Проверки ЛК per bank</b>\n\n"
        "Выберите банк — редактируем текст инструкции и разрешённые типы вложений "
        "(фото/видео/текст)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🛡 {bank}",
                              callback_data=f"ca:verif:{bank}")]
        for bank in BANKS_FOR_VERIFICATION
    ] + [[InlineKeyboardButton(text="◀️ Назад", callback_data="ca:back:main")]])
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await call.answer()


def _verif_bank_text_kb(bank: str):
    cfg = _storage.get_verification_config(bank) or {}
    def mark(v): return "✅" if v else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Редактировать текст инструкции",
                              callback_data=f"ca:verif:edit_text:{bank}")],
        [InlineKeyboardButton(text=f"{mark(cfg.get('allow_photo', True))} Разрешить фото",
                              callback_data=f"ca:verif:toggle:photo:{bank}"),
         InlineKeyboardButton(text=f"{mark(cfg.get('allow_video', True))} Видео",
                              callback_data=f"ca:verif:toggle:video:{bank}")],
        [InlineKeyboardButton(text=f"{mark(cfg.get('allow_text', True))} Текст (ответ клиента)",
                              callback_data=f"ca:verif:toggle:text:{bank}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ca:verif")],
    ])


@router.callback_query(F.data.startswith("ca:verif:") & ~F.data.contains(":edit_text:") & ~F.data.contains(":toggle:"))
async def cb_verif_bank(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    parts = call.data.split(":")
    if len(parts) < 3:
        return
    bank = parts[2]
    cfg = _storage.get_verification_config(bank) or {}
    text = cfg.get("text") or "<i>(текст не задан — используется дефолт из scripted_texts)</i>"
    body = (
        f"🛡 <b>Проверка ЛК {bank}</b>\n\n"
        f"<b>Текущий текст инструкции:</b>\n"
        f"<blockquote>{text[:2000]}</blockquote>"
    )
    try:
        await call.message.edit_text(body, parse_mode="HTML",
                                     reply_markup=_verif_bank_text_kb(bank))
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("ca:verif:toggle:"))
async def cb_verif_toggle(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    parts = call.data.split(":")
    if len(parts) < 5:
        return
    field, bank = parts[3], parts[4]
    cfg = _storage.get_verification_config(bank) or {}
    key_map = {"photo": "allow_photo", "video": "allow_video", "text": "allow_text"}
    key = key_map.get(field)
    if not key:
        await call.answer("Unknown toggle", alert=True); return
    new_val = not bool(cfg.get(key, True))
    await _storage.set_verification_config(bank, **{key: new_val})
    await call.answer(f"{key}={new_val}")
    # Перерисуем экран
    try:
        await call.message.edit_reply_markup(reply_markup=_verif_bank_text_kb(bank))
    except Exception:
        pass


@router.callback_query(F.data.startswith("ca:verif:edit_text:"))
async def cb_verif_edit_text(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    bank = call.data.split(":", 3)[3]
    await state.set_state(CRMAdminForm.waiting_verification_text)
    await state.update_data(verif_bank=bank)
    try:
        await call.message.edit_text(
            f"📝 <b>Редактирование инструкции проверки — {bank}</b>\n\n"
            f"Пришлите новый текст ОДНИМ сообщением. HTML разрешён.\n"
            f"«-» — удалить (вернуть дефолт из scripted_texts).",
            parse_mode="HTML", reply_markup=_kb_back(f"verif_bank:{bank}"),
        )
    except Exception:
        pass
    await call.answer()


@router.message(CRMAdminForm.waiting_verification_text, F.text)
async def msg_verif_text(message: Message, state: FSMContext):
    if not _require_owner(message.from_user.id):
        return
    data = await state.get_data()
    bank = data.get("verif_bank") or ""
    text = (message.text or "").strip()
    if text == "-":
        await _storage.set_verification_config(bank, text="")
        await message.answer(f"🗑 Текст {bank} сброшен на дефолт.")
    else:
        await _storage.set_verification_config(bank, text=text)
        await message.answer(f"✅ Инструкция <b>{bank}</b> сохранена ({len(text)} симв.)",
                             parse_mode="HTML")
    await state.clear()
    await message.answer("⬇️", reply_markup=_kb_main())


@router.callback_query(F.data.startswith("ca:back:verif_bank:"))
async def cb_back_verif_bank(call: CallbackQuery, state: FSMContext):
    await state.clear()
    # Просто перерисуем bank экран
    bank = call.data.split(":", 3)[3]
    call.data = f"ca:verif:{bank}"
    await cb_verif_bank(call, state)


# ═══════════════════════════ PAYMENT TEXTS ═══════════════════════════

@router.callback_query(F.data == "ca:pay")
async def cb_pay(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    await state.clear()
    lines = ["💳 <b>Тексты оплаты</b>\n"]
    for k, lbl in PAYMENT_TEXT_LABELS.items():
        val = _storage.get_payment_text(k) or "<i>(дефолт из scripted_texts)</i>"
        lines.append(f"<b>{lbl}:</b>\n<blockquote>{val[:300]}</blockquote>\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✏️ {lbl}", callback_data=f"ca:pay:edit:{k}")]
        for k, lbl in PAYMENT_TEXT_LABELS.items()
    ] + [[InlineKeyboardButton(text="◀️ Назад", callback_data="ca:back:main")]])
    try:
        await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("ca:pay:edit:"))
async def cb_pay_edit(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    key = call.data.split(":", 3)[3]
    if key not in PAYMENT_TEXT_LABELS:
        await call.answer("Bad key", alert=True); return
    await state.set_state(CRMAdminForm.waiting_payment_text)
    await state.update_data(payment_key=key)
    lbl = PAYMENT_TEXT_LABELS[key]
    hint = ""
    if key == "guarantor":
        hint = "\n\nПлейсхолдер: <code>{price}</code>"
    try:
        await call.message.edit_text(
            f"✏️ <b>Редактируем: {lbl}</b>{hint}\n\n"
            f"Пришлите новый текст ОДНИМ сообщением. HTML разрешён.\n"
            f"«-» — удалить (вернуть дефолт).",
            parse_mode="HTML", reply_markup=_kb_back("pay"),
        )
    except Exception:
        pass
    await call.answer()


@router.message(CRMAdminForm.waiting_payment_text, F.text)
async def msg_pay_text(message: Message, state: FSMContext):
    if not _require_owner(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("payment_key") or ""
    text = (message.text or "").strip()
    if text == "-":
        await _storage.set_payment_text(key, "")
        await message.answer(f"🗑 Текст «{PAYMENT_TEXT_LABELS.get(key, key)}» сброшен.")
    else:
        await _storage.set_payment_text(key, text)
        await message.answer(f"✅ Сохранено ({len(text)} симв.)")
    await state.clear()
    await message.answer("⬇️", reply_markup=_kb_main())


@router.callback_query(F.data == "ca:back:pay")
async def cb_back_pay(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_pay(call, state)


# ═══════════════════════════ GROUPS ═══════════════════════════

@router.callback_query(F.data == "ca:groups")
async def cb_groups(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    await state.clear()
    vg = _storage.get_verification_group_id() or 0
    gg = _storage.get_guarantor_group_id() or 0
    text = (
        "👥 <b>Группы</b>\n\n"
        f"🛡 <b>Проверка ЛК:</b> <code>{vg or '(не задан)'}</code>\n"
        f"💰 <b>Гарант / бухгалтерия:</b> <code>{gg or '(не задан)'}</code>\n\n"
        "Чтобы задать ID — перешлите сюда любое сообщение из нужной группы, "
        "либо пришлите ID числом после выбора кнопки ниже."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Задать группу проверки",
                              callback_data="ca:groups:set:verif")],
        [InlineKeyboardButton(text="💰 Задать группу гаранта",
                              callback_data="ca:groups:set:guar")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ca:back:main")],
    ])
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("ca:groups:set:"))
async def cb_groups_set(call: CallbackQuery, state: FSMContext):
    if not _require_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True); return
    target = call.data.split(":", 3)[3]  # "verif" | "guar"
    if target not in ("verif", "guar"):
        await call.answer("Bad target", alert=True); return
    await state.set_state(CRMAdminForm.waiting_group_id)
    await state.update_data(group_target=target)
    lbl = "проверки" if target == "verif" else "гаранта"
    try:
        await call.message.edit_text(
            f"👥 <b>Задать группу {lbl}</b>\n\n"
            f"Пришлите ID группы (число, обычно с минусом -100…) или "
            f"перешлите сюда любое сообщение из этой группы.",
            parse_mode="HTML", reply_markup=_kb_back("groups"),
        )
    except Exception:
        pass
    await call.answer()


@router.message(CRMAdminForm.waiting_group_id)
async def msg_group_id(message: Message, state: FSMContext):
    if not _require_owner(message.from_user.id):
        return
    data = await state.get_data()
    target = data.get("group_target") or ""
    chat_id_val = None
    # Пересылка → forward_from_chat.id
    if message.forward_from_chat:
        chat_id_val = int(message.forward_from_chat.id)
    else:
        try:
            chat_id_val = int((message.text or "").strip())
        except Exception:
            await message.answer("⚠️ Не понял. Пришлите число (ID группы) или перешлите сообщение.")
            return
    if target == "verif":
        await _storage.set_verification_group_id(chat_id_val)
        await message.answer(f"✅ Группа проверки: <code>{chat_id_val}</code>",
                             parse_mode="HTML")
    elif target == "guar":
        await _storage.set_guarantor_group_id(chat_id_val)
        await message.answer(f"✅ Группа гаранта: <code>{chat_id_val}</code>",
                             parse_mode="HTML")
    await state.clear()
    await message.answer("⬇️", reply_markup=_kb_main())


@router.callback_query(F.data == "ca:back:groups")
async def cb_back_groups(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_groups(call, state)

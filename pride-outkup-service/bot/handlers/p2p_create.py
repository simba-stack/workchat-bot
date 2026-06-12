"""P2P: создание оффера прямо в боте (FSM-флоу).

Шаги:
  1. Сторона: Купить / Продать
  2. Монета (USDT/USDC/TON/BTC/...)
  3. Фиат (RUB/USD/EUR)
  4. Тип цены: Фиксированная / Плавающая (от индекса)
  5. Цена / Margin %
  6. Min / Max лимит
  7. Методы оплаты (multi-select toggle)
  8. Pay window: 15/30/45/60/90/120 мин
  9. Условия (необязательно)
 10. Подтверждение → POST оффер
"""
from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from core.db import AsyncSessionLocal
from core.models import Offer, User
from core.services import price_index as pi_svc

router = Router(name="p2p_create")
logger = logging.getLogger(__name__)


class OfferFSM(StatesGroup):
    coin = State()
    fiat = State()
    price_type = State()
    price = State()
    min_amt = State()
    max_amt = State()
    methods = State()
    pay_window = State()
    conditions = State()
    confirm = State()


def _b(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=data)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


COINS = ["USDT", "USDC", "TON", "BTC", "ETH", "TRX"]
FIATS = ["RUB", "USD", "EUR"]
ALL_METHODS = [
    ("sbp", "СБП"), ("tinkoff", "Тинькофф"), ("sber", "Сбер"),
    ("alpha", "Альфа"), ("ozon", "Озон"), ("raif", "Райф"),
    ("vtb", "ВТБ"), ("cash", "Наличные"),
]
WINDOWS = [15, 30, 45, 60, 90, 120]


async def _get_user(tg_id: int) -> User | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()


# ═══════════════════════ Entry: «+ Создать оффер» ═══════════════════════
@router.callback_query(F.data == "p2p:create")
async def cb_create(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(side="sell")  # default sell
    await call.message.edit_text(
        "<b>Создание оффера</b>\n\nЧто делаете?",
        reply_markup=_kb([
            [_b("Продаю крипту", "pc:side:sell"), _b("Покупаю крипту", "pc:side:buy")],
            [_b("← Назад", "p2p:my_offers")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pc:side:"))
async def cb_side(call: CallbackQuery, state: FSMContext):
    side = call.data.split(":")[2]
    await state.update_data(side=side, methods=[])
    await state.set_state(OfferFSM.coin)
    await call.message.edit_text(
        f"<b>Сторона:</b> {'Продажа' if side=='sell' else 'Покупка'}\n\nВыберите монету:",
        reply_markup=_kb([
            [_b(c, f"pc:coin:{c}") for c in COINS[:3]],
            [_b(c, f"pc:coin:{c}") for c in COINS[3:]],
            [_b("← Назад", "p2p:create")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pc:coin:"))
async def cb_coin(call: CallbackQuery, state: FSMContext):
    coin = call.data.split(":")[2]
    await state.update_data(coin=coin)
    await state.set_state(OfferFSM.fiat)
    await call.message.edit_text(
        f"<b>Монета:</b> {coin}\n\nФиатная валюта:",
        reply_markup=_kb([
            [_b(f, f"pc:fiat:{f}") for f in FIATS],
            [_b("← Назад", f"pc:side:sell")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pc:fiat:"))
async def cb_fiat(call: CallbackQuery, state: FSMContext):
    fiat = call.data.split(":")[2]
    await state.update_data(fiat=fiat)
    data = await state.get_data()
    # Покажем текущий рыночный курс
    async with AsyncSessionLocal() as db:
        idx = await pi_svc.get_index(db, data["coin"], fiat)
    idx_line = f"\nРыночный курс: <b>{float(idx):.2f}</b> {fiat}" if idx else ""
    await state.set_state(OfferFSM.price_type)
    await call.message.edit_text(
        f"<b>{data['coin']}/{fiat}</b>{idx_line}\n\nТип цены:",
        reply_markup=_kb([
            [_b("Фиксированная", "pc:ptype:fixed")],
            [_b("Плавающая (от рынка)", "pc:ptype:float")],
            [_b("← Назад", f"pc:coin:{data['coin']}")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pc:ptype:"))
async def cb_ptype(call: CallbackQuery, state: FSMContext):
    pt = call.data.split(":")[2]
    await state.update_data(price_type=pt)
    await state.set_state(OfferFSM.price)
    data = await state.get_data()
    if pt == "fixed":
        prompt = f"Введи цену (сколько {data['fiat']} за 1 {data['coin']}):"
    else:
        prompt = ("Введи плавающую маржу в % от рынка (85..115).\n"
                  "Пример: <code>100.5</code> = +0.5% от индекса")
    await call.message.edit_text(
        f"<b>Тип:</b> {'Фиксированная' if pt=='fixed' else 'Плавающая'}\n\n{prompt}",
        reply_markup=_kb([[_b("← Назад", f"pc:fiat:{data['fiat']}")]]),
    )
    await call.answer()


@router.message(OfferFSM.price, F.text)
async def msg_price(message: Message, state: FSMContext):
    try:
        v = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Нужно число. Например: 95.50"); return
    if v <= 0:
        await message.answer("> 0"); return
    data = await state.get_data()
    if data["price_type"] == "float" and not (Decimal("85") <= v <= Decimal("115")):
        await message.answer("Маржа 85..115"); return
    await state.update_data(price=str(v))
    await state.set_state(OfferFSM.min_amt)
    await message.answer(
        f"Цена принята: <b>{v}</b>\n\nМинимальная сумма сделки в {data['fiat']}:",
    )


@router.message(OfferFSM.min_amt, F.text)
async def msg_min_amt(message: Message, state: FSMContext):
    try:
        v = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Нужно число"); return
    if v < 100:
        await message.answer("Минимум 100"); return
    await state.update_data(min_amt=str(v))
    await state.set_state(OfferFSM.max_amt)
    data = await state.get_data()
    await message.answer(f"Минимум: <b>{v} {data['fiat']}</b>\n\nМаксимальная сумма:")


@router.message(OfferFSM.max_amt, F.text)
async def msg_max_amt(message: Message, state: FSMContext):
    try:
        v = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Нужно число"); return
    data = await state.get_data()
    if v < Decimal(data["min_amt"]):
        await message.answer(f"Должно быть ≥ {data['min_amt']}"); return
    await state.update_data(max_amt=str(v), methods=[])
    await state.set_state(OfferFSM.methods)
    await _show_methods(message, state)


async def _show_methods(target, state: FSMContext):
    data = await state.get_data()
    sel = set(data.get("methods", []))
    rows = []
    for code, label in ALL_METHODS:
        mark = "✓ " if code in sel else "  "
        rows.append([_b(f"{mark}{label}", f"pc:method:{code}")])
    rows.append([_b("Готово →", "pc:methods_done")])
    text = (
        f"<b>Методы оплаты</b> (выбери до 5):\n\n"
        f"Выбрано: {len(sel)}"
    )
    kb = _kb(rows)
    if hasattr(target, "edit_text"):
        try: await target.edit_text(text, reply_markup=kb); return
        except Exception: pass
    if hasattr(target, "answer"):
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pc:method:"))
async def cb_method(call: CallbackQuery, state: FSMContext):
    code = call.data.split(":")[2]
    data = await state.get_data()
    sel = list(data.get("methods", []))
    if code in sel:
        sel.remove(code)
    elif len(sel) < 5:
        sel.append(code)
    else:
        await call.answer("Максимум 5", show_alert=True); return
    await state.update_data(methods=sel)
    await _show_methods(call.message, state)
    await call.answer()


@router.callback_query(F.data == "pc:methods_done")
async def cb_methods_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("methods"):
        await call.answer("Выбери хотя бы 1 метод", show_alert=True); return
    await state.set_state(OfferFSM.pay_window)
    rows = [[_b(f"{w} мин", f"pc:window:{w}") for w in WINDOWS[:3]],
            [_b(f"{w} мин", f"pc:window:{w}") for w in WINDOWS[3:]]]
    await call.message.edit_text(
        "<b>Окно оплаты</b>\nСколько времени даёшь покупателю на оплату:",
        reply_markup=_kb(rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pc:window:"))
async def cb_window(call: CallbackQuery, state: FSMContext):
    w = int(call.data.split(":")[2])
    await state.update_data(pay_window=w)
    await state.set_state(OfferFSM.conditions)
    await call.message.edit_text(
        "<b>Условия</b> (необязательно)\n\n"
        "Напиши свои требования к контрагенту, например:\n"
        "<i>Работаю с 10:00-22:00 МСК. Чек обязателен. ФИО плательщика должно совпадать с TG.</i>\n\n"
        "Или пропусти — нажми «Пропустить».",
        reply_markup=_kb([[_b("Пропустить", "pc:skip_cond")]]),
    )
    await call.answer()


@router.message(OfferFSM.conditions, F.text)
async def msg_conditions(message: Message, state: FSMContext):
    await state.update_data(conditions=message.text[:1024])
    await _show_confirm(message, state)


@router.callback_query(F.data == "pc:skip_cond")
async def cb_skip_cond(call: CallbackQuery, state: FSMContext):
    await state.update_data(conditions="")
    await _show_confirm(call.message, state)
    await call.answer()


async def _show_confirm(target, state: FSMContext):
    data = await state.get_data()
    methods_h = ", ".join(
        next((l for c, l in ALL_METHODS if c == m), m) for m in data["methods"]
    )
    side_h = "ПРОДАЖА" if data["side"] == "sell" else "ПОКУПКА"
    ptype_h = "fixed" if data["price_type"] == "fixed" else f"float ×{data['price']}%"
    text = (
        f"<b>Проверь оффер:</b>\n\n"
        f"Тип: <b>{side_h}</b> {data['coin']}/{data['fiat']}\n"
        f"Цена: <b>{data['price']}</b> ({ptype_h})\n"
        f"Лимиты: {data['min_amt']}–{data['max_amt']} {data['fiat']}\n"
        f"Методы: {methods_h}\n"
        f"Окно: {data['pay_window']} мин\n"
    )
    if data.get("conditions"):
        text += f"\nУсловия:\n<i>{data['conditions'][:300]}</i>\n"
    await state.set_state(OfferFSM.confirm)
    kb = _kb([
        [_b("Создать и запустить", "pc:submit")],
        [_b("Отмена", "p2p:my_offers")],
    ])
    if hasattr(target, "edit_text"):
        try: await target.edit_text(text, reply_markup=kb); return
        except Exception: pass
    if hasattr(target, "answer"):
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "pc:submit")
async def cb_submit(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    me = await _get_user(call.from_user.id)
    if not me:
        await call.answer("Не зарегистрирован", show_alert=True)
        await state.clear(); return

    async with AsyncSessionLocal() as db:
        price = Decimal(data["price"])
        float_margin = None
        if data["price_type"] == "float":
            float_margin = price
            live = await pi_svc.compute_float_price(db, data["coin"], data["fiat"], price)
            if not live:
                await call.answer(
                    f"Индекс {data['coin']}/{data['fiat']} ещё не загружен. Попробуй позже.",
                    show_alert=True,
                ); return
            stored_price = live
        else:
            stored_price = price

        # Price band проверка
        ok, idx, band = await pi_svc.within_band(db, stored_price, data["coin"], data["fiat"])
        if not ok and idx:
            await call.answer(
                f"Цена отклоняется от рынка >±{float(band)}%. Поправь цену.",
                show_alert=True,
            ); return

        # Для sell — нужен баланс на эскроу
        max_amt = Decimal(data["max_amt"])
        if data["side"] == "sell":
            need = max_amt / stored_price
            if Decimal(me.balance_usdt or 0) < need:
                await call.answer(
                    f"Нужно ≥ {float(need):.4f} {data['coin']} на балансе для эскроу",
                    show_alert=True,
                ); return

        o = Offer(
            user_id=me.id, side=data["side"],
            coin=data["coin"], fiat=data["fiat"],
            price_type=data["price_type"],
            float_margin_pct=float_margin,
            rate_rub_per_usdt=stored_price,
            min_amount_rub=Decimal(data["min_amt"]),
            max_amount_rub=max_amt,
            payment_methods=list(data["methods"]),
            pay_window_min=int(data["pay_window"]),
            conditions=(data.get("conditions") or "").strip()[:1024] or None,
            status="active",
        )
        db.add(o)
        await db.commit()
        offer_id = o.id

    await state.clear()
    await call.message.edit_text(
        f"<b>Оффер #{offer_id} создан и запущен!</b>\n\n"
        f"Покупатели увидят его в стакане.\n"
        f"Управление — в разделе «Мои объявления».",
        reply_markup=_kb([
            [_b("К моим объявлениям", "p2p:my_offers")],
            [_b("← Главное меню P2P", "p2p:menu")],
        ]),
    )
    await call.answer("Оффер создан", show_alert=False)

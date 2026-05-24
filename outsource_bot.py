"""Outsource bot (@marketplace_PRIDE_BOT) — лавка PRIDE для управляющих-аутсорсеров.

Управляющие платят взнос и берут ЛК банков под управление.
Основной flow:
1. /start → главное меню с 4 кнопками (Каталог / Баланс / Мои заказы / Профиль)
2. 📋 Каталог — список доступных ЛК в пуле (in_pool=True, не выкупленные)
3. 💲 Баланс — текущий wallet_balance_usdt + кнопки пополнить/снять
4. 🧾 Мои заказы — ЛК которые юзер выкупил (manager_username == его username)
5. 🎒 Профиль — общая статистика

Когда юзер жмёт "Взять под управление" на ЛК из каталога:
- Проверяем баланс >= list_price_usdt
- Списываем balance
- Устанавливаем manager_username = его username
- Снимаем in_pool=False
- Уведомляем
"""
import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

import config
from storage import storage

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# Главное меню (Reply Keyboard с цветами — Bot API 9.4+, aiogram 3.26+)
# style: "primary" (синий), "success" (зелёный), "danger" (красный)
# Старые клиенты увидят серые кнопки — graceful degradation.
# ════════════════════════════════════════════════════════════════
def _btn(text: str, style: str = "primary") -> KeyboardButton:
    """KeyboardButton с цветом. Если aiogram не знает style — пробуем через extra."""
    try:
        return KeyboardButton(text=text, style=style)
    except Exception:
        kb = KeyboardButton(text=text)
        try:
            kb.__pydantic_extra__ = (kb.__pydantic_extra__ or {})
            kb.__pydantic_extra__["style"] = style
        except Exception:
            pass
        return kb


def _ibtn(text: str, style: str = "primary", **kwargs) -> InlineKeyboardButton:
    """InlineKeyboardButton с цветом (Bot API 9.4+)."""
    try:
        return InlineKeyboardButton(text=text, style=style, **kwargs)
    except Exception:
        kb = InlineKeyboardButton(text=text, **kwargs)
        try:
            kb.__pydantic_extra__ = (kb.__pydantic_extra__ or {})
            kb.__pydantic_extra__["style"] = style
        except Exception:
            pass
        return kb


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [_btn("🪪 Каталог", "primary")],
        [_btn("💲 Баланс", "success")],
        [_btn("🧾 Мои заказы", "primary"), _btn("🎒 Профиль", "primary")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


router = Router(name="outsource_main")


def _username(message_or_call):
    """Универсальный — username юзера в lowercase."""
    u = message_or_call.from_user
    return (u.username or "").lower()


# ════════════════════════════════════════════════════════════════
# /start — регистрация юзера + меню
# ════════════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(message: Message):
    username = _username(message)
    if not username:
        await message.reply(
            "⚠️ У вас не задан @username в Telegram.\n\n"
            "Чтобы пользоваться ботом — установите username в настройках Telegram, "
            "затем напишите /start ещё раз.",
        )
        return
    await storage.register_outsource_manager(username=username, tg_user_id=message.from_user.id)
    name = message.from_user.first_name or "Управляющий"
    await message.reply(
        f"👋 <b>{name}, добро пожаловать в лавку PRIDE!</b>\n\n"
        f"Здесь вы можете брать ЛК банков под управление.\n\n"
        f"📋 <b>Каталог</b> — доступные ЛК\n"
        f"💲 <b>Баланс</b> — пополнить кошелёк\n"
        f"🧾 <b>Мои заказы</b> — ваши активные ЛК\n"
        f"🎒 <b>Профиль</b> — статистика\n\n"
        f"<i>Все списания и пополнения — в USDT (TRC20).</i>",
        reply_markup=MAIN_MENU,
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.reply(
        "📖 <b>Помощь по лавке PRIDE</b>\n\n"
        "/start — главное меню\n"
        "/balance — баланс кошелька\n"
        "/catalog — каталог доступных ЛК\n"
        "/myorders — мои выкупленные ЛК\n"
        "/profile — мой профиль\n\n"
        "По вопросам — пишите @SIMBA_PRIDE_ADM",
        reply_markup=MAIN_MENU,
    )


# ════════════════════════════════════════════════════════════════
# 📋 КАТАЛОГ — главное меню с подразделами Одиночки/Связки
# ════════════════════════════════════════════════════════════════
@router.message(F.text == "🪪 Каталог")
@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
    # Считаем что в каждом подразделе
    all_lks = storage.list_outsource_drop_lks() or {}
    bundles = storage.list_outsource_bundles() if hasattr(storage, "list_outsource_bundles") else {}
    singles_cnt = sum(
        1 for lk in all_lks.values()
        if lk.get("in_pool") and not lk.get("bundle_id")
    )
    bundles_cnt = sum(1 for b in bundles.values() if b.get("in_pool"))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text=f"🎴 Одиночки ({singles_cnt})", callback_data="cat:singles", style="primary")],
        [_ibtn(text=f"🔗 Связки ({bundles_cnt})", callback_data="cat:bundles", style="success")],
    ])
    await message.reply(
        "📋 <b>Каталог лавки PRIDE</b>\n\n"
        f"🎴 <b>Одиночки</b> — отдельные ЛК ({singles_cnt})\n"
        f"🔗 <b>Связки</b> — пакеты ЛК со скидкой ({bundles_cnt})\n\n"
        "<i>Выберите раздел.</i>",
        reply_markup=kb,
    )


@router.callback_query(F.data == "cat:singles")
async def cb_catalog_singles(call: CallbackQuery):
    await call.answer()
    pool_lks = []
    all_lks = storage.list_outsource_drop_lks() or {}
    all_drops = storage.list_outsource_drops() or {}
    for lkid, lk in all_lks.items():
        # Только одиночки: в пуле и не в связке
        if not lk.get("in_pool") or lk.get("bundle_id"):
            continue
        drop = all_drops.get(lk.get("outsource_drop_id"), {})
        pool_lks.append((lkid, lk, drop))
    pool_lks.sort(key=lambda x: -(x[1].get("listed_at") or 0))

    if not pool_lks:
        await call.message.reply(
            "🎴 <b>Одиночки — пусто</b>\n\nПока нет отдельных ЛК. Загляните в 🔗 Связки или позже.",
        )
        return

    lines = [f"🎴 <b>Одиночки — {len(pool_lks)} ЛК</b>\n"]
    rows = []
    for lkid, lk, drop in pool_lks[:20]:
        bank = lk.get("bank") or "—"
        fio = drop.get("fio") or "—"
        price = float(lk.get("list_price_usdt") or 0)
        lines.append(f"\n💼 <b>{bank}</b> · {fio} — <b>{price:.0f} USDT</b>")
        rows.append([_ibtn(
            text=f"💼 {bank} · {fio[:20]} — {price:.0f} USDT",
            callback_data=f"buy:{lkid}",
            style="success",
        )])
    if len(pool_lks) > 20:
        lines.append(f"\n<i>... и ещё {len(pool_lks) - 20}</i>")
    await call.message.reply(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "cat:bundles")
async def cb_catalog_bundles(call: CallbackQuery):
    await call.answer()
    bundles = storage.list_outsource_bundles() if hasattr(storage, "list_outsource_bundles") else {}
    all_lks = storage.list_outsource_drop_lks() or {}
    all_drops = storage.list_outsource_drops() or {}
    active = []
    for bid, b in bundles.items():
        if not b.get("in_pool"):
            continue
        active.append((bid, b))
    active.sort(key=lambda x: -(x[1].get("created_at") or 0))

    if not active:
        await call.message.reply(
            "🔗 <b>Связки — пусто</b>\n\nПока нет доступных связок. Загляните в 🎴 Одиночки или позже.",
        )
        return

    rows = []
    for bid, b in active[:15]:
        name = b.get("name") or f"Связка #{bid.replace('obnd','')}"
        price = float(b.get("list_price_usdt") or 0)
        # Сводка банков
        banks = []
        for lkid in b.get("lk_ids", [])[:5]:
            lk = all_lks.get(str(lkid)) or {}
            banks.append(lk.get("bank") or "?")
        banks_str = " + ".join(banks)
        if len(b.get("lk_ids", [])) > 5:
            banks_str += f" +{len(b['lk_ids']) - 5}"
        rows.append([_ibtn(
            text=f"🔗 {name} · {banks_str[:30]} — {price:.0f} USDT",
            callback_data=f"vbnd:{bid}",
            style="success",
        )])
    await call.message.reply(
        f"🔗 <b>Связки — {len(active)}</b>\n\n"
        "<i>Тапни на связку чтобы увидеть состав и забрать пакет целиком.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("vbnd:"))
async def cb_view_bundle(call: CallbackQuery):
    await call.answer()
    bundle_id = call.data.split(":", 1)[1]
    b = storage.get_outsource_bundle(bundle_id) if hasattr(storage, "get_outsource_bundle") else None
    if not b:
        await call.message.reply("Связка не найдена")
        return
    if not b.get("in_pool"):
        await call.message.reply("Связка уже куплена")
        return
    all_lks = storage.list_outsource_drop_lks() or {}
    all_drops = storage.list_outsource_drops() or {}
    name = b.get("name") or f"Связка #{bundle_id.replace('obnd','')}"
    price = float(b.get("list_price_usdt") or 0)
    lines = [f"🔗 <b>{name}</b>\n"]
    lines.append(f"📦 ЛК в пакете: <b>{len(b.get('lk_ids', []))}</b>")
    lines.append(f"💰 Цена за пакет: <b>{price:.0f} USDT</b>\n")
    lines.append("<b>Состав:</b>")
    for i, lkid in enumerate(b.get("lk_ids", []), 1):
        lk = all_lks.get(str(lkid)) or {}
        drop = all_drops.get(lk.get("outsource_drop_id")) or {}
        bank = lk.get("bank") or "—"
        fio = drop.get("fio") or "—"
        lines.append(f"  {i}. <b>{bank}</b> · {fio}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text=f"💎 Забрать связку — {price:.0f} USDT", callback_data=f"buybnd:{bundle_id}", style="success")],
        [_ibtn(text="◀ Назад к связкам", callback_data="cat:bundles", style="primary")],
    ])
    await call.message.reply("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("buybnd:"))
async def cb_buy_bundle(call: CallbackQuery):
    bundle_id = call.data.split(":", 1)[1]
    username = _username(call)
    if not username:
        await call.answer("Нужен @username в Telegram", show_alert=True)
        return
    b = storage.get_outsource_bundle(bundle_id) if hasattr(storage, "get_outsource_bundle") else None
    if not b:
        await call.answer("Связка не найдена", show_alert=True)
        return
    if not b.get("in_pool"):
        await call.answer("Связка уже куплена другим управляющим", show_alert=True)
        return
    price = float(b.get("list_price_usdt") or 0)
    await storage.register_outsource_manager(username=username, tg_user_id=call.from_user.id)
    mgr = storage.get_outsource_manager(username) or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    if balance < price:
        await call.answer(
            f"Недостаточно средств. Баланс: {balance:.2f} USDT, нужно: {price:.2f} USDT",
            show_alert=True,
        )
        return
    # Атомарная покупка
    result = await storage.buy_outsource_bundle(bundle_id, username) if hasattr(storage, "buy_outsource_bundle") else None
    if not result:
        await call.answer("Не удалось купить (возможно уже забрана)", show_alert=True)
        return
    new_balance = balance - price
    name = b.get("name") or f"Связка #{bundle_id.replace('obnd','')}"
    cnt = len(b.get("lk_ids", []))
    await call.answer(f"✅ Связка забрана! Списано {price:.0f} USDT", show_alert=True)
    await call.message.edit_text(
        f"✅ <b>{name} — взята под управление</b>\n\n"
        f"📦 ЛК в пакете: <b>{cnt}</b>\n"
        f"💸 Списано: <b>{price:.0f} USDT</b>\n"
        f"💼 Остаток: <b>{new_balance:.2f} USDT</b>\n\n"
        f"Все карточки уже в разделе «🧾 Мои заказы».",
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_lk(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    username = _username(call)
    if not username:
        await call.answer("Нужен @username в Telegram", show_alert=True)
        return
    lk = storage.get_outsource_drop_lk(droplk_id)
    if not lk:
        await call.answer("ЛК не найден или уже забран", show_alert=True)
        return
    if not lk.get("in_pool"):
        await call.answer("ЛК уже забран другим управляющим", show_alert=True)
        return
    price = float(lk.get("list_price_usdt") or 0)
    mgr = storage.get_outsource_manager(username) or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    if balance < price:
        await call.answer(
            f"Недостаточно средств. На балансе: {balance:.2f} USDT, нужно: {price:.2f} USDT",
            show_alert=True,
        )
        return
    # Списываем + забираем из пула
    await storage.register_outsource_manager(username=username, tg_user_id=call.from_user.id)
    # Атомарно обновляем баланс через helper
    updated = await storage.update_outsource_manager_balance(
        username=username, delta=-price, paid_delta=price,
    )
    new_balance = float((updated or {}).get("wallet_balance_usdt") or 0)
    # Забираем ЛК из пула
    import time as _time
    await storage.update_outsource_drop_lk(
        droplk_id,
        manager_username=username,
        in_pool=False,
        bought_at=_time.time(),
        bought_by=username,
    )
    await call.answer(f"✅ ЛК забран! Списано {price:.2f} USDT", show_alert=True)
    drop = storage.get_outsource_drop(lk.get("outsource_drop_id")) or {}
    bank = lk.get("bank") or "—"
    fio = drop.get("fio") or "—"
    await call.message.edit_text(
        f"✅ <b>ЛК {bank} · {fio} — взят под управление</b>\n\n"
        f"Списано: <b>{price:.2f} USDT</b>\n"
        f"Остаток на балансе: <b>{new_balance:.2f} USDT</b>\n\n"
        f"Карточка появилась в разделе «🧾 Мои заказы».",
    )


# ════════════════════════════════════════════════════════════════
# 💲 БАЛАНС
# ════════════════════════════════════════════════════════════════
@router.message(F.text == "💲 Баланс")
@router.message(Command("balance"))
async def cmd_balance(message: Message):
    username = _username(message)
    if not username:
        await message.reply("Нужен @username в Telegram")
        return
    mgr = storage.get_outsource_manager(username) or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    paid = float(mgr.get("paid_total_usdt") or 0)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text="➕ Пополнить (USDT TRC20)", callback_data="topup", style="success")],
        [_ibtn(text="📤 Запросить вывод", callback_data="withdraw", style="danger")],
        [_ibtn(text="📜 История операций", callback_data="history", style="primary")],
    ])
    await message.reply(
        f"💼 <b>Ваш баланс</b>\n\n"
        f"Доступно: <b>{balance:.2f} USDT</b>\n"
        f"Всего потрачено: {paid:.2f} USDT\n\n"
        f"<i>USDT TRC20 — единственная поддерживаемая сеть.</i>",
        reply_markup=kb,
    )


class TopUpFSM(StatesGroup):
    waiting_amount = State()


@router.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery, state: FSMContext):
    await call.answer()
    wallet = storage.get_outsource_corp_wallet() if hasattr(storage, "get_outsource_corp_wallet") else ""
    if not wallet:
        await call.message.reply(
            "⚠️ <b>Пополнение временно недоступно</b>\n\n"
            "Корп-кошелёк не настроен. Напишите @SIMBA_PRIDE_ADM.",
        )
        return
    await state.set_state(TopUpFSM.waiting_amount)
    await call.message.reply(
        "➕ <b>Пополнение баланса (USDT TRC20)</b>\n\n"
        "Введите сумму которую хотите пополнить (USDT, минимум 10):\n\n"
        "<i>Например: <code>100</code> или <code>250</code></i>\n\n"
        "Для отмены — /cancel",
    )


@router.message(TopUpFSM.waiting_amount, Command("cancel"))
async def cmd_topup_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Пополнение отменено.", reply_markup=MAIN_MENU)


@router.message(TopUpFSM.waiting_amount, F.text)
async def msg_topup_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    # Игнорируем нажатия кнопок меню — выходим
    if text in ("🪪 Каталог", "💲 Баланс", "🧾 Мои заказы", "🎒 Профиль"):
        await state.clear()
        return  # дальше обработают другие хендлеры
    try:
        base = float(text)
    except Exception:
        await message.reply("Введите число. Например: <code>100</code>")
        return
    if base < 10:
        await message.reply("Минимальная сумма пополнения — <b>10 USDT</b>. Попробуйте ещё раз:")
        return
    if base > 100000:
        await message.reply("Максимум <b>100 000 USDT</b> за раз. Попробуйте ещё раз:")
        return
    username = _username(message)
    if not username:
        await message.reply("Нужен @username в Telegram.")
        await state.clear()
        return
    # Регистрируем юзера если ещё не
    await storage.register_outsource_manager(username=username, tg_user_id=message.from_user.id)
    # Создаём запрос с уникальной суммой
    req = await storage.create_outsource_topup_request(
        username=username, base_amount=base, ttl_seconds=1800,  # 30 мин
    )
    if not req:
        await message.reply("⚠️ Не удалось создать запрос. Попробуйте позже.")
        await state.clear()
        return
    await state.clear()
    wallet = storage.get_outsource_corp_wallet()
    unique = float(req.get("unique_amount") or 0)
    expires_in = 30  # min
    await message.reply(
        f"💰 <b>Заявка на пополнение #{req['id']}</b>\n\n"
        f"Отправьте РОВНО эту сумму:\n"
        f"💎 <b>{unique:.4f} USDT</b>\n\n"
        f"На адрес (USDT TRC20):\n"
        f"<code>{wallet}</code>\n\n"
        f"<b>ВАЖНО:</b>\n"
        f"• Сеть: <b>TRC20</b> (не ERC20!)\n"
        f"• Сумма должна совпадать ДО 4 знаков после запятой\n"
        f"• Зачислится база — <b>{base:.2f} USDT</b>\n"
        f"• Зачисление автоматическое (~30-60 сек после подтверждения сети)\n"
        f"• Срок действия: <b>{expires_in} мин</b>\n\n"
        f"<i>Я пришлю уведомление как только увижу транзакцию.</i>",
        reply_markup=MAIN_MENU,
    )


@router.callback_query(F.data == "withdraw")
async def cb_withdraw(call: CallbackQuery):
    await call.answer()
    await call.message.reply(
        "📤 <b>Запрос вывода</b>\n\n"
        "Напишите админу <b>@SIMBA_PRIDE_ADM</b> с указанием:\n"
        "• Сумма для вывода (USDT)\n"
        "• Ваш USDT TRC20 адрес\n\n"
        "<i>Вывод обрабатывается в течение 24 часов.</i>",
    )


@router.callback_query(F.data == "history")
async def cb_history(call: CallbackQuery):
    await call.answer("История операций — скоро будет 🚧")


# ════════════════════════════════════════════════════════════════
# 🧾 МОИ ЗАКАЗЫ
# ════════════════════════════════════════════════════════════════
@router.message(F.text == "🧾 Мои заказы")
@router.message(Command("myorders"))
async def cmd_myorders(message: Message):
    username = _username(message)
    if not username:
        await message.reply("Нужен @username в Telegram")
        return
    my_lks = []
    all_lks = storage.list_outsource_drop_lks() or {}
    all_drops = storage.list_outsource_drops() or {}
    for lkid, lk in all_lks.items():
        if (lk.get("manager_username") or "") == username and not lk.get("in_pool"):
            drop = all_drops.get(lk.get("outsource_drop_id"), {})
            my_lks.append((lkid, lk, drop))
    my_lks.sort(key=lambda x: -(x[1].get("bought_at") or 0))

    if not my_lks:
        await message.reply(
            "🧾 <b>У вас пока нет активных ЛК</b>\n\n"
            "Загляните в 📋 Каталог чтобы взять ЛК под управление.",
            reply_markup=MAIN_MENU,
        )
        return

    lines = [f"🧾 <b>Ваши ЛК — {len(my_lks)} шт.</b>\n"]
    for lkid, lk, drop in my_lks[:30]:
        bank = lk.get("bank") or "—"
        fio = drop.get("fio") or "—"
        price = float(lk.get("list_price_usdt") or 0)
        login = lk.get("new_login") or "—"
        password = lk.get("new_password") or "—"
        lines.append(
            f"\n💼 <b>{bank}</b> · {fio}\n"
            f"   Логин: <code>{login}</code>\n"
            f"   Пароль: <code>{password}</code>\n"
            f"   Цена: {price:.0f} USDT · <code>{lkid}</code>"
        )
    await message.reply("\n".join(lines), reply_markup=MAIN_MENU)


# ════════════════════════════════════════════════════════════════
# 🎒 ПРОФИЛЬ
# ════════════════════════════════════════════════════════════════
@router.message(F.text == "🎒 Профиль")
@router.message(Command("profile"))
async def cmd_profile(message: Message):
    username = _username(message)
    if not username:
        await message.reply("Нужен @username в Telegram")
        return
    mgr = storage.get_outsource_manager(username) or {}
    stats = mgr.get("stats") or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    paid = float(mgr.get("paid_total_usdt") or 0)
    first_seen = mgr.get("first_seen_ts") or 0
    import time
    days = int((time.time() - first_seen) / 86400) if first_seen else 0
    await message.reply(
        f"🎒 <b>Ваш профиль</b>\n\n"
        f"👤 @{username}\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"📅 В лавке: {days} дн.\n\n"
        f"💼 Баланс: <b>{balance:.2f} USDT</b>\n"
        f"💸 Всего потрачено: {paid:.2f} USDT\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"   • Анкет всего: {stats.get('drops_total', 0)}\n"
        f"   • ЛК всего: {stats.get('lks_total', 0)}\n"
        f"   • ЛК завершено: {stats.get('lks_done', 0)}\n",
        reply_markup=MAIN_MENU,
    )


# ════════════════════════════════════════════════════════════════
# Запуск
# ════════════════════════════════════════════════════════════════
# Глобальный экземпляр (для tron_monitor чтобы слать уведомления юзерам)
_outsource_bot_instance: "Bot | None" = None


def get_outsource_bot():
    """Возвращает текущий экземпляр @marketplace_PRIDE_BOT (или None если не запущен)."""
    return _outsource_bot_instance


async def run_outsource_bot():
    """Запускается из bot.py параллельно с основными ботами."""
    global _outsource_bot_instance
    if not config.OUTSOURCE_BOT_TOKEN:
        logger.warning("OUTSOURCE_BOT_TOKEN не задан — outsource_bot не запущен")
        return
    bot = Bot(
        token=config.OUTSOURCE_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    _outsource_bot_instance = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Outsource bot starting (long polling)...")
    try:
        me = await bot.get_me()
        logger.info("Outsource bot logged in as @%s", me.username)
    except Exception as e:
        logger.error("Outsource bot get_me failed: %s", e)
        return
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error("Outsource bot polling failed: %s", e)

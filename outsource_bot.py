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
# 📋 КАТАЛОГ
# ════════════════════════════════════════════════════════════════
@router.message(F.text == "🪪 Каталог")
@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
    pool_lks = []
    all_lks = storage.list_outsource_drop_lks() or {}
    all_drops = storage.list_outsource_drops() or {}
    for lkid, lk in all_lks.items():
        if not lk.get("in_pool"):
            continue
        drop = all_drops.get(lk.get("outsource_drop_id"), {})
        pool_lks.append((lkid, lk, drop))
    pool_lks.sort(key=lambda x: -(x[1].get("listed_at") or 0))

    if not pool_lks:
        await message.reply(
            "📋 <b>Каталог пуст</b>\n\nПока нет доступных ЛК для управления. Загляните позже!",
            reply_markup=MAIN_MENU,
        )
        return

    lines = [f"📋 <b>Каталог — {len(pool_lks)} ЛК</b>\n"]
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
    await message.reply(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
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


@router.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery):
    await call.answer()
    await call.message.reply(
        "➕ <b>Пополнение баланса</b>\n\n"
        "Чтобы пополнить — переведите USDT TRC20 на адрес администратора и "
        "пришлите ему скриншот / hash транзакции:\n\n"
        "👤 <b>@SIMBA_PRIDE_ADM</b>\n\n"
        "<i>После подтверждения админом баланс пополнится автоматически.</i>",
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
async def run_outsource_bot():
    """Запускается из bot.py параллельно с основными ботами."""
    if not config.OUTSOURCE_BOT_TOKEN:
        logger.warning("OUTSOURCE_BOT_TOKEN не задан — outsource_bot не запущен")
        return
    bot = Bot(
        token=config.OUTSOURCE_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
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

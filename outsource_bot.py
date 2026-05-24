"""Outsource bot (@marketplace_PRIDE_BOT) — лавка PRIDE для управляющих-аутсорсеров.

Все тексты и лейблы кнопок РЕДАКТИРУЕМЫЕ через JARVIS Settings → 💎 Оплата → Тексты бота.
Дефолты в outsource_bot_texts.DEFAULT_TEXTS, override в storage.outsource_bot_texts.

Flow:
- /start или текст btn_catalog → главное меню
- 🪪 Каталог → выбор Одиночки / Связки
- 💲 Баланс → инфо + кнопки Пополнить / Запросить вывод (с автопоказом Условий оплаты)
- 📋 Условия → Условия покупки / Условия оплаты
- Покупка ЛК → ПРЕДВАРИТЕЛЬНО показ Условий покупки → Согласен → оплата
"""
import logging
import time as _time

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
    ReplyKeyboardMarkup, KeyboardButton,
)

import config
from storage import storage
from outsource_bot_texts import DEFAULT_TEXTS, render_text

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Helpers: тексты + кнопки с цветами (Bot API 9.4+)
# ════════════════════════════════════════════════════════════════
def T(key: str, **vars) -> str:
    """Возвращает текст из storage с подстановкой переменных. Если ключ не найден — пустая строка."""
    template = storage.get_outsource_text(key, DEFAULT_TEXTS.get(key, ""))
    return render_text(template, **vars)


def _btn(text: str, style: str = "primary") -> KeyboardButton:
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


def build_main_menu() -> ReplyKeyboardMarkup:
    """Строит главное меню с актуальными лейблами из storage."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [_btn(T("btn_catalog"), "primary")],
            [_btn(T("btn_balance"), "success")],
            [_btn(T("btn_myorders"), "primary"), _btn(T("btn_profile"), "primary")],
            [_btn(T("btn_terms"), "primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _is_btn(key: str):
    """Фабрика динамического фильтра: matches message.text against current text setting."""
    def check(text):
        if not text:
            return False
        return text == storage.get_outsource_text(key, DEFAULT_TEXTS.get(key, ""))
    return check


router = Router(name="outsource_main")


def _username(message_or_call):
    u = message_or_call.from_user
    return (u.username or "").lower()


# ════════════════════════════════════════════════════════════════
# /start
# ════════════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(message: Message):
    username = _username(message)
    if not username:
        await message.reply(T("no_username"))
        return
    await storage.register_outsource_manager(username=username, tg_user_id=message.from_user.id)
    name = message.from_user.first_name or "Управляющий"
    await message.reply(T("start_welcome", name=name), reply_markup=build_main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.reply(T("help"))


# ════════════════════════════════════════════════════════════════
# 📋 КАТАЛОГ
# ════════════════════════════════════════════════════════════════
@router.message(F.text.func(_is_btn("btn_catalog")))
@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
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
        T("catalog_header", singles_cnt=singles_cnt, bundles_cnt=bundles_cnt),
        reply_markup=kb,
    )


@router.callback_query(F.data == "cat:singles")
async def cb_catalog_singles(call: CallbackQuery):
    await call.answer()
    pool_lks = []
    all_lks = storage.list_outsource_drop_lks() or {}
    all_drops = storage.list_outsource_drops() or {}
    for lkid, lk in all_lks.items():
        if not lk.get("in_pool") or lk.get("bundle_id"):
            continue
        drop = all_drops.get(lk.get("outsource_drop_id"), {})
        pool_lks.append((lkid, lk, drop))
    pool_lks.sort(key=lambda x: -(x[1].get("listed_at") or 0))
    if not pool_lks:
        await call.message.reply(T("catalog_singles_empty"))
        return
    lines = [T("catalog_singles_header", count=len(pool_lks))]
    rows = []
    for lkid, lk, drop in pool_lks[:20]:
        bank = lk.get("bank") or "—"
        fio = drop.get("fio") or "—"
        price = float(lk.get("list_price_usdt") or 0)
        lines.append(f"\n💼 <b>{bank}</b> · {fio} — <b>{price:.0f} USDT</b>")
        # show_terms_first → отдельный callback который потом ведёт на реальную покупку
        rows.append([_ibtn(
            text=f"💼 {bank} · {fio[:20]} — {price:.0f} USDT",
            callback_data=f"buyask:{lkid}",
            style="success",
        )])
    if len(pool_lks) > 20:
        lines.append(f"\n<i>... и ещё {len(pool_lks) - 20}</i>")
    await call.message.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "cat:bundles")
async def cb_catalog_bundles(call: CallbackQuery):
    await call.answer()
    bundles = storage.list_outsource_bundles() if hasattr(storage, "list_outsource_bundles") else {}
    all_lks = storage.list_outsource_drop_lks() or {}
    active = [(bid, b) for bid, b in bundles.items() if b.get("in_pool")]
    active.sort(key=lambda x: -(x[1].get("created_at") or 0))
    if not active:
        await call.message.reply(T("catalog_bundles_empty"))
        return
    rows = []
    for bid, b in active[:15]:
        name = b.get("name") or f"Связка #{bid.replace('obnd','')}"
        price = float(b.get("list_price_usdt") or 0)
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
        T("catalog_bundles_header", count=len(active)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("vbnd:"))
async def cb_view_bundle(call: CallbackQuery):
    await call.answer()
    bundle_id = call.data.split(":", 1)[1]
    b = storage.get_outsource_bundle(bundle_id) if hasattr(storage, "get_outsource_bundle") else None
    if not b:
        await call.message.reply(T("bundle_not_found"))
        return
    if not b.get("in_pool"):
        await call.message.reply(T("bundle_taken"))
        return
    all_lks = storage.list_outsource_drop_lks() or {}
    all_drops = storage.list_outsource_drops() or {}
    name = b.get("name") or f"Связка #{bundle_id.replace('obnd','')}"
    price = float(b.get("list_price_usdt") or 0)
    lines = [T("bundle_view_text", name=name, count=len(b.get("lk_ids", [])), price=price)]
    for i, lkid in enumerate(b.get("lk_ids", []), 1):
        lk = all_lks.get(str(lkid)) or {}
        drop = all_drops.get(lk.get("outsource_drop_id")) or {}
        bank = lk.get("bank") or "—"
        fio = drop.get("fio") or "—"
        lines.append(f"  {i}. <b>{bank}</b> · {fio}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text=f"💎 Забрать связку — {price:.0f} USDT", callback_data=f"bndask:{bundle_id}", style="success")],
        [_ibtn(text="◀ Назад к связкам", callback_data="cat:bundles", style="primary")],
    ])
    await call.message.reply("\n".join(lines), reply_markup=kb)


# ════════════════════════════════════════════════════════════════
# 📋 УСЛОВИЯ — отдельная кнопка + автопоказ перед покупкой/оплатой
# ════════════════════════════════════════════════════════════════
@router.message(F.text.func(_is_btn("btn_terms")))
@router.message(Command("terms"))
async def cmd_terms(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text="📋 Условия покупки", callback_data="terms:purchase", style="primary")],
        [_ibtn(text="💰 Условия оплаты", callback_data="terms:payment", style="primary")],
    ])
    await message.reply(T("terms_menu"), reply_markup=kb)


@router.callback_query(F.data == "terms:purchase")
async def cb_terms_purchase(call: CallbackQuery):
    await call.answer()
    await call.message.reply(T("terms_purchase"))


@router.callback_query(F.data == "terms:payment")
async def cb_terms_payment(call: CallbackQuery):
    await call.answer()
    await call.message.reply(T("terms_payment"))


# === Автопоказ перед действиями (покупка / пополнение) ===
@router.callback_query(F.data.startswith("buyask:"))
async def cb_buyask(call: CallbackQuery):
    """Показывает Условия покупки перед фактической покупкой ЛК."""
    await call.answer()
    droplk_id = call.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text=T("terms_agree_btn"), callback_data=f"buy:{droplk_id}", style="success")],
        [_ibtn(text=T("terms_decline_btn"), callback_data="cancel:buy", style="danger")],
    ])
    await call.message.reply(T("terms_purchase"), reply_markup=kb)


@router.callback_query(F.data.startswith("bndask:"))
async def cb_bndask(call: CallbackQuery):
    """Показывает Условия покупки перед фактической покупкой СВЯЗКИ."""
    await call.answer()
    bundle_id = call.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text=T("terms_agree_btn"), callback_data=f"buybnd:{bundle_id}", style="success")],
        [_ibtn(text=T("terms_decline_btn"), callback_data="cancel:buy", style="danger")],
    ])
    await call.message.reply(T("terms_purchase"), reply_markup=kb)


@router.callback_query(F.data == "cancel:buy")
async def cb_cancel_buy(call: CallbackQuery):
    await call.answer()
    try:
        await call.message.edit_text(T("terms_declined"))
    except Exception:
        await call.message.reply(T("terms_declined"))


@router.callback_query(F.data == "cancel:topup")
async def cb_cancel_topup(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    try:
        await call.message.edit_text(T("terms_declined"))
    except Exception:
        await call.message.reply(T("terms_declined"))


# ════════════════════════════════════════════════════════════════
# Покупка одиночки (после согласия с Условиями)
# ════════════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_lk(call: CallbackQuery):
    droplk_id = call.data.split(":", 1)[1]
    username = _username(call)
    if not username:
        await call.answer("Нужен @username в Telegram", show_alert=True)
        return
    lk = storage.get_outsource_drop_lk(droplk_id)
    if not lk:
        await call.answer(T("buy_not_found"), show_alert=True)
        return
    if not lk.get("in_pool"):
        await call.answer(T("buy_taken"), show_alert=True)
        return
    price = float(lk.get("list_price_usdt") or 0)
    mgr = storage.get_outsource_manager(username) or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    if balance < price:
        await call.answer(
            T("buy_no_funds_alert", balance=balance, price=price),
            show_alert=True,
        )
        return
    await storage.register_outsource_manager(username=username, tg_user_id=call.from_user.id)
    updated = await storage.update_outsource_manager_balance(
        username=username, delta=-price, paid_delta=price,
    )
    new_balance = float((updated or {}).get("wallet_balance_usdt") or 0)
    await storage.update_outsource_drop_lk(
        droplk_id,
        manager_username=username,
        in_pool=False,
        bought_at=_time.time(),
        bought_by=username,
    )
    await call.answer(T("buy_success_alert", price=price), show_alert=True)
    drop = storage.get_outsource_drop(lk.get("outsource_drop_id")) or {}
    bank = lk.get("bank") or "—"
    fio = drop.get("fio") or "—"
    await call.message.edit_text(T(
        "buy_success_message", bank=bank, fio=fio, price=price, new_balance=new_balance,
    ))


# ════════════════════════════════════════════════════════════════
# Покупка связки (после согласия с Условиями)
# ════════════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("buybnd:"))
async def cb_buy_bundle(call: CallbackQuery):
    bundle_id = call.data.split(":", 1)[1]
    username = _username(call)
    if not username:
        await call.answer("Нужен @username в Telegram", show_alert=True)
        return
    b = storage.get_outsource_bundle(bundle_id) if hasattr(storage, "get_outsource_bundle") else None
    if not b:
        await call.answer(T("bundle_not_found"), show_alert=True)
        return
    if not b.get("in_pool"):
        await call.answer(T("bundle_taken"), show_alert=True)
        return
    price = float(b.get("list_price_usdt") or 0)
    await storage.register_outsource_manager(username=username, tg_user_id=call.from_user.id)
    mgr = storage.get_outsource_manager(username) or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    if balance < price:
        await call.answer(
            T("buy_no_funds_alert", balance=balance, price=price),
            show_alert=True,
        )
        return
    result = await storage.buy_outsource_bundle(bundle_id, username) if hasattr(storage, "buy_outsource_bundle") else None
    if not result:
        await call.answer("Не удалось купить (возможно уже забрана)", show_alert=True)
        return
    new_balance = balance - price
    name = b.get("name") or f"Связка #{bundle_id.replace('obnd','')}"
    cnt = len(b.get("lk_ids", []))
    await call.answer(T("bundle_buy_success_alert", price=price), show_alert=True)
    await call.message.edit_text(T(
        "bundle_buy_success_message", name=name, count=cnt, price=price, new_balance=new_balance,
    ))


# ════════════════════════════════════════════════════════════════
# 💲 БАЛАНС
# ════════════════════════════════════════════════════════════════
@router.message(F.text.func(_is_btn("btn_balance")))
@router.message(Command("balance"))
async def cmd_balance(message: Message):
    username = _username(message)
    if not username:
        await message.reply(T("no_username"))
        return
    mgr = storage.get_outsource_manager(username) or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    paid = float(mgr.get("paid_total_usdt") or 0)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text="➕ Пополнить (USDT TRC20)", callback_data="topupask", style="success")],
        [_ibtn(text="📤 Запросить вывод", callback_data="withdraw", style="danger")],
        [_ibtn(text="📜 История операций", callback_data="history", style="primary")],
    ])
    await message.reply(
        T("balance_header", balance=balance, paid=paid),
        reply_markup=kb,
    )


# === Автопоказ Условий оплаты перед top-up flow ===
@router.callback_query(F.data == "topupask")
async def cb_topup_ask(call: CallbackQuery):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_ibtn(text=T("terms_agree_btn"), callback_data="topup", style="success")],
        [_ibtn(text=T("terms_decline_btn"), callback_data="cancel:topup", style="danger")],
    ])
    await call.message.reply(T("terms_payment"), reply_markup=kb)


class TopUpFSM(StatesGroup):
    waiting_amount = State()


@router.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery, state: FSMContext):
    await call.answer()
    wallet = storage.get_outsource_corp_wallet() if hasattr(storage, "get_outsource_corp_wallet") else ""
    if not wallet:
        await call.message.reply(T("topup_no_wallet"))
        return
    await state.set_state(TopUpFSM.waiting_amount)
    await call.message.reply(T("topup_ask_amount"))


@router.message(TopUpFSM.waiting_amount, Command("cancel"))
async def cmd_topup_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply(T("terms_declined"), reply_markup=build_main_menu())


@router.message(TopUpFSM.waiting_amount, F.text)
async def msg_topup_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    # Игнорируем нажатия кнопок меню — выходим из FSM
    menu_texts = {
        T("btn_catalog"), T("btn_balance"), T("btn_myorders"),
        T("btn_profile"), T("btn_terms"),
    }
    if text in menu_texts:
        await state.clear()
        return
    try:
        base = float(text)
    except Exception:
        await message.reply(T("topup_amount_invalid"))
        return
    if base < 10:
        await message.reply(T("topup_amount_too_small"))
        return
    if base > 100000:
        await message.reply(T("topup_amount_too_large"))
        return
    username = _username(message)
    if not username:
        await message.reply(T("no_username"))
        await state.clear()
        return
    await storage.register_outsource_manager(username=username, tg_user_id=message.from_user.id)
    req = await storage.create_outsource_topup_request(
        username=username, base_amount=base, ttl_seconds=1800,
    )
    if not req:
        await message.reply(T("topup_create_failed"))
        await state.clear()
        return
    await state.clear()
    wallet = storage.get_outsource_corp_wallet()
    unique = float(req.get("unique_amount") or 0)
    await message.reply(
        T("topup_instructions",
          id=req["id"], unique=unique, wallet=wallet,
          base=base, expires_min=30),
        reply_markup=build_main_menu(),
    )


@router.callback_query(F.data == "withdraw")
async def cb_withdraw(call: CallbackQuery):
    await call.answer()
    await call.message.reply(T("withdraw_message"))


@router.callback_query(F.data == "history")
async def cb_history(call: CallbackQuery):
    await call.answer(T("history_coming"))


# ════════════════════════════════════════════════════════════════
# 🧾 МОИ ЗАКАЗЫ
# ════════════════════════════════════════════════════════════════
@router.message(F.text.func(_is_btn("btn_myorders")))
@router.message(Command("myorders"))
async def cmd_myorders(message: Message):
    username = _username(message)
    if not username:
        await message.reply(T("no_username"))
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
        await message.reply(T("myorders_empty"), reply_markup=build_main_menu())
        return
    lines = [T("myorders_header", count=len(my_lks))]
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
    await message.reply("\n".join(lines), reply_markup=build_main_menu())


# ════════════════════════════════════════════════════════════════
# 🎒 ПРОФИЛЬ
# ════════════════════════════════════════════════════════════════
@router.message(F.text.func(_is_btn("btn_profile")))
@router.message(Command("profile"))
async def cmd_profile(message: Message):
    username = _username(message)
    if not username:
        await message.reply(T("no_username"))
        return
    mgr = storage.get_outsource_manager(username) or {}
    stats = mgr.get("stats") or {}
    balance = float(mgr.get("wallet_balance_usdt") or 0)
    paid = float(mgr.get("paid_total_usdt") or 0)
    first_seen = mgr.get("first_seen_ts") or 0
    days = int((_time.time() - first_seen) / 86400) if first_seen else 0
    await message.reply(
        T("profile_text",
          username=username, tg_id=message.from_user.id, days=days,
          balance=balance, paid=paid,
          drops_total=stats.get("drops_total", 0),
          lks_total=stats.get("lks_total", 0),
          lks_done=stats.get("lks_done", 0)),
        reply_markup=build_main_menu(),
    )


# ════════════════════════════════════════════════════════════════
# Запуск
# ════════════════════════════════════════════════════════════════
_outsource_bot_instance = None  # type: ignore


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

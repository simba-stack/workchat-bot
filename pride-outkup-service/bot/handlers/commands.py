"""Bot UI без emoji-мусора — минималистично.

Только цветные иконки крипты остаются (Telegram custom emoji через unicode).
Все остальные emoji убраны — текстовые лейблы.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, WebAppInfo,
)
from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.models import User

router = Router(name="commands")
logger = logging.getLogger(__name__)


def _miniapp_url(view: str = "") -> str:
    url = f"{settings.miniapp_url}{settings.miniapp_path}"
    return f"{url}?view={view}" if view else url


def _wa(label: str, view: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, web_app=WebAppInfo(url=_miniapp_url(view)))


def _cb(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=data)


# ═══════════════════ /start — главное меню ══════════════════════════
@router.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "<b>PRIDE P2P</b>\n\n"
        "Мультивалютный криптокошелёк. Покупайте, продавайте, храните, "
        "отправляйте и платите криптовалютой, когда хотите."
    )
    await message.answer(text, reply_markup=_main_kb(), disable_web_page_preview=True)


def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_wa("Кошелёк"),               _cb("Обмен",      "main:swap")],
        [_cb("P2P",  "main:p2p"),      _cb("Биржа",      "main:exchange")],
        [_cb("Чеки", "main:checks"),   _cb("Счета",      "main:invoices")],
        [_cb("Crypto Pay", "main:pay"), _cb("Розыгрыши", "main:giveaway")],
        [_cb("Подписки", "main:subs"), _cb("Настройки",  "main:settings")],
    ])


@router.callback_query(F.data == "main:back_to_start")
async def cb_back_to_start(call: CallbackQuery):
    text = (
        "<b>PRIDE P2P</b>\n\n"
        "Мультивалютный криптокошелёк."
    )
    try:
        await call.message.edit_text(text, reply_markup=_main_kb(), disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=_main_kb(), disable_web_page_preview=True)
    await call.answer()


# ═══════════════════ /wallet ════════════════════════════════════════
async def _build_wallet_text(tg_id: int) -> str:
    from core.services import balance_service, rates_service
    from core.models import Coin

    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if not u:
            return "<b>Кошелёк</b>\n\nСначала /start"
        balances = await balance_service.list_balances(db, u.id)
        # Legacy USDT fallback
        legacy_usdt = Decimal(str(u.balance_usdt or 0))
        if legacy_usdt > 0 and Decimal(str(balances.get("USDT", 0) or 0)) == 0:
            balances["USDT"] = legacy_usdt
        coins = (await db.execute(select(Coin).order_by(Coin.sort_order))).scalars().all()
        rates = await rates_service.get_rates()

    lines = ["<b>Кошелёк</b>", ""]
    total_usd = Decimal("0")
    for c in coins:
        bal = Decimal(str(balances.get(c.code, 0) or 0))
        if c.code in ("USDT", "USDC"):
            usd = bal
        else:
            r = rates.get(c.coingecko_id) if c.coingecko_id else None
            price = Decimal(str(r.get("usd", 0))) if r else Decimal("0")
            usd = bal * price
        total_usd += usd
        if bal == 0:
            lines.append(f"<b>{c.name}</b>: 0 {c.code}")
        else:
            amt_str = f"{float(bal):.6f}".rstrip("0").rstrip(".") if bal < 1 else f"{float(bal):.4f}"
            usd_str = f" (${float(usd):.2f})" if usd > 0 else ""
            lines.append(f"<b>{c.name}</b>: {amt_str} {c.code}{usd_str}")
    lines.append("")
    lines.append(f"Итого: <b>${float(total_usd):.2f}</b>")
    return "\n".join(lines)


@router.message(Command("wallet"))
async def cmd_wallet(message: Message):
    text = await _build_wallet_text(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_wa("Открыть кошелёк")],
        [_wa("Пополнить", "deposit"), _wa("Вывести", "withdraw")],
        [_cb("Назад", "main:back_to_start")],
    ])
    await message.answer(text, reply_markup=kb)


# ═══════════════════ /p2p ═══════════════════════════════════════════
@router.message(Command("p2p"))
async def cmd_p2p(message: Message):
    await _show_p2p(message)


@router.callback_query(F.data == "main:p2p")
async def cb_p2p(call: CallbackQuery):
    await _show_p2p(call.message, edit=True)
    await call.answer()


async def _show_p2p(target: Message, edit: bool = False):
    text = (
        "<b>P2P Маркет</b>\n\n"
        "Здесь вы можете <a href=\"#\">купить</a> или <a href=\"#\">продать</a> "
        "криптовалюту переводом на карту или электронный кошелёк."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_cb("Купить",  "p2p:buy"),       _cb("Продать", "p2p:sell")],
        [_cb("Мои сделки",        "p2p:my_deals")],
        [_cb("Создать объявление", "p2p:create")],
        [_cb("Оплата и валюта",    "p2p:settings")],
        [_cb("Мой профиль",        "p2p:profile")],
        [_cb("Назад", "main:back_to_start")],
    ])
    if edit:
        try: await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True); return
        except Exception: pass
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("p2p:"))
async def cb_p2p_sub(call: CallbackQuery):
    action = call.data.split(":", 1)[1]
    if action == "buy":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_cb("Tether (USDT) · 74₽ · 45",       "p2p:coin:USDT:buy")],
            [_cb("Toncoin (TON) · 140₽ · 39",      "p2p:coin:TON:buy")],
            [_cb("Solana (SOL) · 9 303₽ · 5",      "p2p:coin:SOL:buy")],
            [_cb("TRON (TRX) · 36.88₽ · 8",        "p2p:coin:TRX:buy")],
            [_cb("Bitcoin (BTC) · 7 651 834₽ · 3", "p2p:coin:BTC:buy")],
            [_cb("Ethereum (ETH) · 215 882₽ · 3",  "p2p:coin:ETH:buy")],
            [_cb("Dogecoin (DOGE) · 8.89₽ · 3",    "p2p:coin:DOGE:buy")],
            [_cb("Litecoin (LTC) · 4 000₽ · 6",    "p2p:coin:LTC:buy")],
            [_cb("Binance Coin (BNB) · 162 900₽ · 2", "p2p:coin:BNB:buy")],
            [_cb("Назад в P2P Маркет", "main:p2p")],
        ])
        await call.message.edit_text("Выберите криптовалюту, которую вы хотите купить.", reply_markup=kb)
    elif action == "sell":
        await call.message.edit_text(
            "Выберите криптовалюту, которую вы хотите продать.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_cb("Tether (USDT)", "p2p:coin:USDT:sell")],
                [_cb("Toncoin (TON)", "p2p:coin:TON:sell")],
                [_cb("TRON (TRX)",    "p2p:coin:TRX:sell")],
                [_cb("Назад в P2P Маркет", "main:p2p")],
            ]),
        )
    elif action == "my_deals":
        await call.message.edit_text(
            "<b>Мои сделки</b>\n\nСделок пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_cb("Назад", "main:p2p")]]),
        )
    elif action == "create":
        await call.message.edit_text(
            "<b>Создать объявление</b>\n\nВыберите тип объявления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_cb("Покупать крипту", "p2p:create:buy")],
                [_cb("Продавать крипту", "p2p:create:sell")],
                [_cb("Назад", "main:p2p")],
            ]),
        )
    elif action == "settings":
        await call.message.edit_text(
            "<b>Оплата и валюта</b>\n\nЗдесь вы можете выбрать валюту отображаемых "
            "объявлений или управлять своими способами оплаты.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_cb("Валюта P2P Маркета: RUB", "p2p:fiat:RUB")],
                [_cb("Способы оплаты", "p2p:pms")],
                [_cb("Справка PRIDE P2P", "p2p:help")],
                [_cb("Назад", "main:p2p")],
            ]),
        )
    elif action == "profile":
        name = call.from_user.username and ("@" + call.from_user.username) or (call.from_user.first_name or "Гость")
        await call.message.edit_text(
            f"<b>{name}</b>\n\n"
            f"Ваша статистика торговли за <b>30 дней</b>:\n"
            f"0 сделок · 0% выполнено · $0",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_cb("За 30 дней", "p2p:stat:30"), _cb("За всё время", "p2p:stat:all")],
                [_cb("Установить имя пользователя", "p2p:set_name")],
                [_cb("Чёрный список · 0", "p2p:blacklist")],
                [_cb("Мои отзывы · 0", "p2p:reviews")],
                [_cb("Назад в P2P Маркет", "main:p2p")],
            ]),
            disable_web_page_preview=True,
        )
    elif action.startswith("coin:"):
        parts = action.split(":")
        coin = parts[1] if len(parts) > 1 else "USDT"
        side = parts[2] if len(parts) > 2 else "buy"
        verb = "покупки" if side == "buy" else "продажи"
        await call.message.edit_text(
            f"Выберите способ оплаты для {verb} <b>{coin}</b> за RUB.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_cb("СБП · 75.9₽ · 20",        f"p2p:pm:SBP:{coin}:{side}")],
                [_cb("Сбербанк · 71.89₽ · 5",   f"p2p:pm:Sber:{coin}:{side}")],
                [_cb("OZON Банк · 80₽ · 5",     f"p2p:pm:OZON:{coin}:{side}")],
                [_cb("ЮMoney · 81.41₽ · 5",     f"p2p:pm:UMoney:{coin}:{side}")],
                [_cb("Т-Банк · 81₽ · 3",        f"p2p:pm:TBank:{coin}:{side}")],
                [_cb("Альфа-Банк · 78₽ · 3",    f"p2p:pm:Alfa:{coin}:{side}")],
                [_cb("Яндекс Банк · 81₽ · 3",   f"p2p:pm:YBank:{coin}:{side}")],
                [_cb("Райффайзен · 81₽ · 2",    f"p2p:pm:Raif:{coin}:{side}")],
                [_cb("Назад", "p2p:buy" if side == "buy" else "p2p:sell")],
            ]),
        )
    else:
        await call.answer("Раздел в разработке", show_alert=True)
        return
    await call.answer()


# ═══════════════════ /checks ════════════════════════════════════════
@router.message(Command("checks"))
async def cmd_checks(message: Message):
    await _show_checks(message)


@router.callback_query(F.data == "main:checks")
async def cb_checks(call: CallbackQuery):
    await _show_checks(call.message, edit=True)
    await call.answer()


async def _show_checks(target: Message, edit: bool = False):
    text = (
        "<b>Чеки</b>\n\n"
        "Здесь вы можете создать чек для мгновенной отправки криптовалюты любому пользователю.\n\n"
        "<i>Финансовая революция.</i> Отправляйте криптовалюту быстро и легко прямо в Telegram, "
        "используя виртуальные чеки. Забудьте про длинные адреса кошельков."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_wa("Создать чек", "checks")],
        [_cb("Создать из чата", "checks:from_chat")],
        [_cb("Мои чеки", "checks:my")],
        [_cb("Назад", "main:back_to_start")],
    ])
    if edit:
        try: await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True); return
        except Exception: pass
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("checks:"))
async def cb_checks_sub(call: CallbackQuery):
    action = call.data.split(":", 1)[1]
    if action == "my":
        try:
            from core.models import Cheque
            from sqlalchemy import desc as _desc
            async with AsyncSessionLocal() as db:
                u = (await db.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
                if not u:
                    await call.answer("Сначала /start", show_alert=True); return
                rows = (await db.execute(
                    select(Cheque).where(
                        (Cheque.creator_user_id == u.id) | (Cheque.redeemed_by_user_id == u.id),
                    ).order_by(_desc(Cheque.created_at)).limit(20)
                )).scalars().all()
            if not rows:
                await call.message.edit_text(
                    "<b>Мои чеки</b>\n\nЧеков пока нет.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_cb("Назад", "main:checks")]]),
                )
            else:
                lines = ["<b>Мои чеки</b>", ""]
                for c in rows[:15]:
                    status = {"active": "[активный]", "redeemed": "[принят]", "cancelled": "[отменён]"}.get(c.status, "[—]")
                    lines.append(f"{status} {float(c.amount)} {c.coin_code}")
                await call.message.edit_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_cb("Назад", "main:checks")]]),
                )
        except Exception as e:
            await call.answer(f"Ошибка: {e}", show_alert=True)
    elif action == "from_chat":
        await call.answer("Используйте inline-режим в чате: @PrideP2P_bot <сумма>", show_alert=True)
    await call.answer()


# ═══════════════════ /swap ══════════════════════════════════════════
@router.message(Command("swap"))
async def cmd_swap(message: Message):
    await message.answer(
        "<b>Обмен</b>\n\nБыстрый обмен между криптовалютами.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_wa("Открыть обмен", "swap")],
            [_cb("Текущие курсы", "swap:rates")],
            [_cb("Назад", "main:back_to_start")],
        ]),
    )


@router.callback_query(F.data == "main:swap")
async def cb_swap(call: CallbackQuery):
    try:
        await call.message.edit_text(
            "<b>Обмен</b>\n\nБыстрый обмен между криптовалютами.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_wa("Открыть обмен", "swap")],
                [_cb("Текущие курсы", "swap:rates")],
                [_cb("Назад", "main:back_to_start")],
            ]),
        )
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "swap:rates")
async def cb_swap_rates(call: CallbackQuery):
    try:
        from core.services import rates_service
        rates = await rates_service.get_rates()
        lines = ["<b>Текущие курсы</b>", ""]
        for code, cg in (("USDT", "tether"), ("TON", "the-open-network"), ("TRX", "tron"),
                         ("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana"),
                         ("BNB", "binancecoin"), ("LTC", "litecoin"), ("DOGE", "dogecoin")):
            r = rates.get(cg) or {}
            usd = r.get("usd") or (1 if code in ("USDT", "USDC") else 0)
            ch = r.get("usd_24h_change") or 0
            sign = "+" if ch >= 0 else ""
            lines.append(f"<b>{code}</b>: ${usd:.4f}  ({sign}{ch:.2f}%)")
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_cb("Назад", "main:swap")]]),
        )
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)
    await call.answer()


# ═══════════════════ /settings ══════════════════════════════════════
@router.message(Command("settings"))
async def cmd_settings(message: Message):
    await _show_settings(message, tg_user_id=message.from_user.id)


@router.callback_query(F.data == "main:settings")
async def cb_settings(call: CallbackQuery):
    await _show_settings(call.message, tg_user_id=call.from_user.id, edit=True)
    await call.answer()


async def _show_settings(target: Message, tg_user_id: int, edit: bool = False):
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.tg_id == tg_user_id))).scalar_one_or_none()
    name = (u.full_name if u else None) or (u.username if u else None) or "Гость"
    text = (
        f"<b>{name}</b>\n\n"
        f"Часовой пояс: Europe/Moscow\n"
        f"Локальная валюта: USD"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_cb("Рефералы", "set:ref"),         _cb("Уведомления", "set:notif")],
        [_cb("Часовой пояс", "set:tz"),     _cb("Язык бота", "set:lang")],
        [_cb("Валюта бота · USD", "set:fiat")],
        [_cb("Комиссии и лимиты", "set:fees")],
        [_cb("Справка PRIDE P2P", "set:help")],
        [_cb("Что умеет PRIDE P2P", "set:about")],
        [_cb("Написать в поддержку", "set:support")],
        [_cb("Назад", "main:back_to_start")],
    ])
    if edit:
        try: await target.edit_text(text, reply_markup=kb); return
        except Exception: pass
    await target.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("set:"))
async def cb_settings_sub(call: CallbackQuery):
    action = call.data.split(":", 1)[1]
    msgs = {
        "ref": f"<b>Рефералы</b>\n\nПриглашайте друзей и получайте % с их сделок.\n\nВаша ссылка:\n<code>https://t.me/PrideP2P_bot?start=ref_{call.from_user.id}</code>",
        "notif": "<b>Уведомления</b>\n\nДепозиты: включено\nЧеки: включено\nСделки: включено",
        "tz": "<b>Часовой пояс</b>: Europe/Moscow",
        "lang": "<b>Язык</b>: Русский",
        "fiat": "<b>Валюта отображения</b>: USD",
        "fees": "<b>Комиссии и лимиты</b>\n\nWithdraw USDT TRC20: 3.5 USDT\nSwap: 1%\nP2P: 0.7%\nMin withdraw USDT: 5",
        "help": "<a href=\"https://t.me/PrideP2P\">Справка PRIDE P2P</a>",
        "about": "PRIDE P2P — мультивалютный кошелёк и P2P биржа в Telegram.",
        "support": "Поддержка: @PrideSupport_bot",
    }
    text = msgs.get(action, "Раздел в разработке")
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_cb("Назад", "main:settings")]]),
        disable_web_page_preview=True,
    )
    await call.answer()


# ═══════════════════ Заглушки ═══════════════════════════════════════
@router.callback_query(F.data.in_(["main:exchange", "main:invoices", "main:pay", "main:giveaway", "main:subs"]))
async def cb_stub(call: CallbackQuery):
    titles = {
        "main:exchange": "Биржа",
        "main:invoices": "Счета",
        "main:pay": "Crypto Pay",
        "main:giveaway": "Розыгрыши",
        "main:subs": "Подписки",
    }
    await call.message.edit_text(
        f"<b>{titles.get(call.data, '...')}</b>\n\nРаздел в разработке.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_cb("Назад", "main:back_to_start")],
        ]),
    )
    await call.answer()

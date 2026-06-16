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
    # Cache-busting: ?v={boot_timestamp} заставляет Telegram перезабирать свежий HTML
    from bot._miniapp_link import miniapp_link
    return miniapp_link(view)


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


# ═══════════════════ /p2p — handled by bot.handlers.p2p ═══════════
# Полный P2P-флоу живёт в отдельном модуле p2p.py (router зарегистрирован
# в bot/main.py до commands).
@router.callback_query(F.data == "main:p2p")
async def cb_p2p_redirect(call: CallbackQuery):
    from bot.handlers.p2p import _p2p_menu_text, _p2p_menu_kb
    try:
        await call.message.edit_text(await _p2p_menu_text(), reply_markup=_p2p_menu_kb())
    except Exception:
        await call.message.answer(await _p2p_menu_text(), reply_markup=_p2p_menu_kb())
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


def _bot_username() -> str:
    return (settings.bot_username or "PrideP2P_bot").lstrip("@")


def _cheque_link(code: str) -> str:
    return f"https://t.me/{_bot_username()}?start=chq_{code}"


@router.callback_query(F.data.startswith("checks:"))
async def cb_checks_sub(call: CallbackQuery):
    action = call.data.split(":", 1)[1]
    # ── Одиночный чек: checks:view:CODE ──
    if action.startswith("view:"):
        code = action.split(":", 1)[1]
        try:
            from core.models import Cheque
            async with AsyncSessionLocal() as db:
                cq = (await db.execute(select(Cheque).where(Cheque.code == code))).scalar_one_or_none()
                if not cq:
                    await call.answer("Чек не найден", show_alert=True); return
            link = _cheque_link(cq.code)
            status_txt = {"active":"Активный","redeemed":"Получен","cancelled":"Отменён"}.get(cq.status, cq.status)
            comment = f"\nКомментарий: <i>{cq.comment}</i>" if cq.comment else ""
            text = (
                f"<b>Чек {cq.amount} {cq.coin_code}</b>\n"
                f"Статус: <b>{status_txt}</b>\n"
                f"Код: <code>{cq.code}</code>"
                f"{comment}\n\n"
                f"<a href=\"{link}\">{link}</a>"
            )
            rows = []
            if cq.status == "active":
                rows.append([InlineKeyboardButton(text=f"Активировать {cq.amount} {cq.coin_code}", url=link)])
                rows.append([_cb("Отменить чек", f"checks:cancel:{cq.code}")])
            rows.append([_cb("← Мои чеки", "checks:my")])
            await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                          disable_web_page_preview=True)
        except Exception as e:
            await call.answer(f"Ошибка: {e}", show_alert=True)
        await call.answer(); return

    # ── Отмена чека: checks:cancel:CODE ──
    if action.startswith("cancel:"):
        code = action.split(":", 1)[1]
        try:
            from core.models import Cheque
            from core.services import balance_service
            from datetime import datetime, timezone
            async with AsyncSessionLocal() as db:
                cq = (await db.execute(select(Cheque).where(Cheque.code == code))).scalar_one_or_none()
                me = (await db.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
                if not cq or not me or cq.creator_user_id != me.id:
                    await call.answer("Нет доступа", show_alert=True); return
                if cq.status != "active":
                    await call.answer(f"Чек уже {cq.status}", show_alert=True); return
                await balance_service.credit(
                    db, me.id, cq.coin_code, cq.amount,
                    op_type="cheque_cancel", note=f"cheque {cq.code} cancelled",
                    ref_table="cheques", ref_id=cq.id,
                )
                cq.status = "cancelled"
                cq.cancelled_at = datetime.now(timezone.utc)
                await db.commit()
            await call.answer("Чек отменён, средства возвращены", show_alert=True)
        except Exception as e:
            await call.answer(f"Ошибка: {e}", show_alert=True)
        # Назад к списку
        call.data = "checks:my"
        return await cb_checks_sub(call)

    # ── Список "Мои чеки" ──
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
                    "<b>Мои чеки</b>\n\nЧеков пока нет.\n\n"
                    "Создать новый: жми «+ Новый» в Mini-App, или напиши в любом чате\n"
                    f"<code>@{_bot_username()} 5 USDT за пиццу</code>",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_cb("← Назад", "main:checks")]]),
                )
            else:
                lines = ["<b>Мои чеки</b>", ""]
                kb_rows = []
                for c in rows[:15]:
                    status = {"active":"○ актив","redeemed":"✓ получ","cancelled":"✗ отмен"}.get(c.status, "—")
                    role = "🟢" if c.creator_user_id == u.id else "🔵"
                    lines.append(f"{role} {status}  {c.amount} {c.coin_code}  <code>{c.code}</code>")
                    kb_rows.append([_cb(f"{c.amount} {c.coin_code} · {status}", f"checks:view:{c.code}")])
                kb_rows.append([_cb("← Назад", "main:checks")])
                await call.message.edit_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                    disable_web_page_preview=True,
                )
        except Exception as e:
            await call.answer(f"Ошибка: {e}", show_alert=True)
        await call.answer(); return

    if action == "from_chat":
        await call.answer(
            f"В любом чате: @{_bot_username()} <сумма> [USDT] [коммент]",
            show_alert=True,
        )
        return

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

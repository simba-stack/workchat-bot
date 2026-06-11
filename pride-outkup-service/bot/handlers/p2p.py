"""Полноценный P2P в боте.

Идёт прямо в БД (как `wallet.py` / `commands.py` — bot и API в одном процессе).
Не дублирует логику API — использует те же escrow_service / maker_stats / price_index.

Flow:
  /p2p → меню
  «Купить» / «Продать» → выбор coin → fiat → метод оплаты → список (топ 5)
  Карточка оффера → ввод суммы → создать сделку
  Сделка: статус, реквизиты, таймер, кнопки «Я оплатил», «Отмена», «Чат», «Спор»
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import and_, desc, or_, select

from core.db import AsyncSessionLocal
from core.models import Deal, DealMessage, Dispute, Offer, User
from core.services import escrow_service, maker_stats, price_index as pi_svc, settings_kv

router = Router(name="p2p")
logger = logging.getLogger(__name__)

PAY_METHODS_LABELS = {
    "sbp": "СБП", "tinkoff": "Тинькофф", "sber": "Сбер",
    "alpha": "Альфа", "ozon": "Озон", "raif": "Райф", "vtb": "ВТБ",
    "cash": "Наличные", "card_visa": "Visa/Master",
}


class DealOpenFSM(StatesGroup):
    waiting_amount = State()


class ChatFSM(StatesGroup):
    typing = State()


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=data)


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_user(tg_id: int) -> User | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()


# ═══════════════════ Главное меню P2P ═══════════════════════════════════
async def _p2p_menu_text() -> str:
    return (
        "<b>P2P-рынок</b>\n\n"
        "Покупка/продажа крипты у других пользователей напрямую "
        "с защитой эскроу.\n\n"
        "• Купить — найти продавца USDT за рубли\n"
        "• Продать — найти покупателя своей крипты\n"
        "• Мои сделки — текущие и завершённые\n"
        "• Мои объявления — управление своими офферами"
    )


def _p2p_menu_kb() -> InlineKeyboardMarkup:
    return _kb([
        [_btn("Купить", "p2p:browse:buy"), _btn("Продать", "p2p:browse:sell")],
        [_btn("Мои сделки", "p2p:my_deals"), _btn("Мои объявления", "p2p:my_offers")],
        [_btn("← Назад", "main:back_to_start")],
    ])


@router.message(Command("p2p"))
async def cmd_p2p(message: Message):
    await message.answer(await _p2p_menu_text(), reply_markup=_p2p_menu_kb())


@router.callback_query(F.data == "p2p:menu")
async def cb_menu(call: CallbackQuery):
    await call.message.edit_text(await _p2p_menu_text(), reply_markup=_p2p_menu_kb())
    await call.answer()


# ═══════════════════ Выбор coin/fiat/method ═════════════════════════════
@router.callback_query(F.data.startswith("p2p:browse:"))
async def cb_browse(call: CallbackQuery):
    """`p2p:browse:<side>` — выбор coin."""
    side = call.data.split(":")[2]  # buy | sell
    text = (
        f"<b>{'Покупка' if side == 'buy' else 'Продажа'} крипты</b>\n\n"
        f"Выберите монету:"
    )
    kb = _kb([
        [_btn("USDT", f"p2p:coin:{side}:USDT"), _btn("USDC", f"p2p:coin:{side}:USDC")],
        [_btn("TON", f"p2p:coin:{side}:TON"), _btn("BTC", f"p2p:coin:{side}:BTC")],
        [_btn("ETH", f"p2p:coin:{side}:ETH"), _btn("TRX", f"p2p:coin:{side}:TRX")],
        [_btn("← Назад", "p2p:menu")],
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("p2p:coin:"))
async def cb_coin(call: CallbackQuery):
    _, _, side, coin = call.data.split(":")
    text = f"<b>{coin} → {'Купить' if side == 'buy' else 'Продать'}</b>\n\nВалюта:"
    kb = _kb([
        [_btn("RUB", f"p2p:fiat:{side}:{coin}:RUB"),
         _btn("USD", f"p2p:fiat:{side}:{coin}:USD"),
         _btn("EUR", f"p2p:fiat:{side}:{coin}:EUR")],
        [_btn("← Назад", f"p2p:browse:{side}")],
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("p2p:fiat:"))
async def cb_fiat(call: CallbackQuery):
    _, _, side, coin, fiat = call.data.split(":")
    text = f"<b>{coin}/{fiat}</b>\n\nМетод оплаты:"
    kb = _kb([
        [_btn("Любой", f"p2p:list:{side}:{coin}:{fiat}:any:0")],
        [_btn("СБП", f"p2p:list:{side}:{coin}:{fiat}:sbp:0"),
         _btn("Тинькофф", f"p2p:list:{side}:{coin}:{fiat}:tinkoff:0")],
        [_btn("Сбер", f"p2p:list:{side}:{coin}:{fiat}:sber:0"),
         _btn("Альфа", f"p2p:list:{side}:{coin}:{fiat}:alpha:0")],
        [_btn("Наличные", f"p2p:list:{side}:{coin}:{fiat}:cash:0")],
        [_btn("← Назад", f"p2p:coin:{side}:{coin}")],
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


# ═══════════════════ Список офферов ═════════════════════════════════════
PAGE_SIZE = 5


async def _query_offers(
    db, side: str, coin: str, fiat: str, method: str, page: int,
) -> list[Offer]:
    offer_side = "sell" if side == "buy" else "buy"
    q = (
        select(Offer)
        .where(
            Offer.side == offer_side, Offer.status == "active",
            Offer.coin == coin.upper(), Offer.fiat == fiat.upper(),
        )
        .order_by(
            desc(Offer.is_pride_official),
            Offer.rate_rub_per_usdt.asc() if side == "buy" else Offer.rate_rub_per_usdt.desc(),
        )
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    if method and method != "any":
        q = q.where(Offer.payment_methods.any(method))
    return (await db.execute(q)).scalars().all()


@router.callback_query(F.data.startswith("p2p:list:"))
async def cb_list(call: CallbackQuery):
    _, _, side, coin, fiat, method, page_s = call.data.split(":")
    page = int(page_s or 0)
    async with AsyncSessionLocal() as db:
        offers = await _query_offers(db, side, coin, fiat, method, page)
        if not offers and page == 0:
            await call.message.edit_text(
                f"<b>{coin}/{fiat}</b>\n\nОбъявлений пока нет.",
                reply_markup=_kb([[_btn("← Назад", f"p2p:fiat:{side}:{coin}")]]),
            )
            await call.answer()
            return

        idx = await pi_svc.get_index(db, coin, fiat)
        # Author cache
        author_ids = list({o.user_id for o in offers})
        authors = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_(author_ids))
            )).scalars().all()
        }

    lines = [f"<b>{coin}/{fiat} — {'Покупка' if side=='buy' else 'Продажа'}</b>"]
    if idx:
        lines.append(f"Рыночный курс: <b>{float(idx):,.2f}</b> {fiat}\n")
    rows: list[list[InlineKeyboardButton]] = []
    for o in offers:
        a = authors.get(o.user_id)
        # Effective price для float
        if o.price_type == "float" and o.float_margin_pct and idx:
            eff = (idx * o.float_margin_pct / Decimal("100")).quantize(Decimal("0.01"))
        else:
            eff = o.rate_rub_per_usdt
        tier = (a.maker_tier if a else "none") or "none"
        tier_label = {"none":"","bronze":"BR","silver":"SR","gold":"GD","official":"PRIDE"}[tier]
        official = "PRIDE " if o.is_pride_official else ""
        nick = f"@{a.username}" if a and a.username else f"user{o.user_id}"
        rate_txt = (a.completion_rate_pct if a else 100.0)
        deals_txt = a.completed_deals if a else 0
        rows.append([
            _btn(
                f"{official}{tier_label} {nick} — {float(eff):.2f} {fiat} "
                f"[{int(float(o.min_amount_rub))}–{int(float(o.max_amount_rub))}] "
                f"({deals_txt}/{int(rate_txt)}%)",
                f"p2p:offer:{o.id}",
            )
        ])
    # пагинация
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("← Стр", f"p2p:list:{side}:{coin}:{fiat}:{method}:{page-1}"))
    if len(offers) == PAGE_SIZE:
        nav.append(_btn("Стр →", f"p2p:list:{side}:{coin}:{fiat}:{method}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("← Назад", f"p2p:fiat:{side}:{coin}")])
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=_kb(rows),
        disable_web_page_preview=True,
    )
    await call.answer()


# ═══════════════════ Карточка оффера ═══════════════════════════════════
@router.callback_query(F.data.startswith("p2p:offer:"))
async def cb_offer(call: CallbackQuery, state: FSMContext):
    offer_id = int(call.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        o = await db.get(Offer, offer_id)
        if not o:
            await call.answer("Оффер не найден", show_alert=True)
            return
        a = await db.get(User, o.user_id)
        idx = await pi_svc.get_index(db, o.coin or "USDT", o.fiat or "RUB")

    if o.price_type == "float" and o.float_margin_pct and idx:
        eff = (idx * o.float_margin_pct / Decimal("100")).quantize(Decimal("0.01"))
        price_note = f"(float: {float(o.float_margin_pct)}% от индекса)"
    else:
        eff = o.rate_rub_per_usdt
        price_note = "(fixed)"

    methods_human = ", ".join(PAY_METHODS_LABELS.get(m, m) for m in (o.payment_methods or []))
    tier = (a.maker_tier if a else "none") or "none"
    nick = f"@{a.username}" if a and a.username else f"user{o.user_id}"

    text = (
        f"<b>Оффер #{o.id}</b>\n"
        f"Мейкер: {nick}  ({tier.upper()}, {a.completed_deals if a else 0} сделок, "
        f"{(a.completion_rate_pct if a else 100.0)}%)\n"
        f"Тип: {'Продаёт' if o.side=='sell' else 'Покупает'} {o.coin or 'USDT'}\n"
        f"Цена: <b>{float(eff):.2f} {o.fiat or 'RUB'}</b> {price_note}\n"
        f"Лимиты: {float(o.min_amount_rub)} – {float(o.max_amount_rub)} {o.fiat or 'RUB'}\n"
        f"Окно оплаты: {o.pay_window_min or 30} мин\n"
        f"Методы: {methods_human}\n"
    )
    if o.conditions:
        text += f"\nУсловия:\n<i>{o.conditions[:500]}</i>"

    side = "buy" if (o.side == "sell") else "sell"  # с точки зрения тейкера
    await state.update_data(offer_id=o.id, side_view=side)
    rows = [
        [_btn("Открыть сделку", f"p2p:take:{o.id}")],
        [_btn("← Назад", f"p2p:list:{side}:{o.coin or 'USDT'}:{o.fiat or 'RUB'}:any:0")],
    ]
    await call.message.edit_text(text, reply_markup=_kb(rows))
    await call.answer()


# ═══════════════════ Открыть сделку ═════════════════════════════════════
@router.callback_query(F.data.startswith("p2p:take:"))
async def cb_take(call: CallbackQuery, state: FSMContext):
    offer_id = int(call.data.split(":")[2])
    me = await _get_user(call.from_user.id)
    if not me:
        await call.answer("Не зарегистрирован", show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        o = await db.get(Offer, offer_id)
        if not o or o.status != "active":
            await call.answer("Оффер недоступен", show_alert=True)
            return
        if o.user_id == me.id:
            await call.answer("Это ваш оффер — открыть сделку нельзя", show_alert=True)
            return
        # Anti-fraud + counterparty
        ok, err = await maker_stats.can_take_deal(db, me)
        if not ok:
            await call.answer(err or "Заблокировано", show_alert=True)
            return
        ok2, err2 = await maker_stats.check_taker_meets_offer_conditions(db, me, o)
        if not ok2:
            await call.answer(err2 or "Условия не подходят", show_alert=True)
            return

    await state.set_state(DealOpenFSM.waiting_amount)
    await state.update_data(offer_id=offer_id)
    await call.message.edit_text(
        f"Введите сумму в {o.fiat or 'RUB'} "
        f"(от {float(o.min_amount_rub)} до {float(o.max_amount_rub)}):",
        reply_markup=_kb([[_btn("Отмена", f"p2p:offer:{offer_id}")]]),
    )
    await call.answer()


@router.message(DealOpenFSM.waiting_amount, F.text)
async def msg_take_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    offer_id = data.get("offer_id")
    try:
        amount = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Введите число.")
        return
    await state.clear()

    me = await _get_user(message.from_user.id)
    if not me:
        await message.answer("Не зарегистрирован.")
        return
    async with AsyncSessionLocal() as db:
        o = await db.get(Offer, offer_id)
        if not o:
            await message.answer("Оффер не найден.")
            return
        if amount < o.min_amount_rub or amount > o.max_amount_rub:
            await message.answer(
                f"Сумма вне лимитов {float(o.min_amount_rub)}–{float(o.max_amount_rub)}."
            )
            return

        # Эффективная цена
        if o.price_type == "float" and o.float_margin_pct:
            live = await pi_svc.compute_float_price(
                db, o.coin or "USDT", o.fiat or "RUB", o.float_margin_pct
            )
            rate_used = live or o.rate_rub_per_usdt
        else:
            rate_used = o.rate_rub_per_usdt
        amount_crypto = (amount / rate_used).quantize(Decimal("0.0001"))

        # Roles
        if o.side == "sell":
            buyer_id, seller_id = me.id, o.user_id
        else:
            buyer_id, seller_id = o.user_id, me.id
        seller = await db.get(User, seller_id)
        if not seller or seller.balance_usdt < amount_crypto:
            await message.answer("У продавца недостаточно крипты в эскроу. Попробуй другой оффер.")
            return

        fee_pct = await settings_kv.get_fee_v2_pct(db)
        fee_amt = (amount_crypto * fee_pct / 100).quantize(Decimal("0.0001"))

        from datetime import timedelta
        pay_window = o.pay_window_min or 30
        deadline = datetime.now(timezone.utc) + timedelta(minutes=pay_window)

        # Next deal number
        last = (await db.execute(select(Deal.id).order_by(desc(Deal.id)).limit(1))).scalar() or 0
        deal = Deal(
            deal_number=f"dl{last+1:05d}",
            offer_id=o.id,
            buyer_id=buyer_id, seller_id=seller_id,
            coin=o.coin or "USDT", fiat=o.fiat or "RUB",
            amount_rub=amount,
            rate_rub_per_usdt=rate_used,
            amount_usdt=amount_crypto,
            payment_method=(o.payment_methods or ["sbp"])[0],
            status="awaiting_payment",
            fee_pct=fee_pct, fee_usdt=fee_amt,
            expires_at=deadline, pay_deadline_at=deadline,
        )
        db.add(deal)
        await db.flush()
        await escrow_service.lock(db, seller, deal)

        # auto-reply
        if o.auto_reply:
            db.add(DealMessage(
                deal_id=deal.id, from_user_id=o.user_id,
                text=o.auto_reply[:2000], is_system=False,
            ))
        await db.commit()

        # Уведомить второго участника
        try:
            from bot.main import notify_user
            other = await db.get(User, o.user_id)
            if other:
                await notify_user(
                    other.tg_id,
                    f"Новая сделка #{deal.deal_number}: {float(amount)} {deal.fiat} "
                    f"→ {float(amount_crypto)} {deal.coin}. /p2p → Мои сделки.",
                )
        except Exception:
            pass

    await _show_deal(message, deal.id)


# ═══════════════════ Карточка сделки ═══════════════════════════════════
async def _deal_card(deal_id: int, viewer_tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with AsyncSessionLocal() as db:
        d = await db.get(Deal, deal_id)
        if not d:
            return "Сделка не найдена.", _kb([[_btn("← Меню", "p2p:menu")]])
        me = (await db.execute(select(User).where(User.tg_id == viewer_tg_id))).scalar_one_or_none()
        if not me or me.id not in (d.buyer_id, d.seller_id):
            return "Доступ запрещён.", _kb([[_btn("← Меню", "p2p:menu")]])
        buyer = await db.get(User, d.buyer_id)
        seller = await db.get(User, d.seller_id)
        is_buyer = (me.id == d.buyer_id)
        ttl_min = ""
        if d.pay_deadline_at:
            ttl = int((d.pay_deadline_at - datetime.now(timezone.utc)).total_seconds() // 60)
            ttl_min = f" • осталось {max(0, ttl)} мин"

    counterpart_nick = (
        f"@{seller.username or 'seller'}" if is_buyer else f"@{buyer.username or 'buyer'}"
    )
    text = (
        f"<b>Сделка #{d.deal_number}</b>  ({d.status}{ttl_min})\n"
        f"{d.coin or 'USDT'} ↔ {d.fiat or 'RUB'}\n"
        f"Сумма: <b>{float(d.amount_rub)} {d.fiat or 'RUB'}</b> "
        f"= {float(d.amount_usdt)} {d.coin or 'USDT'}\n"
        f"Курс: {float(d.rate_rub_per_usdt)} {d.fiat or 'RUB'}/{d.coin or 'USDT'}\n"
        f"Контрагент: {counterpart_nick}\n"
        f"Метод оплаты: {PAY_METHODS_LABELS.get(d.payment_method, d.payment_method)}\n"
    )
    if d.bank or d.phone_or_card:
        text += f"Реквизиты: {d.bank or ''} {d.phone_or_card or ''}\n"
    if d.receiver_name:
        text += f"Получатель: {d.receiver_name}\n"

    rows: list[list[InlineKeyboardButton]] = []
    if d.status == "awaiting_payment":
        if is_buyer:
            rows.append([_btn("Я оплатил", f"p2p:deal:paid:{d.id}")])
        rows.append([_btn("Отменить", f"p2p:deal:cancel:{d.id}")])
    if d.status == "paid" and not is_buyer:
        rows.append([_btn("Подтвердить получение → Release", f"p2p:deal:release:{d.id}")])
    if d.status in ("awaiting_payment", "paid"):
        rows.append([_btn("Чат", f"p2p:deal:chat:{d.id}"), _btn("Открыть спор", f"p2p:deal:dispute:{d.id}")])
    rows.append([_btn("Обновить", f"p2p:deal:view:{d.id}"), _btn("← Меню", "p2p:menu")])
    return text, _kb(rows)


async def _show_deal(target, deal_id: int):
    """target = Message или CallbackQuery."""
    viewer_tg = target.from_user.id
    text, kb = await _deal_card(deal_id, viewer_tg)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("p2p:deal:view:"))
async def cb_deal_view(call: CallbackQuery):
    deal_id = int(call.data.split(":")[3])
    await _show_deal(call, deal_id)
    await call.answer()


@router.callback_query(F.data.startswith("p2p:deal:paid:"))
async def cb_deal_paid(call: CallbackQuery):
    deal_id = int(call.data.split(":")[3])
    async with AsyncSessionLocal() as db:
        d = await db.get(Deal, deal_id)
        if not d:
            await call.answer("Сделка не найдена", show_alert=True); return
        me = (await db.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
        if not me or me.id != d.buyer_id:
            await call.answer("Только покупатель может отметить оплату", show_alert=True); return
        if d.status != "awaiting_payment":
            await call.answer("Уже не awaiting_payment", show_alert=True); return
        d.status = "paid"
        d.paid_at = datetime.now(timezone.utc)
        db.add(DealMessage(deal_id=d.id, from_user_id=None, text="Покупатель пометил «оплачено».", is_system=True))
        await db.commit()
        try:
            from bot.main import notify_user
            seller = await db.get(User, d.seller_id)
            if seller:
                await notify_user(seller.tg_id, f"#{d.deal_number}: покупатель пометил «оплачено». Проверьте банк и сделайте Release.")
        except Exception:
            pass
    await _show_deal(call, deal_id)
    await call.answer("Помечено", show_alert=False)


@router.callback_query(F.data.startswith("p2p:deal:release:"))
async def cb_deal_release(call: CallbackQuery):
    deal_id = int(call.data.split(":")[3])
    async with AsyncSessionLocal() as db:
        d = await db.get(Deal, deal_id)
        if not d:
            await call.answer("Не найдена", show_alert=True); return
        me = (await db.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
        if not me or me.id != d.seller_id:
            await call.answer("Только продавец делает Release", show_alert=True); return
        if d.status != "paid":
            await call.answer("Можно релизить только после mark_paid", show_alert=True); return
        try:
            await escrow_service.release(db, d)
        except Exception as e:
            logger.exception("[p2p] release failed: %s", e)
            await call.answer(f"Ошибка release: {e}", show_alert=True); return
        db.add(DealMessage(deal_id=d.id, from_user_id=None,
                           text=f"Сделка завершена. {float(d.amount_usdt)} {d.coin} переданы покупателю.",
                           is_system=True))
        await db.commit()
        try:
            from bot.main import notify_user
            buyer = await db.get(User, d.buyer_id)
            if buyer:
                await notify_user(buyer.tg_id, f"#{d.deal_number}: продавец сделал Release. {float(d.amount_usdt)} {d.coin} зачислены.")
        except Exception:
            pass
    await _show_deal(call, deal_id)
    await call.answer("Release выполнен")


@router.callback_query(F.data.startswith("p2p:deal:cancel:"))
async def cb_deal_cancel(call: CallbackQuery):
    deal_id = int(call.data.split(":")[3])
    async with AsyncSessionLocal() as db:
        d = await db.get(Deal, deal_id)
        if not d:
            await call.answer("Не найдена", show_alert=True); return
        me = (await db.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
        if not me or me.id not in (d.buyer_id, d.seller_id):
            await call.answer("Нет доступа", show_alert=True); return
        if d.status != "awaiting_payment":
            await call.answer("Уже не awaiting_payment", show_alert=True); return
        d.status = "cancelled"
        d.cancelled_at = datetime.now(timezone.utc)
        d.cancelled_reason = "user_cancelled"
        await escrow_service.refund(db, d, "user_cancelled")
        db.add(DealMessage(deal_id=d.id, from_user_id=None, text="Сделка отменена.", is_system=True))
        await db.commit()
    await _show_deal(call, deal_id)
    await call.answer("Отменено")


@router.callback_query(F.data.startswith("p2p:deal:dispute:"))
async def cb_deal_dispute(call: CallbackQuery):
    deal_id = int(call.data.split(":")[3])
    async with AsyncSessionLocal() as db:
        d = await db.get(Deal, deal_id)
        if not d:
            await call.answer("Не найдена", show_alert=True); return
        me = (await db.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
        if not me or me.id not in (d.buyer_id, d.seller_id):
            await call.answer("Нет доступа", show_alert=True); return
        if d.status in ("released", "cancelled"):
            await call.answer("Уже закрыта", show_alert=True); return
        d.status = "disputed"
        d.disputed_deals_at = getattr(d, "disputed_deals_at", None)
        db.add(Dispute(
            deal_id=d.id, opened_by_id=me.id,
            reason="Открыто из бота — обратитесь к админу за детальным разбирательством",
            evidence_urls=[], status="open",
        ))
        await db.commit()
    await call.message.answer(
        "Спор открыт. Админ рассмотрит. Сделка заморожена.\n"
        "Опишите детали в чате сделки — это пойдёт в материалы спора.",
    )
    await _show_deal(call, deal_id)
    await call.answer()


# ═══════════════════ Чат сделки ═════════════════════════════════════════
@router.callback_query(F.data.startswith("p2p:deal:chat:"))
async def cb_deal_chat(call: CallbackQuery, state: FSMContext):
    deal_id = int(call.data.split(":")[3])
    async with AsyncSessionLocal() as db:
        d = await db.get(Deal, deal_id)
        if not d:
            await call.answer("Не найдена", show_alert=True); return
        me = (await db.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
        if not me or me.id not in (d.buyer_id, d.seller_id):
            await call.answer("Нет доступа", show_alert=True); return
        msgs = (await db.execute(
            select(DealMessage).where(DealMessage.deal_id == d.id)
            .order_by(DealMessage.created_at.asc()).limit(30)
        )).scalars().all()

    lines = [f"<b>Чат сделки #{d.deal_number}</b>"]
    if not msgs:
        lines.append("\n<i>Сообщений ещё нет — напишите контрагенту.</i>")
    else:
        for m in msgs[-15:]:
            who = "system" if m.is_system else ("вы" if m.from_user_id == me.id else "контрагент")
            ts = m.created_at.strftime("%H:%M") if m.created_at else ""
            lines.append(f"[{ts}] <b>{who}</b>: {m.text[:300]}")
    lines.append("\nПросто отправь сообщение — попадёт контрагенту.")

    await state.set_state(ChatFSM.typing)
    await state.update_data(deal_id=deal_id)
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=_kb([
            [_btn("К сделке", f"p2p:deal:view:{d.id}")],
            [_btn("← Меню P2P", "p2p:menu")],
        ]),
    )
    await call.answer()


@router.message(ChatFSM.typing, F.text)
async def msg_chat(message: Message, state: FSMContext):
    data = await state.get_data()
    deal_id = data.get("deal_id")
    if not deal_id:
        await state.clear(); return
    me = await _get_user(message.from_user.id)
    if not me:
        await state.clear(); return
    async with AsyncSessionLocal() as db:
        d = await db.get(Deal, deal_id)
        if not d or me.id not in (d.buyer_id, d.seller_id):
            await state.clear()
            await message.answer("Доступа к этой сделке нет.")
            return
        if d.status in ("released", "cancelled"):
            await state.clear()
            await message.answer("Сделка закрыта, чат недоступен.")
            return
        db.add(DealMessage(deal_id=deal_id, from_user_id=me.id, text=message.text[:2000]))
        await db.commit()
        # notify other
        other_id = d.seller_id if me.id == d.buyer_id else d.buyer_id
        try:
            from bot.main import notify_user
            other = await db.get(User, other_id)
            if other:
                await notify_user(
                    other.tg_id,
                    f"#{d.deal_number}: {message.text[:200]}",
                )
        except Exception:
            pass
    await message.answer("Отправлено. Напиши ещё или /p2p чтобы выйти.",)


# ═══════════════════ Мои сделки / Мои объявления ═══════════════════════
@router.callback_query(F.data == "p2p:my_deals")
async def cb_my_deals(call: CallbackQuery):
    me = await _get_user(call.from_user.id)
    if not me:
        await call.answer("Не зарегистрирован", show_alert=True); return
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Deal).where(or_(Deal.buyer_id == me.id, Deal.seller_id == me.id))
            .order_by(desc(Deal.created_at)).limit(20)
        )).scalars().all()
    if not rows:
        await call.message.edit_text(
            "Сделок пока нет.",
            reply_markup=_kb([[_btn("← Меню", "p2p:menu")]]),
        )
        await call.answer(); return
    kb_rows = []
    lines = ["<b>Мои сделки</b>"]
    for d in rows:
        role = "купить" if d.buyer_id == me.id else "продать"
        lines.append(f"#{d.deal_number} ({d.status}) {role} {float(d.amount_rub)} {d.fiat}")
        kb_rows.append([_btn(f"#{d.deal_number} — {d.status}", f"p2p:deal:view:{d.id}")])
    kb_rows.append([_btn("← Меню", "p2p:menu")])
    await call.message.edit_text("\n".join(lines), reply_markup=_kb(kb_rows))
    await call.answer()


@router.callback_query(F.data == "p2p:my_offers")
async def cb_my_offers(call: CallbackQuery):
    me = await _get_user(call.from_user.id)
    if not me:
        await call.answer("Не зарегистрирован", show_alert=True); return
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Offer).where(Offer.user_id == me.id).order_by(desc(Offer.created_at)).limit(20)
        )).scalars().all()
    if not rows:
        await call.message.edit_text(
            "Объявлений нет. Создать оффер можно из Mini-App.",
            reply_markup=_kb([[_btn("← Меню", "p2p:menu")]]),
        )
        await call.answer(); return
    kb_rows = []
    lines = ["<b>Мои объявления</b>"]
    for o in rows:
        eff = float(o.rate_rub_per_usdt)
        lines.append(
            f"#{o.id} {o.side} {o.coin}/{o.fiat} {eff:.2f} "
            f"[{int(float(o.min_amount_rub))}–{int(float(o.max_amount_rub))}] ({o.status})"
        )
        toggle_btn = _btn("Возобновить" if o.status == "paused" else "Пауза",
                          f"p2p:offer:toggle:{o.id}")
        kb_rows.append([_btn(f"#{o.id}", f"p2p:offer:{o.id}"), toggle_btn])
    kb_rows.append([_btn("← Меню", "p2p:menu")])
    await call.message.edit_text("\n".join(lines), reply_markup=_kb(kb_rows))
    await call.answer()


@router.callback_query(F.data.startswith("p2p:offer:toggle:"))
async def cb_offer_toggle(call: CallbackQuery):
    offer_id = int(call.data.split(":")[3])
    me = await _get_user(call.from_user.id)
    if not me:
        await call.answer("Не зарегистрирован", show_alert=True); return
    async with AsyncSessionLocal() as db:
        o = await db.get(Offer, offer_id)
        if not o or o.user_id != me.id:
            await call.answer("Нет доступа", show_alert=True); return
        o.status = "active" if o.status == "paused" else "paused"
        if o.status == "active":
            o.paused_reason = None
        await db.commit()
    await call.answer(f"Status: {o.status}", show_alert=False)
    await cb_my_offers(call)

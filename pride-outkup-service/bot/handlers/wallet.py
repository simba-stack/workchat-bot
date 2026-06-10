"""Wallet-команды в @PrideP2P_bot:
/balance — показать балансы всех монет с курсами
/send — FSM отправка криптовалюты по @username (3 шага: coin → @user+amount → confirm)
/swap — заглушка с Mini-App кнопкой
"""
import logging
from decimal import Decimal

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
    WebAppInfo,
)
from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.models import Coin, User
from core.services import balance_service, rates_service

router = Router(name="wallet")
logger = logging.getLogger(__name__)


class SendFSM(StatesGroup):
    pick_coin = State()
    enter_target = State()
    confirm = State()


def _miniapp_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🚀 Открыть приложение",
            web_app=WebAppInfo(url=f"{settings.miniapp_url}{settings.miniapp_path}"),
        )
    ]])


# ─── /balance ──────────────────────────────────────────────────────────
@router.message(Command("balance", "bal"))
async def cmd_balance(message: Message):
    if not message.from_user:
        return
    async with AsyncSessionLocal() as db:
        ures = await db.execute(select(User).where(User.tg_id == message.from_user.id))
        u = ures.scalar_one_or_none()
        if not u:
            await message.answer("Сначала открой /start.", reply_markup=_miniapp_kb())
            return
        bals = await balance_service.list_balances(db, u.id)
        cres = await db.execute(select(Coin).where(Coin.is_active.is_(True)).order_by(Coin.sort_order))
        coins = {c.code: c for c in cres.scalars().all()}
    rates = await rates_service.get_rates()

    if not bals or all(v == 0 for v in bals.values()):
        await message.answer(
            "💼 <b>Баланс пуст</b>\nПополни через /deposit или открой приложение.",
            reply_markup=_miniapp_kb(),
        )
        return

    lines = ["💼 <b>Баланс</b>\n"]
    total_usd = Decimal("0")
    for code, amt in sorted(bals.items(), key=lambda x: -(x[1] or 0)):
        if amt <= 0:
            continue
        c = coins.get(code)
        name = c.name if c else code
        rate_usd = Decimal(str((rates.get(code) or {}).get("usd") or (1 if code in ("USDT","USDC") else 0)))
        usd = Decimal(amt) * rate_usd
        total_usd += usd
        delta = (rates.get(code) or {}).get("change_24h")
        delta_str = ""
        if delta is not None:
            arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "▪")
            delta_str = f" {arrow} {delta:+.2f}%"
        lines.append(f"<b>{name}</b>: {float(amt):.4f} {code}  (~${float(usd):.2f}){delta_str}")
    lines.append(f"\n💰 <b>Всего</b>: ${float(total_usd):.2f}")
    await message.answer("\n".join(lines), reply_markup=_miniapp_kb())


# ─── /send ─────────────────────────────────────────────────────────────
@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    if not message.from_user:
        return
    await state.clear()
    async with AsyncSessionLocal() as db:
        ures = await db.execute(select(User).where(User.tg_id == message.from_user.id))
        u = ures.scalar_one_or_none()
        if not u:
            await message.answer("Сначала открой /start.", reply_markup=_miniapp_kb())
            return
        if u.kyc_status != "verified":
            await message.answer(
                "🔒 Отправка доступна после KYC. Открой приложение → Профиль → Пройти верификацию.",
                reply_markup=_miniapp_kb(),
            )
            return
        bals = await balance_service.list_balances(db, u.id)

    # Список монет с положительным балансом
    options = [(code, amt) for code, amt in bals.items() if amt > 0]
    if not options:
        await message.answer("💼 На балансе пусто. Пополни через /deposit.", reply_markup=_miniapp_kb())
        return

    kb_rows = []
    for code, amt in options[:8]:
        kb_rows.append([InlineKeyboardButton(
            text=f"{code} · {float(amt):.4f}",
            callback_data=f"snd:coin:{code}",
        )])
    kb_rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="snd:cancel")])
    await message.answer(
        "💸 <b>Отправка криптовалюты</b>\n\nВыбери монету:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await state.set_state(SendFSM.pick_coin)


@router.callback_query(F.data == "snd:cancel")
async def cb_send_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отменено.")
    await call.answer()


@router.callback_query(StateFilter(SendFSM.pick_coin), F.data.startswith("snd:coin:"))
async def cb_pick_coin(call: CallbackQuery, state: FSMContext):
    coin = call.data.split(":", 2)[2]
    await state.update_data(coin=coin)
    await call.message.edit_text(
        f"💸 Монета: <b>{coin}</b>\n\n"
        "Отправь сообщение в формате:\n"
        "<code>@username 12.34</code>\n"
        "или с комментарием:\n"
        "<code>@username 12.34 за обед</code>"
    )
    await state.set_state(SendFSM.enter_target)
    await call.answer()


@router.message(StateFilter(SendFSM.enter_target))
async def msg_enter_target(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    # Парсим "@user amount [comment]"
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Формат: <code>@username 12.34</code> [комментарий]")
        return
    username = parts[0].lstrip("@")
    try:
        amount = Decimal(parts[1].replace(",", "."))
    except Exception:
        await message.answer("Сумма должна быть числом.")
        return
    if amount <= 0:
        await message.answer("Сумма должна быть > 0.")
        return
    comment = parts[2] if len(parts) > 2 else ""

    data = await state.get_data()
    coin = data.get("coin")

    # Найти юзера и проверить баланс
    async with AsyncSessionLocal() as db:
        ures = await db.execute(select(User).where(User.tg_id == message.from_user.id))
        me = ures.scalar_one_or_none()
        rres = await db.execute(select(User).where(User.username == username))
        recipient = rres.scalar_one_or_none()
        if not recipient:
            await message.answer(f"❌ @{username} не зарегистрирован в PRIDE P2P.")
            await state.clear()
            return
        if recipient.id == me.id:
            await message.answer("❌ Нельзя себе же.")
            return
        bal = await balance_service.get_balance(db, me.id, coin)
        if bal < amount:
            await message.answer(f"❌ Недостаточно {coin}: на балансе {float(bal):.4f}")
            await state.clear()
            return

    await state.update_data(username=username, recipient_id=recipient.id,
                            recipient_tg_id=recipient.tg_id, amount=str(amount), comment=comment)
    name = recipient.full_name or f"@{username}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="snd:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="snd:cancel"),
    ]])
    txt = (
        f"💸 <b>Подтверждение перевода</b>\n\n"
        f"Получатель: <b>{name}</b> (@{username})\n"
        f"Сумма: <b>{float(amount)} {coin}</b>"
    )
    if comment:
        txt += f"\nКомментарий: <i>{comment}</i>"
    await message.answer(txt, reply_markup=kb)
    await state.set_state(SendFSM.confirm)


@router.callback_query(StateFilter(SendFSM.confirm), F.data == "snd:confirm")
async def cb_confirm_send(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    coin = data["coin"]
    amount = Decimal(data["amount"])
    recipient_id = data["recipient_id"]
    recipient_tg = data["recipient_tg_id"]
    username = data["username"]
    comment = data.get("comment", "")

    from core.models import Transfer
    async with AsyncSessionLocal() as db:
        ures = await db.execute(select(User).where(User.tg_id == call.from_user.id))
        me = ures.scalar_one_or_none()
        if not me:
            await call.message.edit_text("❌ Юзер не найден.")
            await state.clear()
            return
        try:
            await balance_service.transfer_atomic(db, me.id, recipient_id, coin, amount, note=comment)
        except Exception as e:
            await call.message.edit_text(f"❌ {str(e)[:200]}")
            await state.clear()
            return
        tr = Transfer(
            from_user_id=me.id, to_user_id=recipient_id,
            coin_code=coin, amount=amount, comment=comment, status="completed",
        )
        db.add(tr)
        await db.commit()

    await state.clear()
    await call.message.edit_text(
        f"✅ <b>Отправлено</b>\n\n{float(amount)} {coin} → @{username}"
    )
    # Уведомим получателя
    try:
        from bot.main import notify_user
        msg = (
            f"💸 <b>+{float(amount)} {coin}</b>\n"
            f"От @{call.from_user.username or call.from_user.id}"
        )
        if comment:
            msg += f"\n💬 «{comment}»"
        await notify_user(recipient_tg, msg)
    except Exception:
        pass
    await call.answer("Готово!")


# ─── /swap ─────────────────────────────────────────────────────────────
@router.message(Command("swap"))
async def cmd_swap(message: Message):
    await message.answer(
        "🔄 <b>Обмен криптовалюты</b>\n\nОткрой приложение → Кошелёк → Обмен.",
        reply_markup=_miniapp_kb(),
    )


# ─── /wallet_info — admin only ────────────────────────────────────────
@router.message(Command("wallet_info"))
async def cmd_wallet_info(message: Message):
    """Только для admin: показывает hash master key для проверки бэкапа + статистику."""
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return  # silent для не-admin
    import hashlib
    from sqlalchemy import func, select as _sel
    from core.models import SystemSecret, UserDepositAddress
    from core.services.wallet_derive import MASTER_KEY_NAME
    async with AsyncSessionLocal() as db:
        srow = (await db.execute(_sel(SystemSecret).where(SystemSecret.key == MASTER_KEY_NAME))).scalar_one_or_none()
        addr_count = (await db.execute(_sel(func.count(UserDepositAddress.id)))).scalar() or 0
    if not srow:
        await message.answer("⚠️ Master key ещё не сгенерирован (новый сервис?). Открой Mini-App → Пополнить.")
        return
    key_hex = srow.value
    h16 = hashlib.sha256(key_hex.encode()).hexdigest()[:16]
    h32 = hashlib.sha256(key_hex.encode()).hexdigest()[:32]
    await message.answer(
        "🔐 <b>Wallet Info</b>\n\n"
        f"Master key hash16: <code>{h16}</code>\n"
        f"Master key hash32: <code>{h32}</code>\n"
        f"User deposit addresses: <b>{addr_count}</b>\n"
        f"Created: {srow.created_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        "Сверь hash16 с тем что у тебя в 1Password — должен совпадать. "
        "Если не совпадает — ключ был перезаписан/восстановлен."
    )


@router.message(Command("export_master_key"))
async def cmd_export_master(message: Message):
    """Только для admin: повторно отправляет master key (если оригинал потерян).
    ВНИМАНИЕ: пиши команду только в личке боту, не в группе.
    """
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return
    if message.chat.type != "private":
        await message.answer("Эта команда только в личке бота — не в группе.")
        return
    from sqlalchemy import select as _sel
    from core.models import SystemSecret
    from core.services.wallet_derive import MASTER_KEY_NAME
    async with AsyncSessionLocal() as db:
        srow = (await db.execute(_sel(SystemSecret).where(SystemSecret.key == MASTER_KEY_NAME))).scalar_one_or_none()
    if not srow:
        await message.answer("⚠️ Master key ещё не создан.")
        return
    await message.answer(
        "🔐 <b>PRIDE P2P · Master Key (повторная отправка)</b>\n\n"
        f"<code>{srow.value}</code>\n\n"
        "Сохрани в 1Password. Удали это сообщение после копирования."
    )


# ─── /coins — список курсов ────────────────────────────────────────────
@router.message(Command("coins", "rates"))
async def cmd_coins(message: Message):
    rates = await rates_service.get_rates()
    if not rates:
        await message.answer("⏳ Курсы ещё не подтянуты, попробуй через минуту.")
        return
    lines = ["📊 <b>Курсы криптовалют</b>\n"]
    order = ["USDT", "USDC", "TON", "TRX", "BTC", "ETH", "SOL", "BNB", "DOGE", "LTC", "XAUT"]
    for code in order:
        r = rates.get(code)
        if not r:
            continue
        usd = r.get("usd") or 0
        rub = r.get("rub") or 0
        delta = r.get("change_24h") or 0
        arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "▪")
        usd_s = f"${usd:.4f}" if usd < 1 else f"${usd:.2f}"
        lines.append(f"<b>{code}</b>: {usd_s} · {rub:.2f}₽ · {arrow} {delta:+.2f}%")
    await message.answer("\n".join(lines), reply_markup=_miniapp_kb())

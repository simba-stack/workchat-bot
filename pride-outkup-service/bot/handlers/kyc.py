"""KYC flow в ЛС бота + дополнительные команды.

Основная реализация KYC сидит в Mini-App. Бот предлагает быстрый старт через WebApp.
"""
import logging
from decimal import Decimal

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.models import Order, User

router = Router(name="kyc")
logger = logging.getLogger(__name__)


def _miniapp_kb(suffix: str = "") -> InlineKeyboardMarkup:
    from bot._miniapp_link import miniapp_link
    # suffix может быть ?view=kyc; вырежем имя view-параметра
    view = ""
    if suffix and "view=" in suffix:
        try:
            view = suffix.split("view=", 1)[1].split("&", 1)[0]
        except Exception:
            view = ""
    url = miniapp_link(view)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=url))],
    ])


@router.message(Command("kyc"))
async def cmd_kyc(message: Message):
    await message.answer(
        "📋 <b>Верификация</b>\n\n"
        "Жми кнопку ниже → Профиль → «Пройти KYC».\n"
        "После одобрения тебе будет доступен P2P и крупные сделки.",
        reply_markup=_miniapp_kb("#kyc"),
    )


@router.message(Command("balance", "bal"))
async def cmd_balance(message: Message):
    if not message.from_user:
        return
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.tg_id == message.from_user.id))
        u = res.scalar_one_or_none()
    if not u:
        await message.answer("Сначала открой приложение через /start.")
        return
    bal = float(u.balance_usdt)
    kyc = {"pending": "не пройден", "pending_review": "на модерации",
           "verified": "✅ verified", "rejected": "❌ отклонён",
           "banned": "🚫 забанен"}.get(u.kyc_status, u.kyc_status)
    await message.answer(
        f"💰 <b>Баланс</b>: {bal:.4f} USDT\n"
        f"📋 KYC: {kyc} (уровень {u.kyc_level})\n"
        f"⚖️ Trust score: {u.trust_score}\n"
        f"🔢 Сделок: {u.total_deals} (✅ {u.completed_deals}, ❌ {u.cancelled_deals})",
        reply_markup=_miniapp_kb(),
    )


@router.message(Command("deposit"))
async def cmd_deposit(message: Message):
    if not message.from_user:
        return
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.tg_id == message.from_user.id))
        u = res.scalar_one_or_none()
    if not u:
        await message.answer("Сначала открой приложение через /start.")
        return
    if u.kyc_status != "verified":
        await message.answer(
            "Сначала пройди KYC — без верификации депозиты недоступны.",
            reply_markup=_miniapp_kb("#kyc"),
        )
        return
    if not settings.tron_hot_wallet_address:
        await message.answer("⚠️ TRON-кошелёк ещё не настроен.")
        return
    await message.answer(
        "💸 <b>Пополнение USDT TRC20</b>\n\n"
        "Жми «Открыть приложение» → Кошелёк → <b>Пополнить</b>.\n"
        "Введи сумму — мы сгенерируем уникальную сумму с QR-кодом, "
        "по которой опознаем твой перевод и автоматически зачислим баланс.",
        reply_markup=_miniapp_kb(),
    )


@router.message(Command("withdraw"))
async def cmd_withdraw(message: Message):
    await message.answer(
        "💸 Вывод USDT — открой приложение → Кошелёк → «Вывести».",
        reply_markup=_miniapp_kb(),
    )


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    """Последние 5 заявок."""
    if not message.from_user:
        return
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.tg_id == message.from_user.id))
        u = res.scalar_one_or_none()
        if not u:
            await message.answer("Сначала открой приложение через /start.")
            return
        from sqlalchemy import desc as _desc
        ores = await db.execute(
            select(Order).where(Order.user_id == u.id)
            .order_by(_desc(Order.created_at)).limit(5)
        )
        orders = ores.scalars().all()
    if not orders:
        await message.answer("📭 У тебя пока нет заявок. Открой приложение чтобы создать.",
                             reply_markup=_miniapp_kb())
        return
    lines = ["📋 <b>Последние заявки</b>:\n"]
    for o in orders:
        kind_emoji = {"buy_usdt": "💵→💎", "sell_usdt": "💎→💵", "business_outkup": "🏢"}.get(o.kind, "📦")
        lines.append(
            f"{kind_emoji} <b>{o.order_number}</b> · {float(o.amount_rub):.0f}₽ → "
            f"{float(o.amount_usdt):.2f}$ · <i>{o.status}</i>"
        )
    await message.answer("\n".join(lines), reply_markup=_miniapp_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>:\n"
        "/start — открыть приложение\n"
        "/kyc — верификация\n"
        "/balance — баланс USDT и статус\n"
        "/deposit — пополнить USDT TRC20\n"
        "/withdraw — вывести USDT\n"
        "/orders — последние заявки\n"
        "/help — это сообщение"
    )

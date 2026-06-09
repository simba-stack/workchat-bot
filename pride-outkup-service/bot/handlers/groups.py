"""Команды в групповых чатах — /курс, /предложения.

Бот может быть добавлен в любую группу как члене. Реагирует на
команды или на keywords (типа «откуп 100к»).
"""
import logging
import re

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from core.config import settings

router = Router(name="groups")
logger = logging.getLogger(__name__)


@router.message(Command("курс", "rate"))
async def cmd_rate(message: Message):
    from core.db import AsyncSessionLocal
    from core.services import settings_kv
    async with AsyncSessionLocal() as db:
        buy = await settings_kv.get_rate_buy(db)
        sell = await settings_kv.get_rate_sell(db)
        fee = await settings_kv.get_fee_v1_pct(db)
    await message.reply(
        "💱 <b>Курс PRIDE</b>\n\n"
        f"Купить USDT: <b>{float(buy):.2f} ₽</b>\n"
        f"Продать USDT: <b>{float(sell):.2f} ₽</b>\n"
        f"Комиссия: {float(fee):.1f}%\n\n"
        f"Открой Mini-App чтобы совершить обмен → @{settings.bot_username if False else 'PrideP2P_bot'}"
    )


@router.message(Command("предложения", "offers"))
async def cmd_offers(message: Message):
    from core.db import AsyncSessionLocal
    from core.models import Offer
    from sqlalchemy import desc as _desc, select as _sel
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            _sel(Offer)
            .where(Offer.status == "active")
            .order_by(_desc(Offer.is_pride_official), Offer.rate_rub_per_usdt.asc())
            .limit(5)
        )
        offers = res.scalars().all()
    if not offers:
        await message.reply("🏛 Пока нет активных P2P-предложений. Открой Mini-App чтобы создать своё.")
        return
    lines = ["🏛 <b>Топ-5 P2P-предложений</b>:\n"]
    for o in offers:
        official = "⭐" if o.is_pride_official else "👤"
        side_label = "Продаёт" if o.side == "sell" else "Покупает"
        lines.append(
            f"{official} {side_label} USDT по <b>{float(o.rate_rub_per_usdt):.2f} ₽</b> "
            f"· {float(o.min_amount_rub):.0f}–{float(o.max_amount_rub):.0f}₽"
        )
    lines.append("\nОткрой Mini-App → @PrideP2P_bot")
    await message.reply("\n".join(lines))


# Текст-триггер «откуп N к/тыс/рублей»
_OUTKUP_RE = re.compile(
    r"(?:откуп\w*|купить\s+usdt|обмен)\s*(?:на\s+)?(\d+)\s*(к|тыс|тысяч|руб|₽|k)?",
    re.IGNORECASE,
)


@router.message(F.text & F.chat.type.in_({"group", "supergroup"}))
async def text_trigger(message: Message):
    text = message.text or ""
    m = _OUTKUP_RE.search(text)
    if not m:
        return
    amount = int(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("к", "k", "тыс", "тысяч"):
        amount *= 1000
    if amount < 1000:
        return  # слишком мало, явно опечатка

    logger.info(
        "[group_trigger] chat=%s user=%s amount=%s",
        message.chat.id,
        message.from_user.id if message.from_user else "?",
        amount,
    )
    await message.reply(
        f"💱 Понял — открой Mini-App чтобы создать заявку на <b>{amount:,} ₽</b>.\n"
        f"Жми /start у @PrideP2P_bot.".replace(",", " ")
    )

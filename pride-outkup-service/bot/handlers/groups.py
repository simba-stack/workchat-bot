"""Команды в групповых чатах — /курс, /предложения.

Бот может быть добавлен в любую группу как члене. Реагирует на
команды или на keywords (типа «откуп 100к»).
"""
import logging
import re

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="groups")
logger = logging.getLogger(__name__)


@router.message(Command("курс", "rate"))
async def cmd_rate(message: Message):
    # TODO: подтянуть актуальный курс из ExchangeService
    await message.reply(
        "💱 <b>Курс PRIDE</b>\n\n"
        "Купить USDT: <b>84.00 ₽</b>\n"
        "Продать USDT: <b>82.00 ₽</b>\n\n"
        "Открой Mini-App чтобы совершить обмен → @PrideP2P_bot"
    )


@router.message(Command("предложения", "offers"))
async def cmd_offers(message: Message):
    # TODO: подтянуть топ-3 active offers из OfferService
    await message.reply(
        "🏛 <b>Топ P2P-предложений</b>\n\n"
        "Скоро — открой Mini-App: /start у @PrideP2P_bot"
    )


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

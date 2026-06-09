"""/start handler — приветствие + кнопка Mini-App."""
import logging
import re

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from core.config import settings

router = Router(name="start")
logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 Привет! Это <b>PRIDE P2P</b> — биржа обмена USDT ↔ RUB.\n\n"
    "Что умеем:\n"
    "• 💰 Купить/продать USDT по нашему курсу\n"
    "• 🏛 Открытая P2P-биржа (как Binance P2P)\n"
    "• ⚡ Мгновенные выплаты на TRC20\n"
    "• 🛡 Escrow и защита сделок\n\n"
    "Нажми кнопку ниже чтобы открыть приложение."
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    # Парсим реферальный код: /start ref_12345
    args = (message.text or "").split(maxsplit=1)
    referrer_tg_id = None
    if len(args) == 2:
        m = re.match(r"ref_(\d+)", args[1])
        if m:
            referrer_tg_id = int(m.group(1))

    if referrer_tg_id:
        logger.info(
            "[/start] user=%s referrer=%s",
            message.from_user.id if message.from_user else "?",
            referrer_tg_id,
        )
        # TODO: записать referral в БД (Phase A2)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🚀 Открыть PRIDE P2P",
            web_app=WebAppInfo(url=f"{settings.miniapp_url}{settings.miniapp_path}"),
        )],
        [InlineKeyboardButton(text="📖 Как это работает?", callback_data="howto")],
        [InlineKeyboardButton(text="💬 Поддержка", url="https://t.me/PrideSupport_bot")],
    ])

    await message.answer(WELCOME_TEXT, reply_markup=kb)


@router.callback_query(F.data == "howto")
async def cb_howto(call):
    text = (
        "<b>Как работает PRIDE P2P</b>\n\n"
        "<b>1. Регистрация</b>\n"
        "Жми «Открыть PRIDE P2P» — авто-вход через Telegram.\n\n"
        "<b>2. Верификация (KYC)</b>\n"
        "Заполни паспорт + сделай КУЦ-видео. После одобрения — торгуй до 500к₽/сделка.\n\n"
        "<b>3. Обмен</b>\n"
        "• <b>Купить USDT</b>: вводишь сумму → платишь нам RUB → получаешь USDT\n"
        "• <b>Продать USDT</b>: даёшь USDT → получаешь RUB на карту\n\n"
        "<b>4. P2P (скоро)</b>\n"
        "Торгуй с другими клиентами по лучшему курсу. Escrow защищает обоих.\n\n"
        "<b>Курс</b>: ~84₽ за USDT (купить), ~82₽ (продать). Комиссия 3.5% учтена."
    )
    await call.message.answer(text)
    await call.answer()

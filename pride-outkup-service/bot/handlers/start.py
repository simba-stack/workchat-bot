"""/start handler — приветствие + кнопка Mini-App.

Поддерживает deep-link:
  /start chq_CODE       — погасить чек
  /start cheque_CODE    — то же (legacy)
  /start ref_<tg_id>    — реферальная ссылка
"""
import logging
import re

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)
from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.models import Cheque, User

router = Router(name="start")
logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "<b>PRIDE P2P</b> — крипто-кошелёк и P2P-биржа в Telegram.\n\n"
    "Что умеем:\n"
    "• Купить/продать USDT по нашему курсу\n"
    "• Открытая P2P-биржа (как Binance P2P)\n"
    "• Мгновенные выплаты на TRC20\n"
    "• Escrow и защита сделок\n"
    "• Виртуальные чеки между юзерами\n\n"
    "Нажми кнопку ниже чтобы открыть приложение."
)


def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Открыть PRIDE P2P",
            web_app=WebAppInfo(url=f"{settings.miniapp_url}{settings.miniapp_path}"),
        )],
        [InlineKeyboardButton(text="Как это работает?", callback_data="howto")],
        [InlineKeyboardButton(text="Поддержка", url="https://t.me/PrideSupport_bot")],
    ])


async def _try_redeem_cheque(message: Message, code: str) -> bool:
    """Попытаться погасить чек по коду. True если успех."""
    if not code:
        return False
    tg_id = message.from_user.id if message.from_user else None
    if not tg_id:
        return False
    async with AsyncSessionLocal() as db:
        cq = (await db.execute(
            select(Cheque).where(Cheque.code == code)
        )).scalar_one_or_none()
        if not cq:
            await message.answer(f"Чек <code>{code}</code> не найден.")
            return True
        if cq.status != "active":
            status_human = {"redeemed": "уже активирован", "cancelled": "отменён создателем"}.get(cq.status, cq.status)
            await message.answer(f"Чек <code>{code}</code> {status_human}.")
            return True
        # Получатель — наш юзер
        u = (await db.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if not u:
            # Создаём юзера на лету
            u = User(
                tg_id=tg_id,
                username=message.from_user.username,
                full_name=(message.from_user.full_name or "")[:256],
            )
            db.add(u)
            await db.flush()
        # Создатель тоже может активировать (по запросу SIMBA — для теста или ввода средств обратно)
        # Зачисляем
        from core.services import balance_service
        await balance_service.credit(
            db, u.id, cq.coin_code, cq.amount,
            op_type="cheque_redeem", note=f"cheque {cq.code}",
            ref_table="cheques", ref_id=cq.id,
        )
        cq.status = "redeemed"
        cq.redeemed_by_user_id = u.id
        from datetime import datetime, timezone
        cq.redeemed_at = datetime.now(timezone.utc)
        await db.commit()

        # Уведомить создателя
        try:
            from bot.main import notify_user
            creator = await db.get(User, cq.creator_user_id)
            if creator:
                taker_name = f"@{u.username}" if u.username else (u.full_name or f"id{u.tg_id}")
                await notify_user(
                    creator.tg_id,
                    f"Чек {cq.amount} {cq.coin_code} активирован пользователем {taker_name}.",
                )
        except Exception:
            pass

        comment = f"\n<i>{cq.comment}</i>" if cq.comment else ""
        await message.answer(
            f"<b>Чек активирован.</b>{comment}\n\n"
            f"Зачислено: <b>{cq.amount} {cq.coin_code}</b>\n"
            f"Открой Кошелёк чтобы увидеть баланс.",
            reply_markup=_main_kb(),
        )
        return True


@router.message(CommandStart())
async def cmd_start(message: Message):
    args = (message.text or "").split(maxsplit=1)
    payload = args[1].strip() if len(args) == 2 else ""

    # Deep-link: /start chq_XXX или /start cheque_XXX
    if payload.startswith("chq_") or payload.startswith("cheque_"):
        code = payload.split("_", 1)[1]
        if await _try_redeem_cheque(message, code):
            return

    # Реферал: /start ref_12345
    if payload.startswith("ref_"):
        m = re.match(r"ref_(\d+)", payload)
        if m:
            referrer_tg_id = int(m.group(1))
            logger.info("[/start] user=%s referrer=%s",
                        message.from_user.id if message.from_user else "?",
                        referrer_tg_id)
            # TODO: запись referral в БД

    await message.answer(WELCOME_TEXT, reply_markup=_main_kb())


@router.callback_query(F.data == "howto")
async def cb_howto(call):
    text = (
        "<b>Как работает PRIDE P2P</b>\n\n"
        "<b>1. Регистрация</b>\n"
        "Жми «Открыть PRIDE P2P» — авто-вход через Telegram.\n\n"
        "<b>2. Кошелёк</b>\n"
        "Депозит / вывод / переводы между юзерами / свап монет.\n\n"
        "<b>3. P2P-рынок</b>\n"
        "Покупка/продажа USDT за рубли у других пользователей с эскроу.\n\n"
        "<b>4. Чеки</b>\n"
        "Создай чек на любую сумму → отправь ссылку → получатель активирует одним кликом.\n"
        "Команда в чате: <code>@PrideP2P_bot 5 USDT за пиццу</code>"
    )
    await call.message.answer(text)
    await call.answer()

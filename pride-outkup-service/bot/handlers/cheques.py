"""Cheques v2 — Crypto-Bot-style:

1. Inline mode: пользователь пишет в любом чате `@PrideP2P_bot 5 за пиццу`
   → бот предлагает inline-результат: «Создать чек 5 USDT — за пиццу»
   → при клике юзера: создаётся чек (списание с баланса),
   в чат уходит сообщение с deep-link «Активировать чек».

2. Команда /cheque (синоним /checks) — открывает Mini-App с разделом чеков.

Активация чека идёт через /start chq_<code> (см. start.py).
"""
from __future__ import annotations

import logging
import re
import secrets
import string
from decimal import Decimal

from aiogram import Router
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle,
    InputTextMessageContent,
)
from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.models import Cheque, User

router = Router(name="cheques_inline")
logger = logging.getLogger(__name__)

# Парсер inline-запроса: "5", "5 USDT", "5.5 USDT за пиццу", "5 за пиццу"
INLINE_PATTERN = re.compile(
    r"^\s*(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<coin>USDT|USDC|TON|TRX|BTC|ETH|SOL|BNB|DOGE|LTC)?\s*"
    r"(?P<comment>.*?)\s*$",
    re.IGNORECASE,
)


def _gen_code(n: int = 16) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _bot_username() -> str:
    return (settings.bot_username or "PrideP2P_bot").lstrip("@")


@router.inline_query()
async def on_inline(q: InlineQuery):
    """Юзер пишет `@PrideP2P_bot <amount> [coin] [comment]` в любом чате."""
    text = (q.query or "").strip()
    m = INLINE_PATTERN.match(text) if text else None
    if not m:
        # Подсказка
        await q.answer(
            results=[
                InlineQueryResultArticle(
                    id="hint",
                    title="Введите сумму чека",
                    description="Например: 5 USDT за пиццу",
                    input_message_content=InputTextMessageContent(
                        message_text="Пример: <code>@PrideP2P_bot 5 USDT за пиццу</code>",
                        parse_mode="HTML",
                    ),
                )
            ],
            cache_time=1, is_personal=True,
        )
        return

    try:
        amount = Decimal(m.group("amount"))
    except Exception:
        return
    coin = (m.group("coin") or "USDT").upper()
    comment = (m.group("comment") or "").strip()[:200]

    tg_id = q.from_user.id

    # Создаём чек (списываем с баланса юзера атомарно)
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if not u:
            await q.answer(
                results=[
                    InlineQueryResultArticle(
                        id="noacc",
                        title="Сначала зарегистрируйся",
                        description="Откройте бота и нажмите /start",
                        input_message_content=InputTextMessageContent(
                            message_text=f"Сначала открой @{_bot_username()} и нажми /start",
                        ),
                    )
                ],
                cache_time=1, is_personal=True,
            )
            return

        from core.services import balance_service
        try:
            bal = await balance_service.get_balance(db, u.id, coin)
        except Exception:
            bal = Decimal("0")
        # Legacy USDT fallback
        if coin == "USDT" and Decimal(str(bal or 0)) == 0 and Decimal(str(u.balance_usdt or 0)) > 0:
            bal = Decimal(str(u.balance_usdt))

        if Decimal(str(bal or 0)) < amount:
            await q.answer(
                results=[
                    InlineQueryResultArticle(
                        id="lowbal",
                        title=f"Недостаточно {coin}",
                        description=f"Баланс: {float(bal or 0)} {coin}, нужно {float(amount)}",
                        input_message_content=InputTextMessageContent(
                            message_text=f"Не хватает {coin} для чека. Пополни кошелёк через @{_bot_username()}.",
                        ),
                    )
                ],
                cache_time=1, is_personal=True,
            )
            return

        # Списываем и создаём чек
        try:
            code = _gen_code()
            while (await db.execute(select(Cheque).where(Cheque.code == code))).scalar_one_or_none():
                code = _gen_code()
            await balance_service.debit(
                db, u.id, coin, amount,
                op_type="cheque_lock", note=f"cheque {code}",
                ref_table="cheques",
            )
            cq = Cheque(
                creator_user_id=u.id,
                coin_code=coin,
                amount=amount,
                code=code,
                comment=comment or None,
                status="active",
            )
            db.add(cq)
            await db.commit()
        except Exception as e:
            logger.exception("[inline-cheque] create failed: %s", e)
            await q.answer(
                results=[
                    InlineQueryResultArticle(
                        id="err",
                        title="Ошибка создания чека",
                        description=str(e)[:80],
                        input_message_content=InputTextMessageContent(
                            message_text="Не получилось создать чек. Попробуй позже.",
                        ),
                    )
                ],
                cache_time=1, is_personal=True,
            )
            return

    deep = f"https://t.me/{_bot_username()}?start=chq_{code}"
    comment_line = f"\n<i>{comment}</i>" if comment else ""
    text_msg = (
        f"<b>Чек {amount} {coin}</b>{comment_line}\n\n"
        f"Активируй одним кликом ↓"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Активировать {amount} {coin}", url=deep)],
    ])

    await q.answer(
        results=[
            InlineQueryResultArticle(
                id=code,
                title=f"Создать чек {amount} {coin}",
                description=(f"«{comment}»  " if comment else "") + f"→ {amount} {coin}",
                input_message_content=InputTextMessageContent(
                    message_text=text_msg, parse_mode="HTML",
                ),
                reply_markup=kb,
            )
        ],
        cache_time=1, is_personal=True,
    )

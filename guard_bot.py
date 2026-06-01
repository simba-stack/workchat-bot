"""@PrideGuard_bot — изолированный 2FA-бот для подтверждения крупных выплат.

Архитектура (security-first):
  • Отдельный TG-бот с своим токеном (GUARD_BOT_TOKEN в Railway env)
  • Минимальные права: знает только OWNER_TG_ID (TRON_OWNER_TG_ID)
  • При попытке отправить выплату ≥ tfa_threshold_usdt → выплата ставится
    в статус awaiting_2fa, генерится 6-значный код, шлётся в @PrideGuard_bot
  • Owner вводит код в чате с ботом → если ок → выплата разблокируется и
    process_withdrawal продолжает send_usdt_to
  • 3 неверных попытки или таймаут (default 5 мин) → выплата отменяется

Это отдельный слой защиты:
  Hot wallet key → Railway → atomic transfer → @PrideGuard_bot code
  Даже если Railway скомпрометирован, атакующий не знает 6-значный код.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)

logger = logging.getLogger(__name__)

router = Router()

_guard_bot_instance: Optional[Bot] = None


def get_guard_bot() -> Optional[Bot]:
    return _guard_bot_instance


def _is_owner(message_or_call) -> bool:
    """Только owner (TRON_OWNER_TG_ID) может общаться с этим ботом."""
    try:
        from tron_payouts import get_owner_tg_id
        owner_id = get_owner_tg_id()
    except Exception:
        owner_id = 0
    user = (
        message_or_call.from_user
        if hasattr(message_or_call, "from_user") and message_or_call.from_user
        else None
    )
    if not user:
        return False
    return owner_id and int(user.id) == int(owner_id)


@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_start(message: Message):
    if not _is_owner(message):
        await message.reply(
            "🔒 Доступ запрещён.\n\n"
            "Этот бот — личный 2FA-страж PRIDE. "
            "Только для подтверждения крупных выплат."
        )
        return
    await message.reply(
        "🛡 <b>@PrideGuard — 2FA-страж PRIDE</b>\n\n"
        "Я отправляю 6-значные коды подтверждения для выплат от $1000.\n\n"
        "Когда придёт код — просто ответь им в этот чат "
        "(или нажми кнопку «✅ Подтвердить»).\n\n"
        "Если коды приходят без твоего ведома — кто-то пытается "
        "вывести средства. Нажми /panic чтобы заморозить все выплаты."
    )


@router.message(Command("panic"))
async def cmd_panic(message: Message):
    """Аварийная заморозка всех авто-выплат."""
    if not _is_owner(message):
        return
    try:
        from storage import storage
        # Включаем kill-switch через set_payout_safety (он сам берёт _lock)
        try:
            await storage.set_payout_safety(auto_pay_enabled_global=False)
        except Exception:
            # fallback: прямо в state
            cur = storage.state.setdefault("payout_safety", {})
            cur["auto_pay_enabled_global"] = False
            await storage.save()
        await message.reply(
            "🚨 <b>PANIC MODE ACTIVATED</b>\n\n"
            "Все авто-выплаты заморожены (auto_pay_enabled_global=False).\n"
            "Все 2FA-запросы будут отклоняться.\n\n"
            "Чтобы возобновить — JARVIS → Настройки → Финансы → kill-switch OFF."
        )
        # Также отменим все pending 2FA-запросы + связанные withdrawals
        reqs = storage.state.get("pending_2fa_requests") or {}
        cnt = 0
        for rid, r in list(reqs.items()):
            if r.get("status") == "pending":
                await storage.expire_2fa_request(rid)
                wd_id = r.get("withdraw_req_id") or ""
                if wd_id:
                    try:
                        await storage.cancel_withdrawal(wd_id, by="panic")
                    except Exception:
                        pass
                cnt += 1
        if cnt:
            await message.reply(f"🚫 Отменено {cnt} pending 2FA-запросов + связанные заявки.")
    except Exception as e:
        logger.exception("panic failed: %s", e)
        await message.reply(f"❌ Ошибка panic-режима: {e}")


@router.callback_query(F.data.startswith("2fa_confirm:"))
async def cb_2fa_confirm(call: CallbackQuery):
    if not _is_owner(call):
        await call.answer("Нет доступа", show_alert=True)
        return
    request_id = call.data.split(":", 1)[1]
    from storage import storage
    r = storage.get_2fa_request(request_id)
    if not r:
        await call.answer("Запрос не найден или истёк", show_alert=True)
        return
    if r.get("status") != "pending":
        await call.answer(f"Статус: {r.get('status')}", show_alert=True)
        return
    await call.message.reply(
        f"🔐 Введи 6-значный код для подтверждения выплаты:\n"
        f"💸 {r.get('amount_usdt'):.2f} USDT → <code>{r.get('address')}</code>"
    )
    await call.answer()


# Универсальный handler: текст из 6 цифр — попытка верификации
@router.message(F.text.regexp(r"^\d{6}$"))
async def handle_2fa_code(message: Message):
    if not _is_owner(message):
        return
    code = (message.text or "").strip()
    from storage import storage
    # Берём САМЫЙ свежий pending 2FA-запрос
    pending = [
        r for r in (storage.state.get("pending_2fa_requests") or {}).values()
        if r.get("status") == "pending"
    ]
    if not pending:
        await message.reply("ℹ️ Нет активных 2FA-запросов.")
        return
    pending.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    req = pending[0]
    request_id = req.get("request_id")
    result = await storage.verify_2fa_code(request_id, code)
    if result == "ok":
        wd_id = req.get("withdraw_req_id") or ""
        amount = float(req.get("amount_usdt") or 0)
        addr = req.get("address") or ""
        await message.reply(
            f"✅ <b>Код верный.</b>\n\n"
            f"Запускаю выплату #{wd_id}: {amount:.2f} USDT → <code>{addr}</code>"
        )
        # Триггерим выплату — process_withdrawal видит что 2FA verified
        try:
            from auto_payouts_runner import process_withdrawal
            ok, tx_hash = await process_withdrawal(
                wd_id, approved_by=f"2fa:{message.from_user.id}",
            )
            if ok:
                await message.reply(
                    f"💸 <b>Выплата отправлена!</b>\n"
                    f"TX: <code>{tx_hash[:32]}…</code>"
                )
            else:
                await message.reply("❌ Выплата не прошла (см. JARVIS логи).")
        except Exception as e:
            logger.exception("[guard_bot] payout trigger failed: %s", e)
            await message.reply(f"❌ Ошибка выплаты: {e}")
    elif result == "wrong":
        settings = storage.get_balance_settings()
        max_att = int(settings.get("tfa_max_attempts") or 3)
        used = int(req.get("attempts") or 0) + 1
        await message.reply(
            f"❌ Неверный код. Попыток осталось: {max(0, max_att - used)}"
        )
    elif result == "locked":
        await message.reply(
            "🚫 <b>Превышено количество попыток.</b>\n"
            "Заявка на выплату заблокирована. Создай новую через JARVIS если нужно."
        )
        # Также отменяем саму заявку withdraw
        try:
            await storage.cancel_withdrawal(
                req.get("withdraw_req_id") or "",
                by=f"2fa_locked:{message.from_user.id}",
            )
            await storage.add_notification(
                type="error",
                text=f"🚨 2FA locked: 3 неверных попытки для выплаты "
                     f"{req.get('amount_usdt'):.2f}$. Заявка отменена.",
            )
        except Exception:
            pass
    elif result == "expired":
        await message.reply(
            "⏰ Код истёк (5 минут). Создай новую заявку на выплату."
        )
    else:
        await message.reply(f"❓ Статус: {result}")


async def send_2fa_code(request_id: str, code: str, amount: float, address: str) -> bool:
    """Шлёт OWNER'у новый 2FA-запрос в @PrideGuard_bot.

    Вызывается из auto_payouts_runner.process_withdrawal когда сумма ≥ threshold.
    """
    bot = get_guard_bot()
    if not bot:
        logger.warning("[guard_bot] not running — cannot send 2FA code")
        return False
    try:
        from tron_payouts import get_owner_tg_id
        owner_id = get_owner_tg_id()
    except Exception:
        owner_id = 0
    if not owner_id:
        logger.error("[guard_bot] TRON_OWNER_TG_ID не задан — некому слать код")
        return False
    text = (
        f"🔐 <b>2FA подтверждение выплаты</b>\n\n"
        f"💸 Сумма: <b>{amount:.2f} USDT</b>\n"
        f"📥 Адрес: <code>{address}</code>\n"
        f"🆔 Заявка: <code>{request_id}</code>\n\n"
        f"<b>Код:</b> <code>{code}</code>\n\n"
        f"<i>Введи этот код ответом в этот чат. "
        f"Если не ты — нажми /panic.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"2fa_confirm:{request_id}"),
        InlineKeyboardButton(text="🚨 PANIC", callback_data="2fa_panic"),
    ]])
    try:
        await bot.send_message(owner_id, text, reply_markup=kb)
        return True
    except Exception as e:
        logger.exception("[guard_bot] send_2fa_code failed: %s", e)
        return False


@router.callback_query(F.data == "2fa_panic")
async def cb_panic(call: CallbackQuery):
    if not _is_owner(call):
        await call.answer("Нет доступа", show_alert=True)
        return
    await cmd_panic(call.message)
    await call.answer("🚨 PANIC активирован")


async def run_guard_bot():
    """Запускается из bot.py параллельно. Если GUARD_BOT_TOKEN не задан — skip."""
    global _guard_bot_instance
    token = (os.environ.get("GUARD_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning("GUARD_BOT_TOKEN не задан — guard_bot не запущен (2FA отключена)")
        return
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    _guard_bot_instance = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        me = await bot.get_me()
        logger.info("Guard bot online: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.error("Guard bot get_me failed: %s", e)
        return
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    except Exception as e:
        logger.exception("guard_bot polling crashed: %s", e)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass

"""
calc/handlers.py — все хендлеры calc-бота (@PrideStatistic_bot).

Логика ролей:
- owner: tg_id в storage.state["owner_ids"] (задаётся ENV CALC_OWNER_IDS при старте)
- admin: user в admin_chat_id (задаётся /админнастройка в group)
- partner: partner_tg_id клиентского чата (полный доступ к своему чату)
- worker: партнёрский работник с granular perms

Auto-cleanup: клиентские команды-мусор (типа +100к О1) и промежуточные bot-ответы
удаляются через N сек. Заявки на выплату/реквизит — остаются.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ChatMemberUpdated,
)

from calc.storage import storage

logger = logging.getLogger("calc.handlers")
router = Router()

MSK = timezone(timedelta(hours=3))
CLEANUP_DELAY_SEC = 20  # автоудаление промежуточных сообщений


def today_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def is_group(chat_type: str) -> bool:
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


def is_owner_or_admin_msg(msg: Message) -> bool:
    if storage.is_owner(msg.from_user.id):
        return True
    if msg.chat.id == storage.get_admin_chat_id():
        return True
    return False


async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: int = CLEANUP_DELAY_SEC):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def _fmt_money_rub(x: float) -> str:
    return f"{int(x):,}".replace(",", " ")


def _fmt_money_usd(x: float) -> str:
    if abs(x - round(x)) < 0.01:
        return f"{int(round(x))}"
    return f"{x:.2f}"


def _parse_amount(text: str) -> Optional[float]:
    """+100000, +100к, +100k, +100.5к, +1.5кк → число рублей.
    Возвращает None если не парсится."""
    t = (text or "").strip().lower().replace(" ", "").replace(",", ".")
    if not t.startswith("+"):
        return None
    t = t[1:]
    mult = 1
    while t.endswith(("к", "k", "к")):  # к рус + k англ
        mult *= 1000
        t = t[:-1]
    if t.endswith("м") or t.endswith("m"):
        mult *= 1_000_000
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


# ---------- FSM ----------
class Setup(StatesGroup):
    wait_direction_name = State()
    wait_direction_pct = State()
    wait_rate = State()
    wait_wallet = State()
    wait_worker_username = State()
    wait_worker_role = State()
    wait_payout_wallet_edit = State()
    wait_requisite_note = State()
    wait_payout_amount = State()  # админ вводит фактическую сумму выплаты


# ============================================================
# РЕГИСТРАЦИЯ АДМИН-ЧАТА
# ============================================================
@router.message(Command("админнастройка", "adminsetup"))
async def cmd_admin_setup(message: Message):
    """В админ-чате: закрепляет этот чат как админский.
    В личке owner-а: показывает меню.
    В клиент-чате: показывает клиентские настройки."""
    uid = message.from_user.id
    if is_group(message.chat.type):
        # Если это ещё не админ-чат, но owner пишет — назначаем этот чат админским
        if storage.is_owner(uid) and storage.get_admin_chat_id() != message.chat.id:
            await storage.set_admin_chat(message.chat.id)
            await message.reply(
                f"✅ Этот чат назначен <b>админским</b> (id: <code>{message.chat.id}</code>).\n"
                "Все заявки на выплату/реквизиты будут приходить сюда."
            )
            return
        # Уже админский или зарегистрированный клиентский
        if message.chat.id == storage.get_admin_chat_id():
            return await message.reply(
                "Ты в админ-чате. Команды:\n"
                "<code>/курс &lt;chat_id&gt; &lt;число&gt;</code> — курс rub→usd\n"
                "<code>/напр &lt;chat_id&gt; &lt;имя&gt; &lt;%&gt;</code> — направление\n"
                "<code>/напр_вкл &lt;chat_id&gt; &lt;имя&gt;</code> · <code>/напр_выкл</code> · <code>/напр_удал</code>\n"
                "<code>/рассылка &lt;текст&gt;</code> — во все клиентские чаты\n"
                "<code>/чаты</code> — список клиентских чатов"
            )
        # Клиентский чат — показать настройки
        entry = storage.get_client_chat(message.chat.id)
        if entry:
            return await _show_client_setup(message, entry)
        return await message.reply(
            "Чат не зарегистрирован. Owner: <code>+партнёр @username</code>"
        )
    # DM
    if storage.is_owner(uid):
        return await message.reply(
            "DM owner. Команды:\n"
            "<code>/чаты</code>, <code>/курс &lt;chat_id&gt; &lt;число&gt;</code>, "
            "<code>/напр &lt;chat_id&gt; &lt;имя&gt; &lt;%&gt;</code>"
        )
    await message.reply("Команда для админ-чата.")


async def _show_client_setup(message: Message, entry: dict):
    dirs = entry.get("directions") or {}
    lines = [
        f"⚙️ <b>Настройки чата</b>",
        f"Партнёр: @{entry.get('partner_username') or '—'}",
        f"Курс: <b>{entry.get('rate') or '—'}</b>",
        f"Кошелёк TRC20: <code>{entry.get('wallet_trc20') or '—'}</code>",
        "",
        f"Направления ({len(dirs)}):",
    ]
    for d in dirs.values():
        onoff = "✅" if d.get("enabled") else "⛔"
        lines.append(f"  {onoff} {d.get('name')} — {d.get('commission_pct')}%")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Направление", callback_data="setup:add_dir")],
        [InlineKeyboardButton(text="💳 Изменить TRC20", callback_data="setup:set_wallet")],
        [InlineKeyboardButton(text="🔄 Список направлений", callback_data="setup:list_dirs")],
    ])
    await message.reply("\n".join(lines), reply_markup=kb)


# ============================================================
# +ПАРТНЁР @ник — регистрация клиентского чата
# ============================================================
@router.message(F.text.regexp(r"^\s*\+партнёр\s+@?\w+", flags=re.IGNORECASE))
async def cmd_add_partner(message: Message, bot: Bot):
    if not is_group(message.chat.type):
        return await message.reply("Работает только в группе.")
    # Только owner или член админ-чата
    if not is_owner_or_admin_msg(message):
        # Проверим, что автор входит в admin-чат
        admin_id = storage.get_admin_chat_id()
        if not admin_id:
            return await message.reply("Сначала owner должен назначить админ-чат: <code>/админнастройка</code>")
        try:
            member = await bot.get_chat_member(admin_id, message.from_user.id)
            if member.status not in ("creator", "administrator", "member"):
                return await message.reply("Только член админ-чата может регистрировать партнёра.")
        except Exception:
            return await message.reply("Только член админ-чата может регистрировать партнёра.")

    m = re.search(r"\+партнёр\s+@?(\w+)", message.text or "", flags=re.IGNORECASE)
    if not m:
        return
    partner_username = m.group(1)

    # Пытаемся резолвнуть tg_id партнёра через chat member (если он в этом чате)
    partner_tg_id = 0
    try:
        # aiogram >= 3: get_chat_administrators + iterate
        admins = await bot.get_chat_administrators(message.chat.id)
        for a in admins:
            if (a.user.username or "").lower() == partner_username.lower():
                partner_tg_id = a.user.id
                break
    except Exception:
        pass

    entry = await storage.register_client_chat(
        chat_id=message.chat.id,
        chat_title=message.chat.title or "",
        partner_tg_id=partner_tg_id,
        partner_username=partner_username,
    )
    tg_line = (f"tg_id: <code>{partner_tg_id}</code>" if partner_tg_id
               else "tg_id пока не найден (партнёр напишет в чате — подхватим)")
    await message.reply(
        f"✅ Чат зарегистрирован как клиентский.\n"
        f"Партнёр: @{partner_username}\n"
        f"{tg_line}\n\n"
        f"Дальше:\n"
        f"1) admin в админ-чате: <code>/курс {message.chat.id} 80</code>\n"
        f"2) admin: <code>/напр {message.chat.id} О1 20</code>\n"
        f"3) партнёр: <code>/начатьдень</code>\n"
        f"4) сумму пишешь: <code>+100к О1</code>"
    )


# Подхват tg_id партнёра когда он напишет в чате
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def _catch_partner_id(message: Message):
    """Если partner_tg_id=0, а автор совпадает с partner_username — сохраняем id.
    Идёт ПОСЛЕ всех других хендлеров (регистрируется последним)."""
    if not message.from_user or not message.from_user.username:
        return
    entry = storage.get_client_chat(message.chat.id)
    if not entry or entry.get("partner_tg_id"):
        return
    if (entry.get("partner_username") or "").lower() == message.from_user.username.lower():
        await storage.update_client_chat(
            message.chat.id, partner_tg_id=int(message.from_user.id)
        )
        logger.info("[calc] partner_tg_id resolved for %s: %s",
                    message.chat.id, message.from_user.id)


# ============================================================
# /начатьдень
# ============================================================
@router.message(Command("начатьдень", "startday"))
async def cmd_start_day(message: Message, bot: Bot):
    if not is_group(message.chat.type):
        return await message.reply("Команда для клиентского чата.")
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return await message.reply("Чат не зарегистрирован.")
    date_str = today_msk()
    await storage.start_day(message.chat.id, date_str)
    reply = await message.reply(
        f"☀️ День <b>{date_str}</b> начат. Пиши <code>+сумма НАПРАВЛЕНИЕ</code>."
    )
    asyncio.create_task(_delete_later(bot, message.chat.id, message.message_id, 60))
    asyncio.create_task(_delete_later(bot, message.chat.id, reply.message_id, 60))


# ============================================================
# ADMIN COMMANDS (в админ-чате или DM owner)
# ============================================================
def _target_chat_id_from_admin(message: Message) -> Optional[int]:
    """Извлекает chat_id из аргументов команды. Возвращает None если нет."""
    parts = (message.text or "").split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@router.message(Command("курс", "rate"))
async def cmd_set_rate(message: Message):
    if not is_owner_or_admin_msg(message):
        # Может быть partner в своём чате
        if is_group(message.chat.type):
            entry = storage.get_client_chat(message.chat.id)
            if entry and message.from_user.id == entry.get("partner_tg_id"):
                pass
            else:
                return
        else:
            return
    parts = (message.text or "").split()
    # Формат в админ: /курс <chat_id> <rate>
    # Формат в клиент-чате: /курс <rate>
    if is_group(message.chat.type) and message.chat.id != storage.get_admin_chat_id():
        # клиентский чат
        if len(parts) < 2:
            return await message.reply("<code>/курс 80</code>")
        try:
            rate = float(parts[1].replace(",", "."))
        except ValueError:
            return await message.reply("Курс — число.")
        target_chat = message.chat.id
    else:
        if len(parts) < 3:
            return await message.reply("<code>/курс &lt;chat_id&gt; &lt;число&gt;</code>")
        try:
            target_chat = int(parts[1])
            rate = float(parts[2].replace(",", "."))
        except ValueError:
            return await message.reply("chat_id/rate должны быть числами.")
    entry = storage.get_client_chat(target_chat)
    if not entry:
        return await message.reply(f"Чат <code>{target_chat}</code> не зарегистрирован.")
    await storage.update_client_chat(target_chat, rate=rate)
    await message.reply(f"✅ Курс для чата {target_chat}: <b>{rate}</b>")


@router.message(Command("напр"))
async def cmd_add_dir(message: Message):
    """/напр [chat_id] <имя> <%> — добавить/обновить направление."""
    parts = (message.text or "").split()
    # В админ-чате: /напр <chat_id> <name> <pct>
    # В клиент-чате партнёр: /напр <name> <pct>
    if is_group(message.chat.type) and message.chat.id != storage.get_admin_chat_id():
        entry = storage.get_client_chat(message.chat.id)
        if not entry:
            return
        if message.from_user.id != entry.get("partner_tg_id") and not is_owner_or_admin_msg(message):
            return await message.reply("Только партнёр или админ.")
        if len(parts) < 3:
            return await message.reply("<code>/напр О1 20</code>")
        name = parts[1].strip()
        try:
            pct = float(parts[2].replace(",", "."))
        except ValueError:
            return await message.reply("Процент — число.")
        target_chat = message.chat.id
    else:
        if not is_owner_or_admin_msg(message):
            return
        if len(parts) < 4:
            return await message.reply("<code>/напр &lt;chat_id&gt; &lt;имя&gt; &lt;%&gt;</code>")
        try:
            target_chat = int(parts[1])
        except ValueError:
            return await message.reply("chat_id — число.")
        name = parts[2].strip()
        try:
            pct = float(parts[3].replace(",", "."))
        except ValueError:
            return await message.reply("Процент — число.")
    if not storage.get_client_chat(target_chat):
        return await message.reply("Такого чата нет в базе.")
    await storage.set_direction(target_chat, name, pct, enabled=True)
    await message.reply(f"✅ Направление <b>{name}</b> — {pct}% (вкл)")


@router.message(Command("напр_вкл"))
async def cmd_dir_on(message: Message):
    await _toggle_dir_cmd(message, target_state=True)


@router.message(Command("напр_выкл"))
async def cmd_dir_off(message: Message):
    await _toggle_dir_cmd(message, target_state=False)


async def _toggle_dir_cmd(message: Message, target_state: bool):
    parts = (message.text or "").split()
    if is_group(message.chat.type) and message.chat.id != storage.get_admin_chat_id():
        entry = storage.get_client_chat(message.chat.id)
        if not entry:
            return
        if message.from_user.id != entry.get("partner_tg_id") and not is_owner_or_admin_msg(message):
            return
        if len(parts) < 2:
            return await message.reply("Укажи имя направления.")
        name = parts[1].strip()
        target_chat = message.chat.id
    else:
        if not is_owner_or_admin_msg(message):
            return
        if len(parts) < 3:
            return await message.reply("<code>/напр_вкл &lt;chat_id&gt; &lt;имя&gt;</code>")
        try:
            target_chat = int(parts[1])
        except ValueError:
            return await message.reply("chat_id — число.")
        name = parts[2].strip()
    entry = storage.get_client_chat(target_chat)
    d = (entry.get("directions") or {}).get(name)
    if not d:
        return await message.reply("Направление не найдено.")
    if bool(d.get("enabled")) != target_state:
        await storage.toggle_direction(target_chat, name)
    await message.reply(f"{'✅' if target_state else '⛔'} <b>{name}</b>: {'вкл' if target_state else 'выкл'}")


@router.message(Command("напр_удал"))
async def cmd_dir_del(message: Message):
    parts = (message.text or "").split()
    if is_group(message.chat.type) and message.chat.id != storage.get_admin_chat_id():
        entry = storage.get_client_chat(message.chat.id)
        if not entry:
            return
        if message.from_user.id != entry.get("partner_tg_id") and not is_owner_or_admin_msg(message):
            return
        if len(parts) < 2:
            return
        name = parts[1].strip()
        target_chat = message.chat.id
    else:
        if not is_owner_or_admin_msg(message):
            return
        if len(parts) < 3:
            return
        try:
            target_chat = int(parts[1])
        except ValueError:
            return
        name = parts[2].strip()
    ok = await storage.delete_direction(target_chat, name)
    await message.reply("🗑 Удалено" if ok else "Не найдено")


@router.message(Command("чаты"))
async def cmd_list_chats(message: Message):
    if not is_owner_or_admin_msg(message):
        return
    chats = storage.list_client_chats()
    if not chats:
        return await message.reply("Нет клиентских чатов.")
    lines = [f"<b>Клиентские чаты ({len(chats)}):</b>\n"]
    for c in chats:
        dirs = c.get("directions") or {}
        lines.append(
            f"<code>{c['chat_id']}</code> · {html.escape(c.get('chat_title') or '')}\n"
            f"  партнёр: @{c.get('partner_username') or '—'} · "
            f"курс: {c.get('rate') or '—'} · направлений: {len(dirs)}"
        )
    await message.reply("\n".join(lines))


@router.message(Command("рассылка", "broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    if not is_owner_or_admin_msg(message):
        return
    text = (message.text or "").split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("<code>/рассылка &lt;текст&gt;</code>")
    body = text[1]
    chats = storage.list_client_chats()
    sent = 0
    failed = 0
    for c in chats:
        try:
            await bot.send_message(c["chat_id"], body)
            sent += 1
        except Exception as e:
            logger.warning("[calc broadcast] fail %s: %s", c["chat_id"], e)
            failed += 1
        await asyncio.sleep(0.05)
    await message.reply(f"📣 Отправлено: <b>{sent}</b>, ошибок: {failed}")


# ============================================================
# +СУММА НАПРАВЛЕНИЕ (в клиент-чате)
# ============================================================
@router.message(F.chat.type.in_({"group", "supergroup"}) & F.text.regexp(r"^\s*\+\d"))
async def handle_amount_input(message: Message, bot: Bot):
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return
    text = (message.text or "").strip()
    # Формат: +сумма направление
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return  # без направления игнор
    amount = _parse_amount(parts[0])
    if amount is None or amount <= 0:
        return
    dir_name = parts[1].strip()
    directions = entry.get("directions") or {}
    if dir_name not in directions:
        reply = await message.reply(
            f"❓ Направления <b>{html.escape(dir_name)}</b> нет.\n"
            f"Доступные: {', '.join(directions.keys()) or '—'}"
        )
        asyncio.create_task(_delete_later(bot, message.chat.id, message.message_id))
        asyncio.create_task(_delete_later(bot, message.chat.id, reply.message_id))
        return
    if not directions[dir_name].get("enabled"):
        reply = await message.reply(f"⛔ <b>{dir_name}</b> сейчас выключено.")
        asyncio.create_task(_delete_later(bot, message.chat.id, message.message_id))
        asyncio.create_task(_delete_later(bot, message.chat.id, reply.message_id))
        return
    await storage.add_stat_entry(
        chat_id=message.chat.id,
        date_str=today_msk(),
        amount_rub=amount,
        direction=dir_name,
        author_id=message.from_user.id,
        author_username=message.from_user.username or "",
    )
    reply = await message.reply(
        f"✅ +{_fmt_money_rub(amount)}₽ → <b>{dir_name}</b>"
    )
    # Удаляем и вход и ответ через N сек — мусор
    asyncio.create_task(_delete_later(bot, message.chat.id, message.message_id))
    asyncio.create_task(_delete_later(bot, message.chat.id, reply.message_id))


# ============================================================
# /стата
# ============================================================
@router.message(Command("стата", "stats"))
async def cmd_stats(message: Message):
    if not is_group(message.chat.type):
        return
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return
    await _send_stats(message, entry)


async def _send_stats(message: Message, entry: dict):
    s = storage.compute_stats(entry["chat_id"])
    if not s:
        return await message.reply("Нет данных.")
    rate = s["rate"]
    total_rub = s["total_rub"]
    by_dir_rub = s["by_direction_rub"]
    by_dir_usd = s["by_direction_usd"]
    dir_pcts = s["dir_pcts"]
    total_usd = s["total_usd_before_pay"]
    paid = s["paid_usd"]
    remaining = s["remaining_usd"]

    lines = [
        "<b>[PRIDE] Панель партнёра</b>",
        f"Статистика {today_msk()}",
        f"Курс: <b>{rate or '—'}</b>",
        "",
        f"💰 Общая в рублях: <b>{_fmt_money_rub(total_rub)} руб.</b>",
    ]
    if len(by_dir_rub) > 1:
        lines.append("\n<b>По направлениям:</b>")
        for d, amt in by_dir_rub.items():
            pct = dir_pcts.get(d, 0)
            usd = by_dir_usd.get(d, 0)
            lines.append(
                f"  <b>{d}</b>: {_fmt_money_rub(amt)}₽ − {pct:g}% = "
                f"{_fmt_money_rub(amt * (1 - pct/100))}₽ / {rate or '—'} = "
                f"<b>{_fmt_money_usd(usd)}$</b>"
            )
    elif len(by_dir_rub) == 1:
        d, amt = next(iter(by_dir_rub.items()))
        pct = dir_pcts.get(d, 0)
        after = amt * (1 - pct/100)
        lines.append(
            f"  Расчёт: {_fmt_money_rub(amt)} − {pct:g}% = "
            f"{_fmt_money_rub(after)} / {rate or '—'} = "
            f"<b>{_fmt_money_usd(by_dir_usd.get(d,0))}$</b>"
        )
    lines.append(f"\n💵 Общая выплата: <b>{_fmt_money_usd(total_usd)}$</b>")
    lines.append(f"✅ Выплачено: <b>{_fmt_money_usd(paid)}$</b>")
    lines.append(f"🎯 Остаток: <b>{_fmt_money_usd(remaining)}$</b>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Запросить выплату", callback_data="pay:request")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="stats:setup")],
    ])
    await message.reply("\n".join(lines), reply_markup=kb)


# ============================================================
# /статус — направления + запросить реквизит
# ============================================================
@router.message(Command("статус", "status"))
async def cmd_status(message: Message):
    if not is_group(message.chat.type):
        return
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return
    dirs = entry.get("directions") or {}
    lines = ["<b>📋 Доступные направления:</b>\n"]
    enabled_dirs = [d for d in dirs.values() if d.get("enabled")]
    if not enabled_dirs:
        lines.append("Пока нет активных направлений.")
    for d in dirs.values():
        onoff = "✅" if d.get("enabled") else "⛔"
        lines.append(f"  {onoff} <b>{d.get('name')}</b> — {d.get('commission_pct')}%")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Запросить реквизит", callback_data="req:start")],
    ]) if enabled_dirs else None
    await message.reply("\n".join(lines), reply_markup=kb)


# ============================================================
# /профиль — партнёрский профиль + работники
# ============================================================
@router.message(Command("профиль", "profile"))
async def cmd_profile(message: Message):
    if not is_group(message.chat.type):
        return
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return
    s = storage.compute_stats(entry["chat_id"])
    workers = entry.get("workers") or {}
    lines = [
        "<b>👤 Профиль партнёра</b>",
        f"Партнёр: @{entry.get('partner_username') or '—'}",
        f"Общая сумма: <b>{_fmt_money_rub(s.get('total_rub') or 0)} руб.</b>",
        f"Выплачено: <b>{_fmt_money_usd(s.get('paid_usd') or 0)}$</b>",
        f"Работников: <b>{len(workers)}</b>",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Мои работники", callback_data="prof:workers")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="stats:setup")],
    ])
    await message.reply("\n".join(lines), reply_markup=kb)


# ============================================================
# CALLBACKS
# ============================================================
def _check_partner_or_perm(entry: dict, user_id: int, perm: str) -> bool:
    if user_id == entry.get("partner_tg_id"):
        return True
    workers = entry.get("workers") or {}
    w = workers.get(str(int(user_id)))
    if w and (w.get("perms") or {}).get(perm):
        return True
    return False


@router.callback_query(F.data == "stats:setup")
async def cb_stats_setup(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    await cb.answer()
    await _show_client_setup(cb.message, entry)


@router.callback_query(F.data == "setup:add_dir")
async def cb_setup_add_dir(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "add_directions"):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.set_state(Setup.wait_direction_name)
    await state.update_data(chat_id=cb.message.chat.id)
    await cb.message.reply("Название направления (напр. О1):")
    await cb.answer()


@router.message(Setup.wait_direction_name)
async def st_dir_name(message: Message, state: FSMContext, bot: Bot):
    name = (message.text or "").strip()
    if not name or len(name) > 32:
        return await message.reply("Плохое имя. Пришли ещё раз (до 32 симв).")
    await state.update_data(direction_name=name)
    await state.set_state(Setup.wait_direction_pct)
    await message.reply("Процент комиссии (число, напр. 20):")


@router.message(Setup.wait_direction_pct)
async def st_dir_pct(message: Message, state: FSMContext):
    try:
        pct = float((message.text or "").replace(",", "."))
    except ValueError:
        return await message.reply("Число.")
    data = await state.get_data()
    await storage.set_direction(data["chat_id"], data["direction_name"], pct, enabled=True)
    await state.clear()
    await message.reply(f"✅ <b>{data['direction_name']}</b> — {pct}%")


@router.callback_query(F.data == "setup:set_wallet")
async def cb_setup_wallet(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "set_wallet"):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.set_state(Setup.wait_wallet)
    await state.update_data(chat_id=cb.message.chat.id)
    await cb.message.reply("Пришли TRC20 адрес (начинается с T…):")
    await cb.answer()


@router.message(Setup.wait_wallet)
async def st_wallet(message: Message, state: FSMContext):
    w = (message.text or "").strip()
    if not (w.startswith("T") and 30 <= len(w) <= 40):
        return await message.reply("Не похоже на TRC20. Пришли валидный.")
    data = await state.get_data()
    await storage.update_client_chat(data["chat_id"], wallet_trc20=w)
    await state.clear()
    await message.reply(f"✅ Кошелёк: <code>{w}</code>")


@router.callback_query(F.data == "setup:list_dirs")
async def cb_setup_list(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    dirs = entry.get("directions") or {}
    if not dirs:
        return await cb.answer("Пусто", show_alert=True)
    rows = []
    for d in dirs.values():
        onoff = "✅" if d.get("enabled") else "⛔"
        rows.append([
            InlineKeyboardButton(
                text=f"{onoff} {d['name']} · {d['commission_pct']}%",
                callback_data=f"dir:tgl:{d['name']}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"dir:del:{d['name']}"),
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.reply("Направления (нажми чтобы вкл/выкл, 🗑 удалить):", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("dir:tgl:"))
async def cb_dir_tgl(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "add_directions"):
        return await cb.answer("Нет прав.", show_alert=True)
    name = cb.data.split(":", 2)[2]
    new_state = await storage.toggle_direction(cb.message.chat.id, name)
    await cb.answer(f"{name}: {'ВКЛ' if new_state else 'ВЫКЛ'}")


@router.callback_query(F.data.startswith("dir:del:"))
async def cb_dir_del(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "add_directions"):
        return await cb.answer("Нет прав.", show_alert=True)
    name = cb.data.split(":", 2)[2]
    ok = await storage.delete_direction(cb.message.chat.id, name)
    await cb.answer("Удалено" if ok else "Не найдено")


# --- PAYOUT REQUEST ---
@router.callback_query(F.data == "pay:request")
async def cb_pay_request(cb: CallbackQuery, state: FSMContext, bot: Bot):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "request_payout"):
        return await cb.answer("Нет прав на запрос выплаты.", show_alert=True)
    s = storage.compute_stats(entry["chat_id"])
    remaining = s.get("remaining_usd") or 0
    if remaining <= 0:
        return await cb.answer("Остаток к выплате = 0.", show_alert=True)
    wallet = entry.get("wallet_trc20") or ""
    if not wallet:
        await state.set_state(Setup.wait_payout_wallet_edit)
        await state.update_data(chat_id=cb.message.chat.id, amount=remaining)
        await cb.message.reply(
            f"Кошелёк не указан. Пришли TRC20 адрес чтобы создать заявку на {_fmt_money_usd(remaining)}$:"
        )
        return await cb.answer()
    # Есть кошелёк → показать подтверждение
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Запросить {_fmt_money_usd(remaining)}$",
            callback_data=f"pay:confirm:{int(remaining*100)}"
        )],
        [InlineKeyboardButton(text="✏️ Изменить адрес", callback_data="pay:edit_wallet")],
    ])
    await cb.message.reply(
        f"💸 <b>Заявка на выплату</b>\n"
        f"Сумма: <b>{_fmt_money_usd(remaining)}$</b>\n"
        f"На адрес: <code>{wallet}</code>",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "pay:edit_wallet")
async def cb_pay_edit_wallet(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "set_wallet"):
        return await cb.answer("Нет прав менять адрес.", show_alert=True)
    await state.set_state(Setup.wait_payout_wallet_edit)
    await state.update_data(chat_id=cb.message.chat.id, amount=None)
    await cb.message.reply("Пришли новый TRC20 адрес:")
    await cb.answer()


@router.message(Setup.wait_payout_wallet_edit)
async def st_pay_wallet(message: Message, state: FSMContext, bot: Bot):
    w = (message.text or "").strip()
    if not (w.startswith("T") and 30 <= len(w) <= 40):
        return await message.reply("Не похоже на TRC20.")
    data = await state.get_data()
    chat_id = data["chat_id"]
    amount = data.get("amount")
    await storage.update_client_chat(chat_id, wallet_trc20=w)
    await state.clear()
    if amount:
        # сразу создаём заявку
        await _create_and_send_payout(
            bot, chat_id, amount, w,
            message.from_user.id, message.from_user.username or "",
        )
        await message.reply(f"✅ Заявка на {_fmt_money_usd(amount)}$ создана.")
    else:
        await message.reply(f"✅ Адрес обновлён: <code>{w}</code>")


@router.callback_query(F.data.startswith("pay:confirm:"))
async def cb_pay_confirm(cb: CallbackQuery, bot: Bot):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "request_payout"):
        return await cb.answer("Нет прав.", show_alert=True)
    amount = int(cb.data.split(":")[2]) / 100.0
    wallet = entry.get("wallet_trc20") or ""
    if not wallet:
        return await cb.answer("Нет кошелька.", show_alert=True)
    await _create_and_send_payout(
        bot, cb.message.chat.id, amount, wallet,
        cb.from_user.id, cb.from_user.username or "",
    )
    await cb.answer("Заявка создана.")


async def _create_and_send_payout(
    bot: Bot, chat_id: int, amount: float, wallet: str,
    user_id: int, username: str,
):
    entry = storage.get_client_chat(chat_id)
    req = await storage.create_payout_request(
        chat_id=chat_id, amount_usd=amount, wallet=wallet,
        requested_by_id=user_id, requested_by_name=username,
    )
    # Клиенту
    client_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"pay:cancel:{req['id']}")],
    ])
    client_msg = await bot.send_message(
        chat_id,
        f"💸 <b>Заявка на выплату #{req['id']}</b>\n"
        f"Сумма: <b>{_fmt_money_usd(amount)}$</b>\n"
        f"Адрес: <code>{wallet}</code>\n"
        f"Статус: ⏳ ожидает подтверждения",
        reply_markup=client_kb,
    )
    # Админам
    admin_id = storage.get_admin_chat_id()
    if admin_id:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💰 Выплата произведена", callback_data=f"pay:done:{req['id']}"
            )],
            [InlineKeyboardButton(
                text="❌ Отклонить", callback_data=f"pay:reject:{req['id']}"
            )],
        ])
        admin_msg = await bot.send_message(
            admin_id,
            f"🔔 <b>Новая заявка на выплату #{req['id']}</b>\n"
            f"Чат: <code>{chat_id}</code> · {html.escape(entry.get('chat_title') or '')}\n"
            f"Партнёр: @{entry.get('partner_username') or '—'}\n"
            f"Запросил: @{username or '—'} (<code>{user_id}</code>)\n"
            f"Сумма: <b>{_fmt_money_usd(amount)}$</b>\n"
            f"Адрес: <code>{wallet}</code>",
            reply_markup=admin_kb,
        )
        await storage.update_payout_request(
            req["id"],
            client_msg_id=client_msg.message_id,
            admin_msg_id=admin_msg.message_id,
        )


@router.callback_query(F.data.startswith("pay:cancel:"))
async def cb_pay_cancel(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    req = storage.get_payout_request(pid)
    if not req or req.get("status") != "pending":
        return await cb.answer("Уже обработана.", show_alert=True)
    entry = storage.get_client_chat(req["chat_id"])
    if not _check_partner_or_perm(entry, cb.from_user.id, "request_payout"):
        return await cb.answer("Нет прав.", show_alert=True)
    await storage.update_payout_request(pid, status="cancelled")
    try:
        await bot.edit_message_text(
            f"💸 <b>Заявка #{pid}</b> — <s>отменена</s>",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    admin_id = storage.get_admin_chat_id()
    if admin_id and req.get("admin_msg_id"):
        try:
            await bot.edit_message_text(
                f"❌ Заявка #{pid} отменена клиентом.",
                chat_id=admin_id, message_id=req["admin_msg_id"],
            )
        except Exception:
            pass
    await cb.answer("Отменено.")


@router.callback_query(F.data.startswith("pay:reject:"))
async def cb_pay_reject(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    pid = int(cb.data.split(":")[2])
    req = storage.get_payout_request(pid)
    if not req or req.get("status") != "pending":
        return await cb.answer("Уже обработана.", show_alert=True)
    await storage.update_payout_request(pid, status="cancelled")
    try:
        await cb.message.edit_text(f"❌ Заявка #{pid} отклонена админом.")
    except Exception:
        pass
    try:
        await bot.edit_message_text(
            f"❌ Заявка #{pid} отклонена.",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("pay:done:"))
async def cb_pay_done(cb: CallbackQuery, state: FSMContext):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    pid = int(cb.data.split(":")[2])
    req = storage.get_payout_request(pid)
    if not req or req.get("status") != "pending":
        return await cb.answer("Уже обработана.", show_alert=True)
    await state.set_state(Setup.wait_payout_amount)
    await state.update_data(payout_id=pid)
    await cb.message.reply(
        f"Укажи <b>фактическую</b> сумму выплаты в $ по заявке #{pid} "
        f"(запрошено {_fmt_money_usd(req['amount_usd'])}$):"
    )
    await cb.answer()


@router.message(Setup.wait_payout_amount)
async def st_payout_amount(message: Message, state: FSMContext, bot: Bot):
    try:
        amt = float((message.text or "").replace(",", "."))
    except ValueError:
        return await message.reply("Число.")
    if amt <= 0:
        return await message.reply("Больше нуля.")
    data = await state.get_data()
    pid = data["payout_id"]
    req = storage.get_payout_request(pid)
    if not req:
        await state.clear()
        return await message.reply("Заявка пропала.")
    await storage.record_payout(
        chat_id=req["chat_id"], amount_usd=amt,
        note=f"payout_req_id={pid}", admin_id=message.from_user.id,
    )
    await storage.update_payout_request(pid, status="paid", paid_amount_usd=amt)
    await state.clear()
    await message.reply(f"✅ Записано: <b>{_fmt_money_usd(amt)}$</b> клиенту.")
    # обновить сообщения
    try:
        await bot.edit_message_text(
            f"✅ Заявка #{pid} — оплачено <b>{_fmt_money_usd(amt)}$</b>",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    if req.get("admin_msg_id"):
        try:
            await bot.edit_message_text(
                f"✅ Заявка #{pid} — оплачено {_fmt_money_usd(amt)}$",
                chat_id=storage.get_admin_chat_id(),
                message_id=req["admin_msg_id"],
            )
        except Exception:
            pass


# --- REQUISITE REQUEST ---
@router.callback_query(F.data == "req:start")
async def cb_req_start(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    dirs = [d for d in (entry.get("directions") or {}).values() if d.get("enabled")]
    if not dirs:
        return await cb.answer("Нет активных направлений.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=d["name"], callback_data=f"req:pick:{d['name']}")]
        for d in dirs
    ])
    await cb.message.reply("Выбери направление:", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("req:pick:"))
async def cb_req_pick(cb: CallbackQuery, state: FSMContext):
    direction = cb.data.split(":", 2)[2]
    await state.set_state(Setup.wait_requisite_note)
    await state.update_data(chat_id=cb.message.chat.id, direction=direction)
    await cb.message.reply(f"Направление: <b>{direction}</b>\nПришли примечание (текстом):")
    await cb.answer()


@router.message(Setup.wait_requisite_note)
async def st_req_note(message: Message, state: FSMContext, bot: Bot):
    note = (message.text or "").strip()
    if not note or len(note) > 500:
        return await message.reply("Примечание 1-500 символов.")
    data = await state.get_data()
    await state.clear()
    entry = storage.get_client_chat(data["chat_id"])
    req = await storage.create_requisite_request(
        chat_id=data["chat_id"], direction=data["direction"], note=note,
        requested_by_id=message.from_user.id,
        requested_by_name=message.from_user.username or "",
    )
    # Клиенту
    client_msg = await message.reply(
        f"📞 <b>Заявка на реквизит #{req['id']}</b>\n"
        f"Направление: <b>{data['direction']}</b>\n"
        f"Примечание: {html.escape(note)}\n"
        f"Статус: ⏳ ожидает"
    )
    # Админам
    admin_id = storage.get_admin_chat_id()
    if admin_id:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправлено", callback_data=f"req:done:{req['id']}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req:reject:{req['id']}")],
        ])
        admin_msg = await bot.send_message(
            admin_id,
            f"🔔 <b>Заявка на реквизит #{req['id']}</b>\n"
            f"Чат: <code>{data['chat_id']}</code> · "
            f"{html.escape(entry.get('chat_title') or '')}\n"
            f"Партнёр: @{entry.get('partner_username') or '—'}\n"
            f"Запросил: @{message.from_user.username or '—'}\n"
            f"Направление: <b>{data['direction']}</b>\n"
            f"Примечание: {html.escape(note)}",
            reply_markup=admin_kb,
        )
        await storage.update_requisite_request(
            req["id"],
            client_msg_id=client_msg.message_id,
            admin_msg_id=admin_msg.message_id,
        )


@router.callback_query(F.data.startswith("req:done:"))
async def cb_req_done(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    rid = int(cb.data.split(":")[2])
    req = storage.get_requisite_request(rid)
    if not req or req.get("status") != "pending":
        return await cb.answer("Уже обработано.", show_alert=True)
    await storage.update_requisite_request(rid, status="done")
    try:
        await cb.message.edit_text(f"✅ Заявка на реквизит #{rid} — обработана.")
    except Exception:
        pass
    try:
        await bot.edit_message_text(
            f"✅ Заявка на реквизит #{rid} обработана.",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("req:reject:"))
async def cb_req_reject(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    rid = int(cb.data.split(":")[2])
    req = storage.get_requisite_request(rid)
    if not req or req.get("status") != "pending":
        return await cb.answer("Уже обработано.", show_alert=True)
    await storage.update_requisite_request(rid, status="cancelled")
    try:
        await cb.message.edit_text(f"❌ Заявка #{rid} отклонена.")
    except Exception:
        pass
    try:
        await bot.edit_message_text(
            f"❌ Заявка #{rid} отклонена.",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    await cb.answer()


# --- WORKERS ---
@router.callback_query(F.data == "prof:workers")
async def cb_prof_workers(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if cb.from_user.id != entry.get("partner_tg_id") and not is_owner_or_admin_msg(cb.message):
        return await cb.answer("Только партнёр.", show_alert=True)
    workers = entry.get("workers") or {}
    lines = [f"<b>Работники ({len(workers)}):</b>"]
    rows = []
    for w in workers.values():
        p = w.get("perms") or {}
        badges = []
        if p.get("set_wallet"): badges.append("💳")
        if p.get("add_directions"): badges.append("➕")
        if p.get("request_payout"): badges.append("💸")
        lines.append(f"  @{w.get('username') or '—'} · {w.get('role')} · {''.join(badges) or '—'}")
        rows.append([
            InlineKeyboardButton(
                text=f"⚙️ @{w.get('username') or w['tg_id']}",
                callback_data=f"wrk:menu:{w['tg_id']}",
            ),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить работника", callback_data="wrk:add")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.reply("\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "wrk:add")
async def cb_wrk_add(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if cb.from_user.id != entry.get("partner_tg_id"):
        return await cb.answer("Только партнёр.", show_alert=True)
    await state.set_state(Setup.wait_worker_username)
    await state.update_data(chat_id=cb.message.chat.id)
    await cb.message.reply("Пришли @username работника (он должен написать в этот чат хотя бы раз):")
    await cb.answer()


@router.message(Setup.wait_worker_username)
async def st_wrk_uname(message: Message, state: FSMContext, bot: Bot):
    uname = (message.text or "").strip().lstrip("@")
    if not uname or not re.match(r"^\w{3,32}$", uname):
        return await message.reply("Плохой username.")
    # Пытаемся резолвнуть tg_id через админов чата (или ищем в /getChatMember по username)
    tg_id = 0
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        for a in admins:
            if (a.user.username or "").lower() == uname.lower():
                tg_id = a.user.id
                break
    except Exception:
        pass
    if not tg_id:
        # ищем в message.reply_to или последних участниках — не 100%
        await message.reply(
            f"⚠️ Не могу найти tg_id @{uname} в этом чате. "
            f"Пусть напишет хоть одно сообщение — потом снова добавь."
        )
        await state.clear()
        return
    await state.update_data(worker_tg_id=tg_id, worker_username=uname)
    await state.set_state(Setup.wait_worker_role)
    await message.reply(f"@{uname} (id {tg_id}). Роль (напр. Помощник):")


@router.message(Setup.wait_worker_role)
async def st_wrk_role(message: Message, state: FSMContext):
    role = (message.text or "").strip()[:64]
    if not role:
        return await message.reply("Пусто.")
    data = await state.get_data()
    w = await storage.add_worker(
        chat_id=data["chat_id"],
        worker_tg_id=data["worker_tg_id"],
        username=data["worker_username"],
        role=role,
        perms={"set_wallet": False, "add_directions": False, "request_payout": False},
    )
    await state.clear()
    await message.reply(
        f"✅ Работник @{w['username']} · {w['role']} добавлен.\n"
        f"Разрешения выключены — открой /профиль → Работники → ⚙️"
    )


@router.callback_query(F.data.startswith("wrk:menu:"))
async def cb_wrk_menu(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or cb.from_user.id != entry.get("partner_tg_id"):
        return await cb.answer("Только партнёр.", show_alert=True)
    wid = int(cb.data.split(":")[2])
    w = (entry.get("workers") or {}).get(str(wid))
    if not w:
        return await cb.answer("Не найдено.", show_alert=True)
    p = w.get("perms") or {}
    def m(k, label):
        return f"{'✅' if p.get(k) else '⬜'} {label}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=m("set_wallet", "Устанавливать адрес"), callback_data=f"wrk:tgl:{wid}:set_wallet")],
        [InlineKeyboardButton(text=m("add_directions", "Добавлять направления"), callback_data=f"wrk:tgl:{wid}:add_directions")],
        [InlineKeyboardButton(text=m("request_payout", "Запрашивать выплату"), callback_data=f"wrk:tgl:{wid}:request_payout")],
        [InlineKeyboardButton(text="🗑 Удалить работника", callback_data=f"wrk:del:{wid}")],
    ])
    await cb.message.reply(f"⚙️ @{w.get('username')} · {w.get('role')}", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("wrk:tgl:"))
async def cb_wrk_tgl(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or cb.from_user.id != entry.get("partner_tg_id"):
        return await cb.answer("Только партнёр.", show_alert=True)
    _, _, wid_s, perm = cb.data.split(":", 3)
    wid = int(wid_s)
    new_state = await storage.toggle_worker_perm(cb.message.chat.id, wid, perm)
    await cb.answer(f"{perm}: {'ВКЛ' if new_state else 'ВЫКЛ'}")


@router.callback_query(F.data.startswith("wrk:del:"))
async def cb_wrk_del(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or cb.from_user.id != entry.get("partner_tg_id"):
        return await cb.answer("Только партнёр.", show_alert=True)
    wid = int(cb.data.split(":")[2])
    await storage.remove_worker(cb.message.chat.id, wid)
    await cb.answer("Удалён.")
    try:
        await cb.message.edit_text("🗑 Работник удалён.")
    except Exception:
        pass


# ============================================================
# /старт /help — для DM/новых чатов
# ============================================================
@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply(
            "👋 <b>PRIDE Статистика</b>\n"
            "Я калькулятор партнёров. Добавь меня в свой рабочий чат — "
            "owner напишет <code>+партнёр @твой_ник</code>.\n\n"
            "После регистрации в чате доступны: /стата, /статус, /выплата, /профиль"
        )
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return await message.reply(
            "Чат не зарегистрирован.\n"
            "Owner: <code>/админнастройка</code> — назначить админ-чат.\n"
            "Или в клиентском чате: <code>+партнёр @ник</code>"
        )
    await message.reply(
        "Команды: /стата · /статус · /выплата · /профиль · /начатьдень · /настройка\n"
        "Сумма: <code>+100к О1</code>"
    )


# ============================================================
# /выплата — алиас на pay:request
# ============================================================
@router.message(Command("выплата", "payout"))
async def cmd_payout(message: Message, state: FSMContext, bot: Bot):
    if not is_group(message.chat.type):
        return
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return
    if not _check_partner_or_perm(entry, message.from_user.id, "request_payout"):
        return await message.reply("Нет прав на запрос выплаты.")
    s = storage.compute_stats(entry["chat_id"])
    remaining = s.get("remaining_usd") or 0
    if remaining <= 0:
        return await message.reply(f"Остаток к выплате = 0.")
    wallet = entry.get("wallet_trc20") or ""
    if not wallet:
        await state.set_state(Setup.wait_payout_wallet_edit)
        await state.update_data(chat_id=message.chat.id, amount=remaining)
        return await message.reply(
            f"Кошелёк не указан. Пришли TRC20 адрес чтобы создать заявку на {_fmt_money_usd(remaining)}$:"
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Запросить {_fmt_money_usd(remaining)}$",
            callback_data=f"pay:confirm:{int(remaining*100)}"
        )],
        [InlineKeyboardButton(text="✏️ Изменить адрес", callback_data="pay:edit_wallet")],
    ])
    await message.reply(
        f"💸 <b>Заявка на выплату</b>\n"
        f"Сумма: <b>{_fmt_money_usd(remaining)}$</b>\n"
        f"На адрес: <code>{wallet}</code>",
        reply_markup=kb,
    )


# /настройка алиас на клиентский setup
@router.message(Command("настройка", "setup"))
async def cmd_setup(message: Message):
    if not is_group(message.chat.type):
        return
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return await message.reply("Чат не зарегистрирован.")
    await _show_client_setup(message, entry)

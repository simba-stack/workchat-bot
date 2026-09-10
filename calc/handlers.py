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


async def _ensure_partner_tg_id(chat_id: int, user_id: int, username: str) -> None:
    """Если у клиентского чата partner_tg_id=0, а автор совпадает с partner_username —
    записываем его tg_id. Вызывать при каждом сообщении/клике в клиентском чате."""
    if not user_id or not username:
        return
    entry = storage.get_client_chat(chat_id)
    if not entry or entry.get("partner_tg_id"):
        return
    if (entry.get("partner_username") or "").lower() == username.lower():
        await storage.update_client_chat(chat_id, partner_tg_id=user_id)
        logger.info(
            "[calc] auto-resolved partner_tg_id via click: chat=%s user=%s",
            chat_id, user_id,
        )


@router.callback_query.outer_middleware()
async def _resolve_partner_on_callback(handler, event: CallbackQuery, data):
    """Перед любым callback резолвим partner_tg_id если ещё не задан."""
    try:
        if event.message and event.from_user and event.from_user.username:
            await _ensure_partner_tg_id(
                event.message.chat.id, event.from_user.id, event.from_user.username
            )
    except Exception:
        pass
    return await handler(event, data)


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


async def _delete_now(bot: Bot, chat_id: int, message_ids: list[int]):
    """Мгновенное удаление списка сообщений (для очистки FSM-мусора)."""
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def _track_msg(state: FSMContext, msg: Message):
    """Добавляет message_id в state[msgs_to_delete] для последующей очистки."""
    data = await state.get_data()
    ids = list(data.get("_trash_msgs") or [])
    ids.append(msg.message_id)
    await state.update_data(_trash_msgs=ids)


def _close_kb() -> InlineKeyboardMarkup:
    """Клавиатура только с кнопкой Закрыть — для добавления к любому bot-ответу."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")]
    ])


def _with_close(kb: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    """Добавляет к существующей клавиатуре ряд с «Закрыть»."""
    rows = list(kb.inline_keyboard) if kb else []
    # Не дублируем если уже есть
    for r in rows:
        for b in r:
            if b.callback_data == "ui:close":
                return kb or _close_kb()
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _cleanup_fsm(bot: Bot, chat_id: int, state: FSMContext):
    """Удаляет все сообщения, собранные через _track_msg."""
    data = await state.get_data()
    ids = list(data.get("_trash_msgs") or [])
    if ids:
        await _delete_now(bot, chat_id, ids)


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
    wait_stream_name = State()    # партнёр: имя своего направления
    wait_stream_trc20 = State()   # партнёр: TRC20 для этого направления
    wait_stream_trc20_edit = State()  # редактирование адреса существующего
    wait_team_name = State()          # название команды (напр. "Львята")


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


async def _show_client_setup(message: Message, entry: dict, user_id: int = 0):
    dirs = entry.get("directions") or {}
    if not user_id:
        user_id = message.from_user.id if message.from_user else 0
    is_owner_user = storage.is_owner(user_id)
    is_partner_user = user_id == entry.get("partner_tg_id")
    team = entry.get("team_name") or ""
    lines = [
        f"⚙️ <b>Настройки чата</b>",
        f"🦁 Команда: <b>{team or '—'}</b>",
        f"Партнёр: @{entry.get('partner_username') or '—'}",
        f"Курс: <b>{entry.get('rate') or '—'}</b>",
        "",
        f"<b>Способы приёма админа ({len(dirs)}):</b>",
    ]
    if not dirs:
        lines.append("  <i>пусто — админ должен добавить направления</i>")
    for d in dirs.values():
        onoff = "✅" if d.get("enabled") else "⛔"
        lines.append(f"  {onoff} <b>{d.get('name')}</b> — {d.get('commission_pct')}%")

    kb_rows = []
    # Название команды — партнёр или owner
    if is_partner_user or is_owner_user:
        kb_rows.append([InlineKeyboardButton(text="🦁 Название команды", callback_data="setup:team")])
    # Направления партнёра — партнёр или owner
    if is_partner_user or is_owner_user:
        kb_rows.append([InlineKeyboardButton(text="📍 Мои направления", callback_data="prof:streams")])
    # Способы приёма (с %) — только owner
    if is_owner_user:
        kb_rows.append([InlineKeyboardButton(text="➕ Способ приёма", callback_data="setup:add_dir")])
        kb_rows.append([InlineKeyboardButton(text="🔄 Список способов", callback_data="setup:list_dirs")])
    if not is_owner_user:
        lines.append("\n<i>ℹ️ Способы приёма и % настраивает админ.</i>")
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.reply("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "setup:team")
async def cb_setup_team(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not (cb.from_user.id == entry.get("partner_tg_id") or storage.is_owner(cb.from_user.id)):
        return await cb.answer("Только партнёр.", show_alert=True)
    await state.set_state(Setup.wait_team_name)
    await state.update_data(chat_id=cb.message.chat.id)
    prompt = await cb.message.reply(
        "Как называется твоя команда? (напр. <b>Львята</b>, <b>PRIDE Team</b>)"
    )
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_team_name)
async def st_team_name(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    name = (message.text or "").strip()
    if not name or len(name) > 40:
        err = await message.reply("Плохое название (до 40 симв).")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    await storage.update_client_chat(data["chat_id"], team_name=name)
    await _cleanup_fsm(bot, message.chat.id, state)
    await state.clear()
    final = await bot.send_message(
        message.chat.id, f"✅ Команда: <b>{html.escape(name)}</b>"
    )
    asyncio.create_task(_delete_later(bot, message.chat.id, final.message_id, 15))


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
    # В клиентский чат — минимум
    await message.reply(
        f"✅ Чат зарегистрирован как клиентский.\n"
        f"Партнёр: @{partner_username}",
        reply_markup=_close_kb(),
    )
    # В админ-чат — подробная инструкция
    admin_id = storage.get_admin_chat_id()
    if admin_id and admin_id != message.chat.id:
        tg_line = (f"tg_id: <code>{partner_tg_id}</code>" if partner_tg_id
                   else "tg_id: <i>подхватится когда партнёр напишет в чате</i>")
        try:
            await bot.send_message(
                admin_id,
                f"🆕 <b>Новый клиентский чат</b>\n"
                f"🏢 {html.escape(message.chat.title or '—')}\n"
                f"🆔 <code>{message.chat.id}</code>\n"
                f"👤 Партнёр: @{partner_username}\n"
                f"{tg_line}\n\n"
                f"<b>Настройка:</b>\n"
                f"<code>/курс {message.chat.id} 80</code>\n"
                f"<code>/напр {message.chat.id} О1 20</code>\n"
                f"<code>/напр {message.chat.id} О2 15</code>\n\n"
                f"Или прямо в клиентском чате как owner: <code>/напр О1 20</code>",
                reply_markup=_close_kb(),
            )
        except Exception as e:
            logger.warning("[calc] failed to notify admin_chat about new client: %s", e)


# _catch_partner_id перенесён в САМЫЙ КОНЕЦ файла чтобы не перехватывать команды


# ============================================================
# /начатьдень
# ============================================================
_DEFAULT_STARTDAY_TEXT = (
    "☀️ <b>Админ на связи!</b>\n"
    "📅 {date} — приём открыт.\n"
    "Пишите суммы: <code>+сумма НАПРАВЛЕНИЕ СПОСОБ</code>"
)
_DEFAULT_ENDDAY_TEXT = (
    "🌙 <b>Приём закрыт</b> — админ ушёл спать.\n"
    "Заявки на выплату/реквизит обработаются с утра."
)


def _get_saved_template(key: str, default: str) -> str:
    return storage.state.get(key) or default


async def _save_template(key: str, text: str) -> None:
    from calc.storage import _lock as _st_lock
    async with _st_lock:
        storage.state[key] = text
        await storage._save_unlocked()


def _build_day_report(date_str: str) -> str:
    """Собирает отчёт за день по всем клиентам.
    Показывает: обороты, выплаты, разбивку по партнёрам и направлениям."""
    chats = storage.list_client_chats()
    total_rub_all = 0.0
    total_usd_all = 0.0
    total_paid_all = 0.0
    payouts_all: list[tuple] = []  # (partner, stream, amt_usd, ts)
    partner_summary: dict[str, dict] = {}  # partner_username → {rub, usd, paid}

    for c in chats:
        s = storage.compute_stats(c["chat_id"], date_str=date_str)
        rub_today = s.get("total_rub") or 0
        usd_today = s.get("total_usd_before_pay") or 0
        # Выплаты только за date_str
        paid_today = 0.0
        by_dir_today: dict[str, float] = {}
        for p in c.get("payouts") or []:
            ts = p.get("ts") or 0
            d = datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d")
            if d != date_str:
                continue
            amt = float(p.get("amount_usd") or 0)
            paid_today += amt
            stream = p.get("stream") or "—"
            by_dir_today[stream] = by_dir_today.get(stream, 0) + amt
            payouts_all.append((
                c.get("team_name") or c.get("partner_username") or "—",
                stream, amt, ts
            ))
        if not (rub_today or paid_today):
            continue
        total_rub_all += rub_today
        total_usd_all += usd_today
        total_paid_all += paid_today
        key = c.get("team_name") or f"@{c.get('partner_username') or c.get('chat_id')}"
        partner_summary[key] = {
            "rub": rub_today,
            "usd": usd_today,
            "paid": paid_today,
            "by_dir_paid": by_dir_today,
            "streams_rub": s.get("by_stream_rub") or {},
            "chat_id": c["chat_id"],
        }

    lines = [
        f"📊 <b>Итог дня — {date_str}</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Оборот всего: <b>{_fmt_money_rub(total_rub_all)} ₽</b>",
        f"💵 Насчитано: <b>{_fmt_money_usd(total_usd_all)}$</b>",
        f"✅ Выплачено: <b>{_fmt_money_usd(total_paid_all)}$</b>",
        f"👥 Активных клиентов: <b>{len(partner_summary)}</b>",
        "",
    ]
    if not partner_summary:
        lines.append("<i>Ничего не было.</i>")
        return "\n".join(lines)

    lines.append("<b>👤 По партнёрам:</b>")
    for key in sorted(partner_summary.keys(),
                      key=lambda k: partner_summary[k]["rub"], reverse=True):
        p = partner_summary[key]
        streams = p["streams_rub"]
        stream_line = ""
        if streams:
            top = sorted(streams.items(), key=lambda x: x[1], reverse=True)
            stream_line = " · ".join(f"{d} {_fmt_money_rub(v)}₽" for d, v in top[:3])
        lines.append(
            f"\n  🦁 <b>{html.escape(str(key))}</b>\n"
            f"     💰 {_fmt_money_rub(p['rub'])}₽ = {_fmt_money_usd(p['usd'])}$  ·  "
            f"✅ {_fmt_money_usd(p['paid'])}$"
        )
        if stream_line:
            lines.append(f"     📍 {stream_line}")
        # По направлениям выплат
        if p["by_dir_paid"]:
            dir_str = " · ".join(
                f"{d}: {_fmt_money_usd(v)}$"
                for d, v in sorted(p["by_dir_paid"].items(), key=lambda x: x[1], reverse=True)
            )
            lines.append(f"     💸 выплат: {dir_str}")
    return "\n".join(lines)


@router.message(Command("итогдня", "итог", "dayreport"))
async def cmd_day_report(message: Message):
    """/итогдня — сводка за сегодня для админа."""
    if not is_owner_or_admin_msg(message):
        return
    parts = (message.text or "").split()
    date_str = parts[1] if len(parts) >= 2 else today_msk()
    report = _build_day_report(date_str)
    await message.reply(report, reply_markup=_close_kb())


@router.message(Command("обновитьдень", "новыйдень", "закрытьдень"))
async def cmd_close_day(message: Message, bot: Bot):
    """/обновитьдень — рассылает "день закрыт" + показывает админу отчёт."""
    admin_id = storage.get_admin_chat_id()
    if not is_group(message.chat.type) or message.chat.id != admin_id:
        return
    if not is_owner_or_admin_msg(message):
        return
    date_str = today_msk()
    # Рассылка клиентам
    text = _get_saved_template("endday_text", _DEFAULT_ENDDAY_TEXT)
    chats = storage.list_client_chats()
    sent = 0
    failed = 0
    for c in chats:
        try:
            await bot.send_message(c["chat_id"], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    # Отчёт админу
    report = _build_day_report(date_str)
    await message.reply(
        f"🌙 День <b>{date_str}</b> закрыт.\n"
        f"Рассылка: <b>{sent}</b> чатов, ошибок: {failed}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n{report}",
        reply_markup=_close_kb(),
    )


@router.message(Command("удалитьчата", "удалитьклиента", "deletechat"))
async def cmd_delete_chat(message: Message):
    """/удалитьчата <chat_id> — полное удаление клиентского чата из БД (owner)."""
    if not storage.is_owner(message.from_user.id):
        return await message.reply("Только owner.")
    parts = (message.text or "").split()
    if len(parts) < 2:
        chats = storage.list_client_chats()
        lines = ["<b>Формат:</b> <code>/удалитьчата &lt;chat_id&gt;</code>\n\nСписок:"]
        for c in chats:
            team = c.get("team_name") or c.get("partner_username") or "—"
            lines.append(f"  <code>{c['chat_id']}</code> · {team} · @{c.get('partner_username') or '—'}")
        return await message.reply("\n".join(lines), reply_markup=_close_kb())
    try:
        chat_id = int(parts[1])
    except ValueError:
        return await message.reply("chat_id — число.")
    entry = storage.get_client_chat(chat_id)
    if not entry:
        return await message.reply(f"Чата <code>{chat_id}</code> нет в базе.")
    team = entry.get("team_name") or entry.get("partner_username") or "—"
    ok = await storage.delete_client_chat(chat_id)
    await message.reply(
        f"🗑 <b>Удалено:</b> <code>{chat_id}</code> · {team}" if ok else "Не удалось.",
        reply_markup=_close_kb(),
    )


@router.message(Command("сбросчата", "resetchat"))
async def cmd_reset_chat(message: Message):
    """/сбросчата <chat_id> — обнуляет статистику (days + payouts) чата, сохраняет настройки."""
    if not storage.is_owner(message.from_user.id):
        return await message.reply("Только owner.")
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.reply("Формат: <code>/сбросчата &lt;chat_id&gt;</code>")
    try:
        chat_id = int(parts[1])
    except ValueError:
        return await message.reply("chat_id — число.")
    ok = await storage.reset_client_stats(chat_id)
    await message.reply(
        f"♻️ Стата чата <code>{chat_id}</code> обнулена. Направления/курс сохранены."
        if ok else "Чат не найден.",
        reply_markup=_close_kb(),
    )


@router.message(Command("шаблоны", "templates"))
async def cmd_templates(message: Message):
    """Показать сохранённые шаблоны рассылки. Только owner/admin."""
    if not is_owner_or_admin_msg(message):
        return
    sd = _get_saved_template("startday_text", _DEFAULT_STARTDAY_TEXT)
    ed = _get_saved_template("endday_text", _DEFAULT_ENDDAY_TEXT)
    await message.reply(
        f"<b>📄 Шаблоны рассылок</b>\n\n"
        f"<b>☀️ Начало дня:</b>\n{sd}\n\n"
        f"<b>🌙 Конец дня:</b>\n{ed}\n\n"
        f"<i>Заменить: /начатьдень &lt;новый текст&gt; или /конецдня &lt;новый текст&gt;</i>\n"
        f"<i>Плейсхолдер {{date}} заменится на сегодняшнюю дату.</i>",
        reply_markup=_close_kb(),
    )


@router.message(Command("конецдня", "endday", "закончитьдень"))
async def cmd_end_day(message: Message, bot: Bot):
    """В админ-чате — рассылает всем клиентам "приём закрыт".
    Если после команды есть текст — рассылает его и сохраняет как шаблон.
    Без текста — используется последний сохранённый (или дефолтный)."""
    admin_id = storage.get_admin_chat_id()
    if not is_group(message.chat.type) or message.chat.id != admin_id:
        return
    if not is_owner_or_admin_msg(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    custom = parts[1].strip() if len(parts) >= 2 else ""
    if custom:
        text = custom
        await _save_template("endday_text", custom)
    else:
        text = _get_saved_template("endday_text", _DEFAULT_ENDDAY_TEXT)
    chats = storage.list_client_chats()
    sent = 0
    failed = 0
    for c in chats:
        try:
            await bot.send_message(c["chat_id"], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.reply(
        f"🌙 Конец дня объявлен. Разослано: <b>{sent}</b>, ошибок: {failed}\n"
        f"<i>Текст сохранён как шаблон.</i>" if custom else
        f"🌙 Конец дня объявлен. Разослано: <b>{sent}</b>, ошибок: {failed}",
        reply_markup=_close_kb(),
    )


@router.message(Command("начатьдень", "startday"))
async def cmd_start_day(message: Message, bot: Bot):
    """В админ-чате — рассылает всем клиентам "админ на связи, день начат".
    В остальных чатах — ничего не делает."""
    admin_id = storage.get_admin_chat_id()
    if not is_group(message.chat.type) or message.chat.id != admin_id:
        return
    if not is_owner_or_admin_msg(message):
        return
    date_str = today_msk()
    parts = (message.text or "").split(maxsplit=1)
    custom = parts[1].strip() if len(parts) >= 2 else ""
    if custom:
        text = custom.replace("{date}", date_str)
        await _save_template("startday_text", custom)
    else:
        text = _get_saved_template("startday_text", _DEFAULT_STARTDAY_TEXT).replace("{date}", date_str)
    chats = storage.list_client_chats()
    sent = 0
    failed = 0
    for c in chats:
        try:
            await bot.send_message(c["chat_id"], text)
            await storage.start_day(c["chat_id"], date_str)
            sent += 1
        except Exception as e:
            logger.warning("[calc startday] fail %s: %s", c["chat_id"], e)
            failed += 1
        await asyncio.sleep(0.05)
    await message.reply(
        f"☀️ День {date_str} объявлен.\n"
        f"Разослано: <b>{sent}</b> чатов  ·  Ошибок: {failed}"
        + ("\n<i>Текст сохранён как шаблон.</i>" if custom else ""),
        reply_markup=_close_kb(),
    )


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
    await message.reply(f"✅ Курс для чата {target_chat}: <b>{rate}</b>", reply_markup=_close_kb())


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
    await message.reply(f"✅ Направление <b>{name}</b> — {pct}% (вкл)", reply_markup=_close_kb())


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
    await message.reply(
        f"{'✅' if target_state else '⛔'} <b>{name}</b>: {'вкл' if target_state else 'выкл'}",
        reply_markup=_close_kb(),
    )


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
    await message.reply("🗑 Удалено" if ok else "Не найдено", reply_markup=_close_kb())


@router.message(Command("чаты"))
async def cmd_list_chats(message: Message):
    if not is_owner_or_admin_msg(message):
        return
    chats = storage.list_client_chats()
    if not chats:
        return await message.reply(
            "🦁 <b>PRIDE · Клиентская база</b>\n\n"
            "<i>Партнёров ещё нет.</i>\n"
            "Зарегистрируй первого: добавь бота в клиентский чат и напиши "
            "<code>+партнёр @nick</code>"
        )

    # Считаем агрегаты по каждому чату
    rows = []
    total_rub_all = 0.0
    total_usd_all = 0.0
    total_paid_all = 0.0
    total_remaining_all = 0.0
    active_count = 0
    pending_payouts = len([
        p for p in (storage.state.get("pending_payouts") or [])
        if p.get("status") == "pending"
    ])
    pending_reqs = len([
        r for r in (storage.state.get("pending_requisites") or [])
        if r.get("status") == "pending"
    ])

    for c in chats:
        s = storage.compute_stats(c["chat_id"])
        total_rub_all += s.get("total_rub") or 0
        total_usd_all += s.get("total_usd_before_pay") or 0
        total_paid_all += s.get("paid_usd") or 0
        total_remaining_all += s.get("remaining_usd") or 0
        if s.get("total_rub"):
            active_count += 1
        rows.append((c, s))

    # Сортируем по остатку выплаты (крупные должники сверху)
    rows.sort(key=lambda r: r[1].get("remaining_usd") or 0, reverse=True)

    lines = [
        "🦁 <b>PRIDE · Клиентская база</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"👥 Партнёров: <b>{len(chats)}</b>  ·  🔥 Активных: <b>{active_count}</b>",
        f"💰 Общий оборот: <b>{_fmt_money_rub(total_rub_all)} ₽</b>",
        f"💵 Насчитано: <b>{_fmt_money_usd(total_usd_all)}$</b>  ·  "
        f"✅ Выплачено: <b>{_fmt_money_usd(total_paid_all)}$</b>",
        f"🎯 <b>Долг партнёрам: {_fmt_money_usd(total_remaining_all)}$</b>",
    ]
    if pending_payouts or pending_reqs:
        lines.append(
            f"⚡ Ожидают: 💸 <b>{pending_payouts}</b> выплат  ·  "
            f"📞 <b>{pending_reqs}</b> реквизитов"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━\n")

    for i, (c, s) in enumerate(rows, 1):
        dirs = c.get("directions") or {}
        dirs_on = sum(1 for d in dirs.values() if d.get("enabled"))
        team = c.get("team_name") or ""
        title_raw = (c.get("chat_title") or "").strip() or "без названия"
        title = html.escape(team or title_raw)
        partner = c.get("partner_username") or "—"
        rate = c.get("rate") or 0
        wallet_badge = "💳" if c.get("wallet_trc20") else "⚠️"
        rub = s.get("total_rub") or 0
        usd_total = s.get("total_usd_before_pay") or 0
        paid = s.get("paid_usd") or 0
        remaining = s.get("remaining_usd") or 0

        # Медалька по объёму
        if i == 1 and rub > 0:
            badge = "🥇"
        elif i == 2 and rub > 0:
            badge = "🥈"
        elif i == 3 and rub > 0:
            badge = "🥉"
        else:
            badge = f"<b>{i}.</b>"

        # Статус: если есть долг — 🔴, если всё выплачено — 🟢, если пусто — ⚪
        if remaining > 0.01:
            status = "🔴"
        elif rub > 0:
            status = "🟢"
        else:
            status = "⚪"

        # По направлениям — короткая строка
        by_dir = s.get("by_direction_rub") or {}
        dir_line = ""
        if by_dir:
            top_dirs = sorted(by_dir.items(), key=lambda x: x[1], reverse=True)[:3]
            dir_line = " · ".join(f"{d}: {_fmt_money_rub(v)}" for d, v in top_dirs)
            if len(by_dir) > 3:
                dir_line += f" +{len(by_dir)-3}"

        block = [
            f"{badge} {status} <b>{title}</b>",
            f"   👤 @{partner}  ·  💱 {rate or '—'}  ·  {wallet_badge} {dirs_on}/{len(dirs)} напр.",
            f"   💰 <b>{_fmt_money_rub(rub)} ₽</b>  →  "
            f"💵 {_fmt_money_usd(usd_total)}$  |  "
            f"✅ {_fmt_money_usd(paid)}$  |  🎯 <b>{_fmt_money_usd(remaining)}$</b>",
        ]
        if dir_line:
            block.append(f"   📊 {dir_line}")
        block.append(f"   <code>{c['chat_id']}</code>")
        lines.append("\n".join(block))
        lines.append("")  # пустая строка между блоками

    lines.append(
        "<i>🔴 есть долг  ·  🟢 всё выплачено  ·  ⚪ нет оборота</i>\n"
        "<i>💳 адрес есть  ·  ⚠️ адрес не указан</i>"
    )

    # Инлайн-кнопки для детального просмотра по чатам с оборотом
    active_rows = [(c, s) for c, s in rows if s.get("total_rub")]
    kb = None
    if active_rows:
        kb_rows = []
        for c, s in active_rows[:8]:  # макс 8 кнопок
            title_short = (c.get("chat_title") or "чат")[:20]
            remaining = s.get("remaining_usd") or 0
            kb_rows.append([InlineKeyboardButton(
                text=f"🔍 {title_short} · {_fmt_money_usd(remaining)}$",
                callback_data=f"cli:detail:{c['chat_id']}"
            )])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    text = "\n".join(lines)
    # Разбиваем если слишком длинно
    if len(text) > 3800:
        chunks = []
        cur = ""
        for line in lines:
            if len(cur) + len(line) > 3800:
                chunks.append(cur)
                cur = line + "\n"
            else:
                cur += line + "\n"
        if cur:
            chunks.append(cur)
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1 and kb:
                await message.reply(chunk, reply_markup=kb)
            else:
                await message.reply(chunk)
    else:
        await message.reply(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cli:detail:"))
async def cb_client_detail(cb: CallbackQuery):
    if cb.message.chat.id != storage.get_admin_chat_id() and not storage.is_owner(cb.from_user.id):
        return await cb.answer("Только для админ-чата.", show_alert=True)
    chat_id = int(cb.data.split(":")[2])
    c = storage.get_client_chat(chat_id)
    if not c:
        return await cb.answer("Чат не найден.", show_alert=True)
    s = storage.compute_stats(chat_id)
    dirs = c.get("directions") or {}
    workers = c.get("workers") or {}
    payouts = c.get("payouts") or []
    by_dir_rub = s.get("by_direction_rub") or {}
    by_dir_usd = s.get("by_direction_usd") or {}
    dir_pcts = s.get("dir_pcts") or {}

    lines = [
        f"🔍 <b>Детали партнёра</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏢 <b>{html.escape(c.get('chat_title') or '—')}</b>",
        f"👤 Партнёр: @{c.get('partner_username') or '—'}",
        f"🆔 <code>{chat_id}</code>",
        f"💱 Курс: <b>{c.get('rate') or '—'}</b>",
        f"💳 Кошелёк: <code>{c.get('wallet_trc20') or '—'}</code>",
        f"👥 Работников: {len(workers)}",
        "",
        f"💰 <b>Общий оборот: {_fmt_money_rub(s.get('total_rub') or 0)} ₽</b>",
        f"💵 Насчитано: {_fmt_money_usd(s.get('total_usd_before_pay') or 0)}$",
        f"✅ Выплачено: {_fmt_money_usd(s.get('paid_usd') or 0)}$",
        f"🎯 <b>Остаток: {_fmt_money_usd(s.get('remaining_usd') or 0)}$</b>",
    ]

    if by_dir_rub:
        lines.append("\n<b>📊 По направлениям:</b>")
        for d in sorted(by_dir_rub.keys(), key=lambda k: by_dir_rub[k], reverse=True):
            rub = by_dir_rub[d]
            pct = dir_pcts.get(d, 0)
            usd = by_dir_usd.get(d, 0)
            cfg = dirs.get(d) or {}
            onoff = "✅" if cfg.get("enabled") else "⛔"
            lines.append(
                f"  {onoff} <b>{d}</b>: {_fmt_money_rub(rub)}₽ − {pct:g}% = "
                f"<b>{_fmt_money_usd(usd)}$</b>"
            )

    if payouts:
        lines.append(f"\n<b>💸 Последние выплаты ({len(payouts)}):</b>")
        for p in payouts[-5:][::-1]:
            ts = datetime.fromtimestamp(p.get("ts") or 0, MSK).strftime("%d.%m %H:%M")
            lines.append(f"  {ts} · <b>{_fmt_money_usd(p.get('amount_usd') or 0)}$</b>")

    if workers:
        lines.append(f"\n<b>👥 Работники:</b>")
        for w in workers.values():
            p = w.get("perms") or {}
            badges = []
            if p.get("set_wallet"): badges.append("💳")
            if p.get("add_directions"): badges.append("➕")
            if p.get("request_payout"): badges.append("💸")
            lines.append(
                f"  @{w.get('username') or '—'} · {w.get('role') or '—'} · "
                f"{''.join(badges) or 'без прав'}"
            )

    await cb.message.reply("\n".join(lines))
    await cb.answer()


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
    await message.reply(f"📣 Отправлено: <b>{sent}</b>, ошибок: {failed}", reply_markup=_close_kb())


# ============================================================
# +СУММА НАПРАВЛЕНИЕ (в клиент-чате)
# ============================================================
@router.message(F.chat.type.in_({"group", "supergroup"}) & F.text.regexp(r"^\s*\+\d"))
async def handle_amount_input(message: Message, bot: Bot):
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return
    text = (message.text or "").strip()
    # Формат: +сумма <направление_партнёра> <способ_приёма>
    parts = text.split()
    if len(parts) < 3:
        reply = await message.reply(
            "❓ Формат: <code>+сумма НАПРАВЛЕНИЕ СПОСОБ</code>\n"
            "Пример: <code>+100к Мороженое ДАЧА</code>\n"
            "  • НАПРАВЛЕНИЕ = твой магазин (жми /профиль → 📍 Мои направления)\n"
            "  • СПОСОБ = способ приёма от админа (см. /статус)"
        )
        asyncio.create_task(_delete_later(bot, message.chat.id, message.message_id, 15))
        asyncio.create_task(_delete_later(bot, message.chat.id, reply.message_id, 15))
        return
    amount = _parse_amount(parts[0])
    if amount is None or amount <= 0:
        return
    stream_input = parts[1].strip()
    method_input = parts[2].strip()
    streams = entry.get("streams") or {}
    methods = entry.get("directions") or {}

    # Case-insensitive lookup: находим канонический ключ
    stream_name = next(
        (k for k in streams.keys() if k.lower() == stream_input.lower()),
        None,
    )
    method_name = next(
        (k for k in methods.keys() if k.lower() == method_input.lower()),
        None,
    )

    err_text = None
    if not stream_name:
        err_text = (
            f"❓ Твоего направления <b>{html.escape(stream_input)}</b> нет.\n"
            f"Твои направления: {', '.join(streams.keys()) or '—'}\n"
            f"Добавь: /профиль → 📍 Мои направления"
        )
    elif not streams[stream_name].get("enabled"):
        err_text = f"⛔ <b>{stream_name}</b> у тебя выключено."
    elif not method_name:
        err_text = (
            f"❓ Способа приёма <b>{html.escape(method_input)}</b> нет.\n"
            f"Доступные способы: {', '.join(methods.keys()) or '—'}"
        )
    elif not methods[method_name].get("enabled"):
        err_text = f"⛔ Способ <b>{method_name}</b> сейчас выключен."

    if err_text:
        reply = await message.reply(err_text)
        asyncio.create_task(_delete_later(bot, message.chat.id, message.message_id, 20))
        asyncio.create_task(_delete_later(bot, message.chat.id, reply.message_id, 20))
        return

    await storage.add_stat_entry(
        chat_id=message.chat.id,
        date_str=today_msk(),
        amount_rub=amount,
        stream=stream_name,
        payment_method=method_name,
        author_id=message.from_user.id,
        author_username=message.from_user.username or "",
    )
    reply = await message.reply(
        f"✅ +{_fmt_money_rub(amount)}₽ → <b>{stream_name}</b> · <i>{method_name}</i>"
    )
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
    by_stream_rub = s.get("by_stream_rub") or {}
    by_stream_usd = s.get("by_stream_usd") or {}
    by_stream_method_rub = s.get("by_stream_method_rub") or {}
    method_pcts = s.get("method_pcts") or {}
    paid_by_stream = s.get("paid_by_stream") or {}
    remaining_by_stream = s.get("remaining_by_stream") or {}
    total_usd = s["total_usd_before_pay"]
    paid = s["paid_usd"]
    remaining = s["remaining_usd"]

    team = entry.get("team_name") or "PRIDE · Панель партнёра"
    lines = [
        f"🦁 <b>{html.escape(team)}</b>",
        f"📅 {today_msk()}  ·  💱 Курс: <b>{rate or '—'}</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"💰 Общий оборот: <b>{_fmt_money_rub(total_rub)} ₽</b>",
        f"💵 Насчитано: <b>{_fmt_money_usd(total_usd)}$</b>",
        f"✅ Выплачено: <b>{_fmt_money_usd(paid)}$</b>",
    ]
    pending = s.get("pending_usd") or 0
    available = s.get("available_usd") or 0
    if pending > 0.01:
        lines.append(f"⏳ В очереди на выплату: <b>{_fmt_money_usd(pending)}$</b>")
    lines.append(f"🎯 <b>Доступно к запросу: {_fmt_money_usd(available)}$</b>")
    if pending > 0.01:
        lines.append(f"<i>(общий остаток {_fmt_money_usd(remaining)}$ = доступно + в очереди)</i>")

    if by_stream_rub:
        pending_by_stream = s.get("pending_by_stream") or {}
        available_by_stream = s.get("available_by_stream") or {}
        lines.append("\n<b>📍 По твоим направлениям:</b>")
        for stream in sorted(by_stream_rub.keys(), key=lambda k: by_stream_rub[k], reverse=True):
            rub = by_stream_rub[stream]
            usd = by_stream_usd.get(stream, 0)
            paid_s = paid_by_stream.get(stream, 0)
            pending_s = pending_by_stream.get(stream, 0)
            avail_s = available_by_stream.get(stream, 0)
            lines.append(
                f"\n  📍 <b>{stream}</b>: {_fmt_money_rub(rub)}₽ = "
                f"<b>{_fmt_money_usd(usd)}$</b>"
            )
            details = []
            if paid_s > 0.01:
                details.append(f"✅ {_fmt_money_usd(paid_s)}$")
            if pending_s > 0.01:
                details.append(f"⏳ {_fmt_money_usd(pending_s)}$")
            details.append(f"🎯 <b>{_fmt_money_usd(avail_s)}$</b>")
            lines.append(f"     " + "  ·  ".join(details))
            # Разбивка по способам приёма внутри направления
            methods = by_stream_method_rub.get(stream) or {}
            if len(methods) > 1 or (methods and list(methods.keys())[0] != "—"):
                for m, m_rub in sorted(methods.items(), key=lambda x: x[1], reverse=True):
                    pct = method_pcts.get(m, 0)
                    after = m_rub * (1 - pct/100)
                    m_usd = after / rate if rate > 0 else 0
                    lines.append(
                        f"     • <i>{m}</i>: {_fmt_money_rub(m_rub)}₽ −{pct:g}% = {_fmt_money_usd(m_usd)}$"
                    )

    if not total_rub:
        lines.append("\n<i>Ещё ничего не сдано. Пиши: <code>+100к НАПРАВЛЕНИЕ СПОСОБ</code></i>")

    kb_rows = []
    if remaining > 0.01:
        kb_rows.append([InlineKeyboardButton(text="💸 Запросить выплату", callback_data="pay:request")])
    kb_rows.append([InlineKeyboardButton(text="📍 Мои направления", callback_data="prof:streams")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
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
    kb_rows = []
    if enabled_dirs:
        kb_rows.append([InlineKeyboardButton(text="📞 Запросить реквизит", callback_data="req:start")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
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
    today = today_msk()
    month_start = today[:7] + "-01"  # напр. 2026-09-01
    s_all = storage.compute_stats(entry["chat_id"])
    s_month = storage.compute_stats(entry["chat_id"], date_from=month_start)
    s_day = storage.compute_stats(entry["chat_id"], date_str=today)
    workers = entry.get("workers") or {}
    streams = entry.get("streams") or {}
    team = entry.get("team_name") or ""

    def _line(label: str, s: dict) -> str:
        rub = s.get("total_rub") or 0
        paid = s.get("paid_usd") or 0
        usd = s.get("total_usd_before_pay") or 0
        avail = s.get("available_usd") or 0
        return (
            f"<b>{label}</b>\n"
            f"  💰 {_fmt_money_rub(rub)}₽ = {_fmt_money_usd(usd)}$\n"
            f"  ✅ выплачено: {_fmt_money_usd(paid)}$  ·  "
            f"🎯 остаток: {_fmt_money_usd(avail)}$"
        )

    lines = [
        "<b>👤 Профиль партнёра</b>",
        f"🦁 Команда: <b>{team or '— (задай в настройках)'}</b>",
        f"👤 Партнёр: @{entry.get('partner_username') or '—'}",
        f"📍 Направлений: <b>{len(streams)}</b>  ·  👥 Работников: <b>{len(workers)}</b>",
        "━━━━━━━━━━━━━━━━━━━",
        _line("📅 Сегодня", s_day),
        "",
        _line("📆 За месяц", s_month),
        "",
        _line("📊 За всё время", s_all),
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 Мои направления", callback_data="prof:streams")],
        [InlineKeyboardButton(text="👥 Мои работники", callback_data="prof:workers")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="stats:setup")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])
    await message.reply("\n".join(lines), reply_markup=kb)


# ============================================================
# ПАРТНЁРСКИЕ НАПРАВЛЕНИЯ (streams)
# ============================================================
@router.callback_query(F.data == "prof:streams")
async def cb_prof_streams(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    is_partner = cb.from_user.id == entry.get("partner_tg_id")
    is_owner = storage.is_owner(cb.from_user.id)
    can_add = is_partner or is_owner or _check_partner_or_perm(entry, cb.from_user.id, "add_directions")
    streams = entry.get("streams") or {}
    lines = [f"<b>📍 Мои направления ({len(streams)}):</b>"]
    if not streams:
        lines.append("  <i>пусто — добавь своё первое направление</i>")
    for st in streams.values():
        onoff = "✅" if st.get("enabled") else "⛔"
        trc = st.get("trc20") or "—"
        trc_short = trc if len(trc) < 20 else trc[:6] + "…" + trc[-4:]
        lines.append(f"  {onoff} <b>{st.get('name')}</b> → <code>{trc_short}</code>")
    rows = []
    for st in streams.values():
        rows.append([
            InlineKeyboardButton(
                text=f"⚙️ {st.get('name')}",
                callback_data=f"stream:menu:{st.get('name')}",
            ),
        ])
    if can_add:
        rows.append([InlineKeyboardButton(text="➕ Добавить направление", callback_data="stream:add")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.reply("\n".join(lines), reply_markup=kb)
    await cb.answer()


# Универсальный обработчик закрытия сообщений
@router.callback_query(F.data == "ui:close")
async def cb_ui_close(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "stream:add")
async def cb_stream_add(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not (cb.from_user.id == entry.get("partner_tg_id")
            or storage.is_owner(cb.from_user.id)
            or _check_partner_or_perm(entry, cb.from_user.id, "add_directions")):
        return await cb.answer("Только партнёр или его работник с правом.", show_alert=True)
    await state.set_state(Setup.wait_stream_name)
    await state.update_data(chat_id=cb.message.chat.id)
    prompt = await cb.message.reply(
        "Название направления (напр. Мороженое, Лопаты, Магазин1):"
    )
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_stream_name)
async def st_stream_name(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    name = (message.text or "").strip()
    if not name or len(name) > 32:
        err = await message.reply("Плохое имя (до 32 симв).")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    entry = storage.get_client_chat(data["chat_id"])
    if entry and name in (entry.get("streams") or {}):
        err = await message.reply(f"Направление <b>{name}</b> уже есть. Пришли другое.")
        await _track_msg(state, err)
        return
    await state.update_data(stream_name=name)
    await state.set_state(Setup.wait_stream_trc20)
    prompt = await message.reply(
        f"Направление: <b>{name}</b>\n"
        f"Пришли TRC20 адрес для выплат по этому направлению (начинается с T):"
    )
    await _track_msg(state, prompt)


@router.message(Setup.wait_stream_trc20)
async def st_stream_trc20(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    trc = (message.text or "").strip()
    if not (trc.startswith("T") and 30 <= len(trc) <= 40):
        err = await message.reply("Не похоже на TRC20 (начинается с T, длина ~34).")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    await storage.add_stream(data["chat_id"], data["stream_name"], trc, enabled=True)
    await _cleanup_fsm(bot, message.chat.id, state)
    await state.clear()
    final = await bot.send_message(
        message.chat.id,
        f"✅ Направление <b>{data['stream_name']}</b> добавлено.\n"
        f"TRC20: <code>{trc}</code>"
    )
    asyncio.create_task(_delete_later(bot, message.chat.id, final.message_id, 15))


@router.callback_query(F.data.startswith("stream:menu:"))
async def cb_stream_menu(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    name = cb.data.split(":", 2)[2]
    st = (entry.get("streams") or {}).get(name)
    if not st:
        return await cb.answer("Не найдено.", show_alert=True)
    onoff = "✅ ВКЛ" if st.get("enabled") else "⛔ ВЫКЛ"
    trc = st.get("trc20") or "—"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{onoff} · переключить", callback_data=f"stream:tgl:{name}")],
        [InlineKeyboardButton(text="💳 Изменить TRC20", callback_data=f"stream:trc:{name}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"stream:del:{name}")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])
    await cb.message.reply(
        f"📍 <b>{name}</b>\nTRC20: <code>{trc}</code>", reply_markup=kb
    )
    await cb.answer()


@router.callback_query(F.data.startswith("stream:tgl:"))
async def cb_stream_tgl(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or not (cb.from_user.id == entry.get("partner_tg_id")
                         or storage.is_owner(cb.from_user.id)
                         or _check_partner_or_perm(entry, cb.from_user.id, "add_directions")):
        return await cb.answer("Нет прав.", show_alert=True)
    name = cb.data.split(":", 2)[2]
    new = await storage.toggle_stream(cb.message.chat.id, name)
    await cb.answer(f"{name}: {'ВКЛ' if new else 'ВЫКЛ'}")


@router.callback_query(F.data.startswith("stream:trc:"))
async def cb_stream_trc(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or not (cb.from_user.id == entry.get("partner_tg_id")
                         or storage.is_owner(cb.from_user.id)
                         or _check_partner_or_perm(entry, cb.from_user.id, "set_wallet")):
        return await cb.answer("Нет прав.", show_alert=True)
    name = cb.data.split(":", 2)[2]
    await state.set_state(Setup.wait_stream_trc20_edit)
    await state.update_data(chat_id=cb.message.chat.id, stream_name=name)
    prompt = await cb.message.reply(f"Новый TRC20 для <b>{name}</b>:")
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_stream_trc20_edit)
async def st_stream_trc_edit(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    trc = (message.text or "").strip()
    if not (trc.startswith("T") and 30 <= len(trc) <= 40):
        err = await message.reply("Не похоже на TRC20.")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    await storage.update_stream_trc20(data["chat_id"], data["stream_name"], trc)
    await _cleanup_fsm(bot, message.chat.id, state)
    await state.clear()
    final = await bot.send_message(
        message.chat.id,
        f"✅ TRC20 для <b>{data['stream_name']}</b>: <code>{trc}</code>"
    )
    asyncio.create_task(_delete_later(bot, message.chat.id, final.message_id, 15))


@router.callback_query(F.data.startswith("stream:del:"))
async def cb_stream_del(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or not (cb.from_user.id == entry.get("partner_tg_id")
                         or storage.is_owner(cb.from_user.id)):
        return await cb.answer("Только партнёр.", show_alert=True)
    name = cb.data.split(":", 2)[2]
    ok = await storage.delete_stream(cb.message.chat.id, name)
    await cb.answer("Удалено" if ok else "Не найдено")
    if ok:
        try:
            await cb.message.edit_text(f"🗑 Направление <b>{name}</b> удалено.")
        except Exception:
            pass


# ============================================================
# CALLBACKS
# ============================================================
def _check_partner_or_perm(entry: dict, user_id: int, perm: str) -> bool:
    if storage.is_owner(user_id):
        return True
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
    await _show_client_setup(cb.message, entry, user_id=cb.from_user.id)


@router.callback_query(F.data == "setup:add_dir")
async def cb_setup_add_dir(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    # Только owner может добавлять направления
    if not storage.is_owner(cb.from_user.id):
        return await cb.answer(
            "Направления добавляет админ. Обратись в админ-чат PRIDE.",
            show_alert=True,
        )
    await state.set_state(Setup.wait_direction_name)
    await state.update_data(chat_id=cb.message.chat.id)
    prompt = await cb.message.reply("Название направления (напр. О1, ДАЧА):")
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_direction_name)
async def st_dir_name(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)  # трекаем ответ юзера
    name = (message.text or "").strip()
    if not name or len(name) > 32:
        err = await message.reply("Плохое имя. Пришли ещё раз (до 32 симв).")
        await _track_msg(state, err)
        return
    await state.update_data(direction_name=name)
    await state.set_state(Setup.wait_direction_pct)
    prompt = await message.reply("Процент комиссии (число, напр. 20):")
    await _track_msg(state, prompt)


@router.message(Setup.wait_direction_pct)
async def st_dir_pct(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    try:
        pct = float((message.text or "").replace(",", "."))
    except ValueError:
        err = await message.reply("Число.")
        await _track_msg(state, err)
        return
    if pct < 0 or pct > 100:
        err = await message.reply("Процент от 0 до 100.")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    await storage.set_direction(data["chat_id"], data["direction_name"], pct, enabled=True)
    await _cleanup_fsm(bot, message.chat.id, state)
    await state.clear()
    final = await bot.send_message(
        message.chat.id,
        f"✅ Направление <b>{data['direction_name']}</b> — {pct:g}% добавлено."
    )
    asyncio.create_task(_delete_later(bot, message.chat.id, final.message_id, 10))


@router.callback_query(F.data == "setup:set_wallet")
async def cb_setup_wallet(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "set_wallet"):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.set_state(Setup.wait_wallet)
    await state.update_data(chat_id=cb.message.chat.id)
    prompt = await cb.message.reply("Пришли TRC20 адрес (начинается с T…):")
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_wallet)
async def st_wallet(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    w = (message.text or "").strip()
    if not (w.startswith("T") and 30 <= len(w) <= 40):
        err = await message.reply("Не похоже на TRC20. Пришли валидный.")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    await storage.update_client_chat(data["chat_id"], wallet_trc20=w)
    await _cleanup_fsm(bot, message.chat.id, state)
    await state.clear()
    final = await bot.send_message(message.chat.id, f"✅ Кошелёк установлен: <code>{w}</code>")
    asyncio.create_task(_delete_later(bot, message.chat.id, final.message_id, 15))


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
    # используем available (за вычетом уже висящих заявок)
    available_by_stream = s.get("available_by_stream") or {}
    active = {k: v for k, v in available_by_stream.items() if v > 0.01}
    if not active:
        pending = s.get("pending_usd") or 0
        if pending > 0:
            return await cb.answer(
                f"Все остатки уже в очереди на выплату ({_fmt_money_usd(pending)}$). Ждём админа.",
                show_alert=True,
            )
        return await cb.answer("Остаток к выплате = 0.", show_alert=True)
    # Если несколько направлений с остатком — выбор
    if len(active) > 1:
        total = sum(active.values())
        rows = [[InlineKeyboardButton(
            text=f"💥 ВСЕ направления · {_fmt_money_usd(total)}$",
            callback_data="pay:all",
        )]]
        for st, amt in sorted(active.items(), key=lambda x: x[1], reverse=True):
            rows.append([InlineKeyboardButton(
                text=f"📍 {st} · {_fmt_money_usd(amt)}$",
                callback_data=f"pay:stream:{st}"
            )])
        rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await cb.message.reply("Выбери направление для выплаты:", reply_markup=kb)
        return await cb.answer()
    # Одно направление с остатком
    stream_name, remaining = next(iter(active.items()))
    return await _show_payout_confirm(cb, entry, stream_name, remaining, state, bot)


async def _show_payout_confirm(cb, entry, stream_name, remaining, state, bot):
    streams = entry.get("streams") or {}
    st = streams.get(stream_name)
    wallet = (st or {}).get("trc20") or entry.get("wallet_trc20") or ""
    if not wallet:
        await cb.message.reply(
            f"⚠️ У направления <b>{stream_name}</b> не указан TRC20 адрес.\n"
            f"Открой /профиль → 📍 Мои направления → выбери <b>{stream_name}</b> → 💳 Изменить TRC20"
        )
        return await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Запросить {_fmt_money_usd(remaining)}$",
            callback_data=f"pay:confirm:{stream_name}:{int(remaining*100)}"
        )],
        [InlineKeyboardButton(
            text="✏️ Изменить адрес",
            callback_data=f"stream:trc:{stream_name}",
        )],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])
    await cb.message.reply(
        f"💸 <b>Заявка на выплату</b>\n"
        f"📍 Направление: <b>{stream_name}</b>\n"
        f"💰 Сумма: <b>{_fmt_money_usd(remaining)}$</b>\n"
        f"💳 Адрес: <code>{wallet}</code>",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "pay:all")
async def cb_pay_all(cb: CallbackQuery, bot: Bot):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "request_payout"):
        return await cb.answer("Нет прав.", show_alert=True)
    s = storage.compute_stats(entry["chat_id"])
    available_by_stream = s.get("available_by_stream") or {}
    active = {k: v for k, v in available_by_stream.items() if v > 0.01}
    if not active:
        pending = s.get("pending_usd") or 0
        if pending > 0:
            return await cb.answer(
                f"Всё уже в очереди ({_fmt_money_usd(pending)}$).", show_alert=True
            )
        return await cb.answer("Нет остатков.", show_alert=True)
    streams = entry.get("streams") or {}
    # Case-insensitive lookup: строим карту lower→canonical
    stream_lookup = {k.lower(): k for k in streams.keys()}
    created = 0
    skipped_no_wallet = []
    errors = []
    for stream_name, amount in active.items():
        canonical = stream_lookup.get(stream_name.lower()) or stream_name
        st = streams.get(canonical) or {}
        wallet = st.get("trc20") or ""
        if not wallet:
            skipped_no_wallet.append(stream_name)
            continue
        try:
            await _create_and_send_payout(
                bot, cb.message.chat.id, amount, wallet,
                cb.from_user.id, cb.from_user.username or "",
                stream=canonical,
            )
            created += 1
        except Exception as e:
            logger.exception("[calc] pay:all error for %s: %s", stream_name, e)
            errors.append(f"{stream_name}: {e}")
    lines = [f"✅ Создано заявок: <b>{created}</b>"]
    if skipped_no_wallet:
        lines.append(
            f"⚠️ Не отправлено (нет TRC20): {', '.join(skipped_no_wallet)}"
        )
    if errors:
        lines.append("❌ Ошибки:\n" + "\n".join(errors[:5]))
    await cb.message.reply("\n".join(lines), reply_markup=_close_kb())
    await cb.answer(f"Создано: {created} заявок")
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay:stream:"))
async def cb_pay_stream(cb: CallbackQuery, state: FSMContext, bot: Bot):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "request_payout"):
        return await cb.answer("Нет прав.", show_alert=True)
    stream_name = cb.data.split(":", 2)[2]
    s = storage.compute_stats(entry["chat_id"])
    available = (s.get("available_by_stream") or {}).get(stream_name, 0)
    if available <= 0:
        pending = (s.get("pending_by_stream") or {}).get(stream_name, 0)
        if pending > 0:
            return await cb.answer(
                f"Остаток по <b>{stream_name}</b> уже в очереди ({_fmt_money_usd(pending)}$).",
                show_alert=True,
            )
        return await cb.answer("Остаток по этому направлению = 0.", show_alert=True)
    return await _show_payout_confirm(cb, entry, stream_name, available, state, bot)


@router.callback_query(F.data == "pay:edit_wallet")
async def cb_pay_edit_wallet(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "set_wallet"):
        return await cb.answer("Нет прав менять адрес.", show_alert=True)
    await state.set_state(Setup.wait_payout_wallet_edit)
    await state.update_data(chat_id=cb.message.chat.id, amount=None)
    prompt = await cb.message.reply("Пришли новый TRC20 адрес:")
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_payout_wallet_edit)
async def st_pay_wallet(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    w = (message.text or "").strip()
    if not (w.startswith("T") and 30 <= len(w) <= 40):
        err = await message.reply("Не похоже на TRC20.")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    chat_id = data["chat_id"]
    amount = data.get("amount")
    await storage.update_client_chat(chat_id, wallet_trc20=w)
    await _cleanup_fsm(bot, chat_id, state)
    await state.clear()
    if amount:
        await _create_and_send_payout(
            bot, chat_id, amount, w,
            message.from_user.id, message.from_user.username or "",
        )
    else:
        final = await bot.send_message(chat_id, f"✅ Адрес обновлён: <code>{w}</code>")
        asyncio.create_task(_delete_later(bot, chat_id, final.message_id, 15))


@router.callback_query(F.data.startswith("pay:confirm:"))
async def cb_pay_confirm(cb: CallbackQuery, bot: Bot):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if not _check_partner_or_perm(entry, cb.from_user.id, "request_payout"):
        return await cb.answer("Нет прав.", show_alert=True)
    parts = cb.data.split(":")
    # pay:confirm:<stream>:<amount*100>
    stream_name = parts[2] if len(parts) >= 4 else ""
    amount = int(parts[-1]) / 100.0
    streams = entry.get("streams") or {}
    wallet = ""
    if stream_name and stream_name in streams:
        wallet = streams[stream_name].get("trc20") or ""
    if not wallet:
        wallet = entry.get("wallet_trc20") or ""
    if not wallet:
        return await cb.answer("Нет кошелька для этого направления.", show_alert=True)
    # Финальная сверка: не превысить available (защита от race/повторного клика)
    s = storage.compute_stats(cb.message.chat.id)
    available = (s.get("available_by_stream") or {}).get(stream_name, 0)
    if amount > available + 0.01:
        try:
            await cb.message.delete()
        except Exception:
            pass
        return await cb.answer(
            f"⚠️ Уже в очереди / оплачено. Доступно к запросу: {_fmt_money_usd(max(available, 0))}$",
            show_alert=True,
        )
    await _create_and_send_payout(
        bot, cb.message.chat.id, amount, wallet,
        cb.from_user.id, cb.from_user.username or "",
        stream=stream_name,
    )
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Заявка создана.")


async def _create_and_send_payout(
    bot: Bot, chat_id: int, amount: float, wallet: str,
    user_id: int, username: str, stream: str = "",
):
    entry = storage.get_client_chat(chat_id)
    req = await storage.create_payout_request(
        chat_id=chat_id, amount_usd=amount, wallet=wallet,
        requested_by_id=user_id, requested_by_name=username, stream=stream,
    )
    # Клиенту
    client_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"pay:cancel:{req['id']}")],
    ])
    stream_line = f"📍 Направление: <b>{stream}</b>\n" if stream else ""
    client_msg = await bot.send_message(
        chat_id,
        f"💸 <b>Заявка на выплату #{req['id']}</b>\n"
        f"{stream_line}"
        f"Сумма: <b>{_fmt_money_usd(amount)}$</b>\n"
        f"Адрес: <code>{wallet}</code>\n"
        f"Статус: ⏳ ожидает подтверждения",
        reply_markup=client_kb,
    )
    # Ссылка на клиентское сообщение
    def _msg_link(cid: int, mid: int) -> str:
        s = str(cid)
        if s.startswith("-100"):
            s = s[4:]
        elif s.startswith("-"):
            s = s[1:]
        return f"https://t.me/c/{s}/{mid}"
    client_link = _msg_link(chat_id, client_msg.message_id)
    team = entry.get("team_name") or ""
    team_line = f"🦁 Команда: <b>{html.escape(team)}</b>\n" if team else ""

    admin_id = storage.get_admin_chat_id()
    logger.info(
        "[calc] payout: admin_id=%s chat=%s req_id=%s",
        admin_id, chat_id, req["id"],
    )
    if not admin_id:
        warn = await bot.send_message(
            chat_id,
            "⚠️ Админ-чат не настроен. Заявка сохранена, но админ не получил уведомление."
        )
        asyncio.create_task(_delete_later(bot, chat_id, warn.message_id, 30))
        return
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖐 Взял", callback_data=f"pay:take:{req['id']}")],
        [InlineKeyboardButton(text="💰 Выплата произведена", callback_data=f"pay:done:{req['id']}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay:reject:{req['id']}")],
        [InlineKeyboardButton(text="🔗 К сообщению клиента", url=client_link)],
    ])
    try:
        admin_msg = await bot.send_message(
            admin_id,
            f"🔔 <b>Новая заявка на выплату #{req['id']}</b>\n"
            f"{team_line}"
            f"💬 Чат: {html.escape(entry.get('chat_title') or '')} "
            f"(<code>{chat_id}</code>)\n"
            f"👤 Партнёр: @{entry.get('partner_username') or '—'}\n"
            f"{stream_line}"
            f"✍️ Запросил: @{username or '—'} (<code>{user_id}</code>)\n"
            f"💰 Сумма: <b>{_fmt_money_usd(amount)}$</b>\n"
            f"💳 Адрес: <code>{wallet}</code>",
            reply_markup=admin_kb,
        )
        await storage.update_payout_request(
            req["id"],
            client_msg_id=client_msg.message_id,
            admin_msg_id=admin_msg.message_id,
        )
    except Exception as e:
        logger.exception("[calc] failed to send payout to admin_chat %s: %s", admin_id, e)
        warn = await bot.send_message(
            chat_id,
            f"⚠️ Не смог отправить заявку в админ-чат: <code>{e}</code>"
        )
        asyncio.create_task(_delete_later(bot, chat_id, warn.message_id, 60))


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


@router.callback_query(F.data.startswith("pay:take:"))
async def cb_pay_take(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    pid = int(cb.data.split(":")[2])
    req = storage.get_payout_request(pid)
    if not req or req.get("status") != "pending":
        return await cb.answer("Уже обработана.", show_alert=True)
    await storage.update_payout_request(
        pid, status="taken",
        taken_by_id=cb.from_user.id,
        taken_by_name=cb.from_user.username or "",
    )
    try:
        current = cb.message.html_text or cb.message.text or ""
        await cb.message.edit_text(
            f"🖐 <b>ВЗЯЛ:</b> @{cb.from_user.username or cb.from_user.id}\n\n{current}",
            reply_markup=cb.message.reply_markup,
        )
    except Exception:
        pass
    try:
        await bot.edit_message_text(
            f"💸 <b>Заявка #{pid}</b>\n"
            f"Сумма: <b>{_fmt_money_usd(req['amount_usd'])}$</b>\n"
            f"Адрес: <code>{req['wallet']}</code>\n"
            f"Статус: 🖐 В работе — @{cb.from_user.username or 'admin'}",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    await cb.answer("Взял в работу.")


@router.callback_query(F.data.startswith("pay:reject:"))
async def cb_pay_reject(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    pid = int(cb.data.split(":")[2])
    req = storage.get_payout_request(pid)
    if not req or req.get("status") not in ("pending", "taken"):
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
    if not req or req.get("status") not in ("pending", "taken"):
        return await cb.answer("Уже обработана.", show_alert=True)
    req_amount = req.get("amount_usd") or 0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Полная — {_fmt_money_usd(req_amount)}$",
            callback_data=f"pay:donefull:{pid}",
        )],
        [InlineKeyboardButton(text="✏️ Частичная — ввести сумму", callback_data=f"pay:donepart:{pid}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="ui:close")],
    ])
    await cb.message.reply(
        f"Выплата по заявке #{pid} (запрошено {_fmt_money_usd(req_amount)}$):",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("pay:donefull:"))
async def cb_pay_done_full(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    pid = int(cb.data.split(":")[2])
    req = storage.get_payout_request(pid)
    if not req or req.get("status") not in ("pending", "taken"):
        return await cb.answer("Уже обработана.", show_alert=True)
    amt = float(req.get("amount_usd") or 0)
    await storage.record_payout(
        chat_id=req["chat_id"], amount_usd=amt,
        note=f"payout_req_id={pid}", admin_id=cb.from_user.id,
        stream=req.get("stream") or "",
    )
    await storage.update_payout_request(pid, status="paid", paid_amount_usd=amt)
    try:
        await cb.message.delete()
    except Exception:
        pass
    # Обновляем сообщения
    try:
        await bot.edit_message_text(
            f"✅ Заявка #{pid} — оплачено <b>{_fmt_money_usd(amt)}$</b> (полная)",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    if req.get("admin_msg_id"):
        try:
            await bot.edit_message_text(
                f"✅ Заявка #{pid} — оплачено {_fmt_money_usd(amt)}$ (полная)\n"
                f"Оплатил: @{cb.from_user.username or cb.from_user.id}",
                chat_id=storage.get_admin_chat_id(),
                message_id=req["admin_msg_id"],
            )
        except Exception:
            pass
    await cb.answer(f"✅ Оплачено {_fmt_money_usd(amt)}$")


@router.callback_query(F.data.startswith("pay:donepart:"))
async def cb_pay_done_part(cb: CallbackQuery, state: FSMContext):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    pid = int(cb.data.split(":")[2])
    req = storage.get_payout_request(pid)
    if not req or req.get("status") not in ("pending", "taken"):
        return await cb.answer("Уже обработана.", show_alert=True)
    await state.set_state(Setup.wait_payout_amount)
    await state.update_data(payout_id=pid)
    try:
        await cb.message.delete()
    except Exception:
        pass
    prompt = await cb.message.answer(
        f"Введи фактическую сумму в $ по заявке #{pid} "
        f"(запрошено {_fmt_money_usd(req['amount_usd'])}$):"
    )
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_payout_amount)
async def st_payout_amount(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    try:
        amt = float((message.text or "").replace(",", "."))
    except ValueError:
        err = await message.reply("Число.")
        await _track_msg(state, err)
        return
    if amt <= 0:
        err = await message.reply("Больше нуля.")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    pid = data["payout_id"]
    req = storage.get_payout_request(pid)
    if not req:
        await _cleanup_fsm(bot, message.chat.id, state)
        await state.clear()
        return await message.reply("Заявка пропала.")
    await storage.record_payout(
        chat_id=req["chat_id"], amount_usd=amt,
        note=f"payout_req_id={pid}", admin_id=message.from_user.id,
        stream=req.get("stream") or "",
    )
    await storage.update_payout_request(pid, status="paid", paid_amount_usd=amt)
    await _cleanup_fsm(bot, message.chat.id, state)
    await state.clear()
    final = await bot.send_message(
        message.chat.id, f"✅ Записано: <b>{_fmt_money_usd(amt)}$</b> клиенту."
    )
    asyncio.create_task(_delete_later(bot, message.chat.id, final.message_id, 15))
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
    prompt = await cb.message.reply(f"Направление: <b>{direction}</b>\nПришли примечание (текстом):")
    await _track_msg(state, prompt)
    # Также трекаем то сообщение с кнопками выбора направления
    await _track_msg(state, cb.message)
    await cb.answer()


@router.message(Setup.wait_requisite_note)
async def st_req_note(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    note = (message.text or "").strip()
    if not note or len(note) > 500:
        err = await message.reply("Примечание 1-500 символов.")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    await _cleanup_fsm(bot, message.chat.id, state)
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
    # Ссылка на клиентское сообщение (t.me/c/<chat_id>/<msg_id>)
    def _msg_link(chat_id: int, msg_id: int) -> str:
        cid = str(chat_id)
        if cid.startswith("-100"):
            cid = cid[4:]
        elif cid.startswith("-"):
            cid = cid[1:]
        return f"https://t.me/c/{cid}/{msg_id}"

    client_link = _msg_link(data["chat_id"], client_msg.message_id)
    team = entry.get("team_name") or ""
    team_line = f"🦁 Команда: <b>{html.escape(team)}</b>\n" if team else ""

    # Админам
    admin_id = storage.get_admin_chat_id()
    logger.info(
        "[calc] req_note: admin_id=%s chat=%s req_id=%s",
        admin_id, data["chat_id"], req["id"],
    )
    if not admin_id:
        # предупреждаем клиента
        warn = await message.reply(
            "⚠️ Админ-чат не настроен. Заявка сохранена, но никто не получил уведомление."
        )
        asyncio.create_task(_delete_later(bot, message.chat.id, warn.message_id, 30))
        return
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖐 Взял", callback_data=f"req:take:{req['id']}")],
        [InlineKeyboardButton(text="✅ Отправлено", callback_data=f"req:done:{req['id']}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req:reject:{req['id']}")],
        [InlineKeyboardButton(text="🔗 К сообщению клиента", url=client_link)],
    ])
    try:
        admin_msg = await bot.send_message(
            admin_id,
            f"🔔 <b>Заявка на реквизит #{req['id']}</b>\n"
            f"{team_line}"
            f"💬 Чат: {html.escape(entry.get('chat_title') or '')} "
            f"(<code>{data['chat_id']}</code>)\n"
            f"👤 Партнёр: @{entry.get('partner_username') or '—'}\n"
            f"✍️ Запросил: @{message.from_user.username or '—'}\n"
            f"📥 Направление приёма: <b>{data['direction']}</b>\n"
            f"📝 Примечание: {html.escape(note)}",
            reply_markup=admin_kb,
        )
        await storage.update_requisite_request(
            req["id"],
            client_msg_id=client_msg.message_id,
            admin_msg_id=admin_msg.message_id,
        )
    except Exception as e:
        logger.exception("[calc] failed to send req to admin_chat %s: %s", admin_id, e)
        warn = await message.reply(
            f"⚠️ Не смог отправить заявку в админ-чат: <code>{e}</code>\n"
            f"Проверь что бот в чате <code>{admin_id}</code> и имеет права писать."
        )
        asyncio.create_task(_delete_later(bot, message.chat.id, warn.message_id, 60))


@router.callback_query(F.data.startswith("req:take:"))
async def cb_req_take(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    rid = int(cb.data.split(":")[2])
    req = storage.get_requisite_request(rid)
    if not req:
        return await cb.answer("Не найдено.", show_alert=True)
    if req.get("status") != "pending":
        return await cb.answer("Уже обработана.", show_alert=True)
    await storage.update_requisite_request(
        rid, status="taken",
        taken_by_id=cb.from_user.id,
        taken_by_name=cb.from_user.username or "",
    )
    # Обновляем сообщение админов
    try:
        current = cb.message.html_text or cb.message.text or ""
        await cb.message.edit_text(
            f"🖐 <b>ВЗЯЛ:</b> @{cb.from_user.username or cb.from_user.id}\n\n{current}",
            reply_markup=cb.message.reply_markup,
        )
    except Exception:
        pass
    # Обновляем клиенту
    try:
        await bot.edit_message_text(
            f"📞 <b>Заявка #{rid}</b>\n"
            f"Направление: <b>{req.get('direction')}</b>\n"
            f"Примечание: {html.escape(req.get('note') or '')}\n"
            f"Статус: 🖐 В работе — @{cb.from_user.username or 'admin'}",
            chat_id=req["chat_id"], message_id=req["client_msg_id"],
        )
    except Exception:
        pass
    await cb.answer("Взял в работу.")


@router.callback_query(F.data.startswith("req:done:"))
async def cb_req_done(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != storage.get_admin_chat_id():
        return await cb.answer()
    rid = int(cb.data.split(":")[2])
    req = storage.get_requisite_request(rid)
    if not req or req.get("status") not in ("pending", "taken"):
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
    if not req or req.get("status") not in ("pending", "taken"):
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
    if cb.from_user.id != entry.get("partner_tg_id") and not storage.is_owner(cb.from_user.id):
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
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.reply("\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "wrk:add")
async def cb_wrk_add(cb: CallbackQuery, state: FSMContext):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry:
        return await cb.answer()
    if cb.from_user.id != entry.get("partner_tg_id") and not storage.is_owner(cb.from_user.id):
        return await cb.answer("Только партнёр.", show_alert=True)
    await state.set_state(Setup.wait_worker_username)
    await state.update_data(chat_id=cb.message.chat.id)
    prompt = await cb.message.reply(
        "Пришли <b>@username</b> или <b>tg_id</b> работника.\n"
        "<i>Узнать tg_id — @userinfobot</i>"
    )
    await _track_msg(state, prompt)
    await cb.answer()


@router.message(Setup.wait_worker_username)
async def st_wrk_uname(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    raw = (message.text or "").strip()
    tg_id = 0
    uname = ""
    # Вариант 1: чистый tg_id (цифры)
    if raw.lstrip("-").isdigit():
        tg_id = int(raw)
        # попробуем достать username из member трекера или get_chat_member
        entry = storage.get_client_chat(message.chat.id)
        mem = entry.get("members") or {} if entry else {}
        for m in mem.values():
            if int(m.get("tg_id") or 0) == tg_id:
                uname = m.get("username") or ""
                break
        if not uname:
            try:
                m = await bot.get_chat_member(message.chat.id, tg_id)
                uname = (m.user.username or "") if m and m.user else ""
            except Exception:
                pass
    else:
        # Вариант 2: username
        uname = raw.lstrip("@")
        if not re.match(r"^\w{3,32}$", uname):
            err = await message.reply("Плохой username или tg_id.")
            await _track_msg(state, err)
            return
        # Резолвим tg_id: member трекер → админы чата
        mem = storage.find_member(message.chat.id, uname)
        if mem:
            tg_id = int(mem.get("tg_id") or 0)
        if not tg_id:
            try:
                admins = await bot.get_chat_administrators(message.chat.id)
                for a in admins:
                    if (a.user.username or "").lower() == uname.lower():
                        tg_id = a.user.id
                        break
            except Exception:
                pass
    if not tg_id:
        await _cleanup_fsm(bot, message.chat.id, state)
        await state.clear()
        warn = await message.reply(
            f"⚠️ Не могу найти @{uname or raw} в этом чате.\n"
            f"Либо пусть напишет хоть раз сюда, либо пришли его <b>tg_id</b> "
            f"(узнать: @userinfobot)."
        )
        asyncio.create_task(_delete_later(bot, message.chat.id, warn.message_id, 30))
        return
    await state.update_data(worker_tg_id=tg_id, worker_username=uname)
    await state.set_state(Setup.wait_worker_role)
    prompt = await message.reply(f"@{uname} (id {tg_id}). Роль (напр. Помощник):")
    await _track_msg(state, prompt)


@router.message(Setup.wait_worker_role)
async def st_wrk_role(message: Message, state: FSMContext, bot: Bot):
    await _track_msg(state, message)
    role = (message.text or "").strip()[:64]
    if not role:
        err = await message.reply("Пусто.")
        await _track_msg(state, err)
        return
    data = await state.get_data()
    w = await storage.add_worker(
        chat_id=data["chat_id"],
        worker_tg_id=data["worker_tg_id"],
        username=data["worker_username"],
        role=role,
        perms={"set_wallet": False, "add_directions": False, "request_payout": False},
    )
    await _cleanup_fsm(bot, message.chat.id, state)
    await state.clear()
    final = await bot.send_message(
        message.chat.id,
        f"✅ Работник @{w['username']} · {w['role']} добавлен.\n"
        f"Разрешения выключены — открой /профиль → Работники → ⚙️"
    )
    asyncio.create_task(_delete_later(bot, message.chat.id, final.message_id, 20))


@router.callback_query(F.data.startswith("wrk:menu:"))
async def cb_wrk_menu(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or (cb.from_user.id != entry.get("partner_tg_id") and not storage.is_owner(cb.from_user.id)):
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
    if not entry or (cb.from_user.id != entry.get("partner_tg_id") and not storage.is_owner(cb.from_user.id)):
        return await cb.answer("Только партнёр.", show_alert=True)
    _, _, wid_s, perm = cb.data.split(":", 3)
    wid = int(wid_s)
    new_state = await storage.toggle_worker_perm(cb.message.chat.id, wid, perm)
    await cb.answer(f"{perm}: {'ВКЛ' if new_state else 'ВЫКЛ'}")


@router.callback_query(F.data.startswith("wrk:del:"))
async def cb_wrk_del(cb: CallbackQuery):
    entry = storage.get_client_chat(cb.message.chat.id)
    if not entry or (cb.from_user.id != entry.get("partner_tg_id") and not storage.is_owner(cb.from_user.id)):
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
@router.message(Command("whoami", "кто", "id"))
async def cmd_whoami(message: Message):
    """Диагностика — кто я, куда пишу, есть ли доступ."""
    is_owner = storage.is_owner(message.from_user.id)
    admin_chat = storage.get_admin_chat_id()
    is_admin_chat = message.chat.id == admin_chat if admin_chat else False
    entry = storage.get_client_chat(message.chat.id)
    lines = [
        "🔍 <b>WHOAMI</b>",
        f"👤 Ты: @{message.from_user.username or '—'}",
        f"🆔 Твой tg_id: <code>{message.from_user.id}</code>",
        f"💬 Этот чат: <code>{message.chat.id}</code>",
        f"📛 Тип чата: {message.chat.type}",
        "",
        f"🦁 Owner: <b>{'ДА' if is_owner else 'НЕТ'}</b>",
        f"🛡 Админ-чат установлен: <code>{admin_chat or '—'}</code>",
        f"🏢 Это админ-чат: <b>{'ДА' if is_admin_chat else 'НЕТ'}</b>",
        f"📊 Это клиентский чат: <b>{'ДА' if entry else 'НЕТ'}</b>",
    ]
    if entry:
        lines.append(f"   партнёр: @{entry.get('partner_username') or '—'}")
        lines.append(f"   partner_tg_id: <code>{entry.get('partner_tg_id') or '0'}</code>")
        lines.append(f"   курс: {entry.get('rate') or '—'}")
        lines.append(f"   направлений: {len(entry.get('directions') or {})}")
    if not is_owner:
        lines.append(
            "\n⚠️ Тебя нет в CALC_OWNER_IDS. Впиши свой tg_id в Railway → "
            "calc-bot → Variables → CALC_OWNER_IDS"
        )
    await message.reply("\n".join(lines), reply_markup=_close_kb())


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
    available_by_stream = s.get("available_by_stream") or {}
    active = {k: v for k, v in available_by_stream.items() if v > 0.01}
    if not active:
        pending = s.get("pending_usd") or 0
        if pending > 0:
            return await message.reply(
                f"Все остатки уже в очереди на выплату ({_fmt_money_usd(pending)}$)."
            )
        return await message.reply("Остаток к выплате = 0.")
    streams = entry.get("streams") or {}
    # Одно направление — сразу подтверждение
    if len(active) == 1:
        stream_name, remaining = next(iter(active.items()))
        wallet = (streams.get(stream_name) or {}).get("trc20") or ""
        if not wallet:
            return await message.reply(
                f"⚠️ У направления <b>{stream_name}</b> не указан TRC20.\n"
                f"Открой /профиль → 📍 Мои направления → <b>{stream_name}</b> → 💳"
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Запросить {_fmt_money_usd(remaining)}$",
                callback_data=f"pay:confirm:{stream_name}:{int(remaining*100)}"
            )],
            [InlineKeyboardButton(
                text="✏️ Изменить адрес",
                callback_data=f"stream:trc:{stream_name}",
            )],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
        ])
        return await message.reply(
            f"💸 <b>Заявка на выплату</b>\n"
            f"📍 <b>{stream_name}</b>\n"
            f"💰 <b>{_fmt_money_usd(remaining)}$</b>\n"
            f"💳 <code>{wallet}</code>",
            reply_markup=kb,
        )
    # Несколько направлений — выбор + "ВСЕ"
    total = sum(active.values())
    rows = [[InlineKeyboardButton(
        text=f"💥 ВСЕ направления · {_fmt_money_usd(total)}$",
        callback_data="pay:all",
    )]]
    for st, amt in sorted(active.items(), key=lambda x: x[1], reverse=True):
        rows.append([InlineKeyboardButton(
            text=f"📍 {st} · {_fmt_money_usd(amt)}$",
            callback_data=f"pay:stream:{st}"
        )])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    await message.reply(
        "Выбери направление для выплаты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
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


# ============================================================
# АЛИАСЫ БЕЗ СЛЕША — простые русские слова
# Регистрируется ДО catch_partner_id, но ПОСЛЕ всех Command-хендлеров.
# ============================================================
_TEXT_ALIASES = {
    "стата": cmd_stats,
    "статистика": cmd_stats,
    "статус": cmd_status,
    "выплата": cmd_payout,
    "профиль": cmd_profile,
    "настройка": cmd_setup,
    "настройки": cmd_setup,
    "чаты": cmd_list_chats,
    "кто": cmd_whoami,
    "whoami": cmd_whoami,
    # Админские рассылочные команды:
    "начатьдень": cmd_start_day,
    "начать": cmd_start_day,
    "деньначат": cmd_start_day,
}
# Подвязываем end_day алиасы после определения функции (она объявлена ниже в файле)
try:
    _TEXT_ALIASES["закончитьдень"] = cmd_end_day
    _TEXT_ALIASES["конецдня"] = cmd_end_day
except NameError:
    pass  # cmd_end_day определится позже — тогда подвяжется при следующей загрузке


@router.message(F.text)
async def _text_command_aliases(message: Message, state: FSMContext, bot: Bot):
    """Если пользователь пишет команду просто словом (без /), диспатчим."""
    # НЕ трогаем если пользователь в FSM-состоянии (там ждём ответ)
    cur_state = await state.get_state()
    if cur_state:
        return
    text = (message.text or "").strip().lower()
    # Разрешаем "стата" или "стата что-нибудь" — берём первое слово
    first_word = text.split()[0] if text else ""
    handler = _TEXT_ALIASES.get(first_word)
    if not handler:
        return
    # Вызываем оригинальный хендлер. Сигнатуры у них разные,
    # безопасно передавать все возможные kwargs — питон схавает.
    import inspect
    sig = inspect.signature(handler)
    kwargs = {}
    if "bot" in sig.parameters:
        kwargs["bot"] = bot
    if "state" in sig.parameters:
        kwargs["state"] = state
    await handler(message, **kwargs)


# ============================================================
# LAST — Подхват tg_id партнёра. Регистрируется в САМОМ КОНЦЕ
# чтобы Command-хендлеры срабатывали раньше. Фильтр: не команда, есть текст, есть username.
# ============================================================
@router.message(
    F.chat.type.in_({"group", "supergroup"})
    & F.text
    & ~F.text.startswith("/")
    & ~F.text.startswith("+")
)
async def _catch_partner_id(message: Message):
    if not message.from_user or not message.from_user.username:
        return
    entry = storage.get_client_chat(message.chat.id)
    if not entry:
        return
    # Запоминаем всех кто писал — понадобится для добавления работника
    await storage.remember_member(
        message.chat.id,
        message.from_user.id,
        message.from_user.username,
    )
    # Резолвим partner_tg_id если он ещё 0
    if not entry.get("partner_tg_id"):
        if (entry.get("partner_username") or "").lower() == message.from_user.username.lower():
            await storage.update_client_chat(
                message.chat.id, partner_tg_id=int(message.from_user.id)
            )
            logger.info("[calc] partner_tg_id resolved for %s: %s",
                        message.chat.id, message.from_user.id)

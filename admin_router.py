"""Admin panel: /admin command, inline-keyboard menu, FSM for inputs."""
import html
import logging
from aiogram import Router, F

import config
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from storage import storage

logger = logging.getLogger(__name__)
router = Router()


class AdminFSM(StatesGroup):
    add_worker = State()
    set_welcome = State()
    set_cooldown = State()
    add_admin = State()
    set_brain_chat = State()
    set_ai_model = State()
    set_idle_minutes = State()
    set_coord_chat = State()
    set_accounting_chat = State()
    set_lk_chat = State()
    set_role_name = State()       # FSM роли: ввод названия роли (username
                                  # передаётся в state.data из callback'а)
    set_invite_gif = State()      # ожидаем reply/forward с GIF (file_id)
    set_invite_emoji = State()    # формат "EMOJI document_id" или "EMOJI -"
    set_invite_jobs = State()     # текст раздела «Вакансии»
    broadcast_text = State()      # ввод текста рассылки (audience в state.data)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Работники", callback_data="adm:workers")],
        [InlineKeyboardButton(text="🎭 Роли работников", callback_data="adm:roles")],
        [InlineKeyboardButton(text="💬 Приветствие", callback_data="adm:welcome")],
        [InlineKeyboardButton(text="⏱ Кулдаун", callback_data="adm:cooldown")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📈 Источники трафика", callback_data="adm:traffic")],
        [InlineKeyboardButton(text="🧠 AI (Claude)", callback_data="adm:ai")],
        [InlineKeyboardButton(text="📨 Invite-бот (welcome)", callback_data="adm:invite")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="🔐 Админы", callback_data="adm:admins")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="adm:close")],
    ])


def _broadcast_audience_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Всем кто нажимал /start", callback_data="adm:bc:all")],
        [InlineKeyboardButton(text="💤 Только тем кто НЕ зашёл в work-чат", callback_data="adm:bc:inactive")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu")],
    ])


def _broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="adm:bc:send")],
        [InlineKeyboardButton(text="✏️ Переписать", callback_data="adm:bc:rewrite")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:bc:cancel")],
    ])


# ─── BROADCAST flow ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:broadcast")
async def cb_broadcast_menu(call: CallbackQuery, state: FSMContext):
    if not storage.is_admin(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    await state.clear()
    n_all = len(storage.list_bot_users() or {})
    n_inactive = len(storage.list_inactive_bot_users() or [])
    await call.message.edit_text(
        f"📢 <b>Рассылка через @PrideInviteWork_bot</b>\n\n"
        f"Выберите аудиторию:\n"
        f"• <b>Все:</b> {n_all} чел.\n"
        f"• <b>Не зашедшие в work-чат:</b> {n_inactive} чел.",
        reply_markup=_broadcast_audience_kb(),
    )
    await call.answer()


async def _broadcast_ask_text(call: CallbackQuery, state: FSMContext, audience: str):
    if not storage.is_admin(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    await state.set_state(AdminFSM.broadcast_text)
    await state.update_data(audience=audience)
    label = "ВСЕМ" if audience == "all" else "НЕ зашедшим в work-чат"
    count = (
        len(storage.list_bot_users() or {}) if audience == "all"
        else len(storage.list_inactive_bot_users() or [])
    )
    await call.message.edit_text(
        f"✍️ <b>Рассылка → {label}</b> ({count} чел.)\n\n"
        f"Пришлите текст следующим сообщением. Поддерживается HTML "
        f"(<b>жирный</b>, <i>курсив</i>, <a href='url'>ссылка</a>).\n\n"
        f"Для отмены — /admin",
    )
    await call.answer()


@router.callback_query(F.data == "adm:bc:all")
async def cb_bc_all(call: CallbackQuery, state: FSMContext):
    await _broadcast_ask_text(call, state, "all")


@router.callback_query(F.data == "adm:bc:inactive")
async def cb_bc_inactive(call: CallbackQuery, state: FSMContext):
    await _broadcast_ask_text(call, state, "inactive")


@router.message(AdminFSM.broadcast_text, F.text & ~F.text.startswith("/"))
async def handle_broadcast_text(message: Message, state: FSMContext):
    data = await state.get_data()
    audience = data.get("audience", "all")
    text = (message.text or "").strip()
    if not text:
        await message.reply("Пустой текст — отправьте ещё раз.")
        return
    await state.update_data(text=text)
    label = "ВСЕМ" if audience == "all" else "НЕ зашедшим в work-чат"
    count = (
        len(storage.list_bot_users() or {}) if audience == "all"
        else len(storage.list_inactive_bot_users() or [])
    )
    await message.reply(
        f"📋 <b>Предпросмотр рассылки</b>\n\n"
        f"<b>Аудитория:</b> {label} ({count} чел.)\n\n"
        f"────── текст ──────\n{text}\n────────────────\n\n"
        f"Отправить?",
        reply_markup=_broadcast_confirm_kb(),
    )


@router.callback_query(F.data == "adm:bc:rewrite")
async def cb_bc_rewrite(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    audience = data.get("audience", "all")
    await _broadcast_ask_text(call, state, audience)


@router.callback_query(F.data == "adm:bc:cancel")
async def cb_bc_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Рассылка отменена.", reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "adm:bc:send")
async def cb_bc_send(call: CallbackQuery, state: FSMContext):
    if not storage.is_admin(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    data = await state.get_data()
    audience = data.get("audience", "all")
    text = data.get("text", "")
    if not text:
        await call.answer("Нет текста для рассылки", show_alert=True)
        return

    # Собираем список user_id
    if audience == "all":
        users_dict = storage.list_bot_users() or {}
        user_ids = [int(uid) for uid in users_dict.keys()]
    else:
        user_ids = [u["user_id"] for u in (storage.list_inactive_bot_users() or [])]
    total = len(user_ids)
    if total == 0:
        await call.message.edit_text("⚠️ Аудитория пустая — некому отправлять.", reply_markup=main_menu_kb())
        await state.clear()
        await call.answer()
        return

    await call.message.edit_text(
        f"⏳ Отправляю {total} сообщений... подожди (~{total // 20 + 1} сек)",
    )
    await call.answer()

    import asyncio as _asyncio
    bot = call.message.bot
    sent, failed, blocked = 0, 0, 0
    # Throttle: Telegram limit ~30 msg/sec в общую группу но безопасно 20 msg/sec
    for i, uid in enumerate(user_ids):
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            es = str(e).lower()
            if "bot was blocked" in es or "user is deactivated" in es or "chat not found" in es:
                blocked += 1
            else:
                failed += 1
                logger.warning("broadcast to %s failed: %s", uid, e)
        # 50ms между сообщениями — ~20 msg/sec
        if i < total - 1:
            await _asyncio.sleep(0.05)

    await state.clear()
    summary = (
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"• Отправлено: <b>{sent}</b>\n"
        f"• Заблокировали бота / удалены: <b>{blocked}</b>\n"
        f"• Ошибки: <b>{failed}</b>\n"
        f"• Всего в аудитории: {total}"
    )
    try:
        await call.message.edit_text(summary, reply_markup=main_menu_kb())
    except Exception:
        await call.message.answer(summary, reply_markup=main_menu_kb())


@router.callback_query(F.data == "adm:menu")
async def cb_back_to_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
    await call.answer()


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        return  # silent ignore
    await state.clear()
    await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())


@router.message(Command("healthcheck"))
async def cmd_healthcheck(message: Message):
    """Прогоняет проверку всех систем и шлёт отчёт. Доступно только админам."""
    if not storage.is_admin(message.from_user.id):
        return  # silent ignore
    status_msg = await message.answer("⏳ Прогоняю проверку всех систем...")
    try:
        from health_check import HealthChecker
        h = HealthChecker()
        await h.run_all()
        text = h.format_telegram_message()
        try:
            await status_msg.edit_text(text, parse_mode="HTML",
                                       disable_web_page_preview=True)
        except Exception:
            # если сообщение получилось слишком длинным — шлём новым
            await message.answer(text, parse_mode="HTML",
                                 disable_web_page_preview=True)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Healthcheck сам упал: <code>{e}</code>\n\n"
            f"Это значит что-то очень серьёзное — посмотри логи Railway.",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("adm:"))
async def on_cb(call: CallbackQuery, state: FSMContext):
    if not storage.is_admin(call.from_user.id):
        await call.answer("Нет прав", show_alert=True)
        return

    action = call.data.split(":", 1)[1]
    await state.clear()

    if action == "close":
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer()
        return
    elif action == "main":
        await call.message.edit_text("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
    elif action == "workers":
        await render_workers(call)
    elif action == "roles":
        await render_roles(call)
    elif action.startswith("role_setname:"):
        # Клик «🎭 <role>» рядом с конкретным работником —
        # начинаем FSM ввода названия роли для него.
        uname = action.split(":", 1)[1]
        await state.update_data(role_username=uname)
        current = storage.get_worker_role(uname) or {}
        current_role = current.get("role") or ""
        note = f"\n\n<i>Текущая роль: {current_role}</i>" if current_role else ""
        await call.message.edit_text(
            f"🎭 <b>Роль для @{uname}</b>\n\n"
            "Пришлите название роли (до 16 символов).\n"
            "Примеры: <code>Менеджер</code>, <code>Оператор</code>, "
            f"<code>Бухгалтер</code>, <code>Откупщик</code>, <code>Тимон</code>.{note}\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_role_name)
    elif action.startswith("role_del:"):
        uname = action.split(":", 1)[1]
        await storage.remove_worker_role(uname)
        await call.answer(f"Роль @{uname} удалена.")
        await render_roles(call)
    elif action.startswith("role_toggle:"):
        uname = action.split(":", 1)[1]
        info = storage.get_worker_role(uname)
        new_is_admin = not bool(info.get("is_admin"))
        await storage.set_worker_role(
            uname, info.get("role") or "Сотрудник", new_is_admin,
        )
        await call.answer(
            "Админ: " + ("ДА" if new_is_admin else "НЕТ") + f" для @{uname}"
        )
        await render_roles(call)
    elif action == "welcome":
        await render_welcome(call)
    elif action == "cooldown":
        await render_cooldown(call)
    elif action == "stats":
        await render_stats(call)
    elif action == "traffic":
        await render_traffic(call)
    elif action == "ai":
        await render_ai(call)
    elif action == "ai_toggle":
        await storage.set_ai_enabled(not storage.is_ai_enabled())
        await call.answer("AI " + ("включён" if storage.is_ai_enabled() else "выключен"))
        await render_ai(call)
    elif action == "wb_toggle":
        await storage.set_writeback_enabled(not storage.is_writeback_enabled())
        await call.answer(
            "Writeback " + ("включён" if storage.is_writeback_enabled() else "выключен")
        )
        await render_ai(call)
    elif action == "ai_set_chat":
        await call.message.edit_text(
            "Пришлите ID чата для логов AI (число, например <code>-1001234567890</code> "
            "для супергрупп или <code>0</code> чтобы отключить).\n\n"
            "<i>Юзербот должен быть участником этого чата — он будет туда писать "
            "[AI-LOG] записи и читать заметки админа как доп. контекст.</i>\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_brain_chat)
    elif action == "ai_set_model":
        await call.message.edit_text(
            "Пришлите название модели Claude.\n\n"
            "Варианты:\n"
            "• <code>claude-sonnet-4-6</code> — баланс (рекомендуется)\n"
            "• <code>claude-opus-4-6</code> — самый умный, дороже в 5x\n"
            "• <code>claude-haiku-4-5-20251001</code> — быстрый и дешёвый\n\n"
            "Пустая строка — вернуть на дефолт из config.\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_ai_model)
    elif action == "ai_set_coord":
        await call.message.edit_text(
            "Пришлите ID координаторской беседы (число, например "
            "<code>-1001234567890</code> для супергрупп или <code>0</code> чтобы "
            "отключить эскалацию).\n\n"
            "<i>Юзербот должен быть участником этой беседы — туда он будет писать "
            "вызовы специалистам команды.</i>\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_coord_chat)
    elif action == "ai_set_accounting":
        await call.message.edit_text(
            "Пришлите ID чата «Бухгалтерия» (число или <code>0</code> чтобы отключить).\n\n"
            "<i>Юзербот должен быть участником этой беседы. Туда будут приходить "
            "ежедневные отчёты + менеджер вводит туда команды (приход/расход/курс/ЛК/etc).</i>\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_accounting_chat)
    elif action == "ai_set_lk":
        await call.message.edit_text(
            "Пришлите ID Группы 1 «Личные кабинеты» (число или <code>0</code>).\n\n"
            "<i>Юзербот должен быть участником. Туда бот будет постить и обновлять "
            "карточки ЛК (анкеты), а команды БРАК/БЛОК — отслеживать.</i>\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_lk_chat)
    elif action == "ai_set_idle":
        await call.message.edit_text(
            "Пришлите значение «тишины сотрудников» в минутах "
            "(целое число от 0 до 1440).\n\n"
            "<b>0</b> = AI отвечает всегда, даже если только что писал сотрудник "
            "(может пересекаться с живым общением!).\n\n"
            "<b>5</b> (по умолчанию) — если сотрудник писал в последние 5 минут, "
            "AI молчит. Безопасно: AI не вмешивается в живой диалог.\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_idle_minutes)
    elif action == "invite":
        await render_invite(call)
    elif action == "invite_setgif":
        await call.message.edit_text(
            "🎞 <b>GIF для welcome InviteWork-бота</b>\n\n"
            "Пришлите GIF/анимацию <b>одним сообщением</b> — я возьму её file_id "
            "и сохраню. Можно переслать GIF из любого чата.\n\n"
            "Чтобы убрать GIF — пришлите <code>-</code> (минус).\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_invite_gif)
    elif action == "invite_setemoji":
        cur = storage.get_invite_premium_emoji() or {}
        cur_str = "\n".join(f"  {k} → <code>{v}</code>" for k, v in cur.items()) or "<i>(пусто)</i>"
        await call.message.edit_text(
            "🎨 <b>Premium-эмодзи для welcome</b>\n\n"
            "Пришлите <b>одним сообщением</b> в формате:\n"
            "<code>EMOJI DOCUMENT_ID</code>\n\n"
            "Например:\n"
            "<code>🔥 5462863737368090301</code>\n\n"
            "Чтобы удалить — <code>EMOJI -</code> (минус вместо ID).\n\n"
            "<b>Как получить DOCUMENT_ID:</b>\n"
            "1. Premium-юзер шлёт боту нужный premium-эмодзи.\n"
            "2. В логах JSON ищем <code>custom_emoji_id</code>.\n"
            "3. Или используем @CustomEmojiInfoBot.\n\n"
            f"<b>Текущие маппинги:</b>\n{cur_str}\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_invite_emoji)
    elif action == "invite_setjobs":
        cur = storage.get_invite_jobs_text()
        safe = cur.replace("<", "&lt;").replace(">", "&gt;")
        await call.message.edit_text(
            "💼 <b>Текст раздела «Вакансии»</b>\n\n"
            "Текущий текст:\n\n<blockquote>" + safe + "</blockquote>\n\n"
            "Пришлите новый текст (HTML — поддерживается).\n\n"
            "Чтобы вернуть default — пришлите <code>-</code>.\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_invite_jobs)
    elif action == "invite_clearemoji":
        await storage.set_invite_premium_emoji({})
        await call.answer("Premium-эмодзи очищены.")
        await render_invite(call)
    elif action == "admins":
        await render_admins(call)
    elif action == "worker_add":
        await call.message.edit_text(
            "Пришлите @username работника одним сообщением.\n\n"
            "Или /admin чтобы отменить.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.add_worker)
    elif action == "welcome_edit":
        await call.message.edit_text(
            "Пришлите новый текст приветствия одним сообщением.\n\n"
            "💎 <b>Premium-эмодзи / форматирование:</b> перешлите/скопируйте сюда "
            "своё готовое сообщение из Telegram (со своего premium-аккаунта) — "
            "бот сохранит текст вместе с эмодзи и форматированием.\n\n"
            "📝 Лимит — 4096 символов на сообщение. Длинный текст без премиум-эмодзи "
            "будет автоматически разбит при отправке.\n\n"
            "Или /admin чтобы отменить.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_welcome)
    elif action == "cooldown_edit":
        await call.message.edit_text(
            "Пришлите кулдаун в минутах (0 — без кулдауна).\n\n"
            "Или /admin чтобы отменить.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_cooldown)
    elif action == "admin_add":
        await call.message.edit_text(
            "Пришлите Telegram ID нового админа.\n"
            "Чтобы узнать ID — попросите его написать @userinfobot.\n\n"
            "Или /admin чтобы отменить.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.add_admin)
    elif action.startswith("worker_del:"):
        u = action.split(":", 1)[1]
        await storage.remove_worker(u)
        await call.answer(f"Удалён @{u}")
        await render_workers(call)
    elif action.startswith("admin_del:"):
        try:
            uid = int(action.split(":", 1)[1])
            await storage.remove_admin(uid)
            await call.answer(f"Удалён {uid}")
        except ValueError:
            await call.answer("Ошибка", show_alert=True)
        await render_admins(call)

    await call.answer()


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")],
    ])


async def render_workers(call: CallbackQuery):
    workers = storage.get_workers()
    roles = storage.list_worker_roles()
    text = f"👥 <b>Работники ({len(workers)})</b>\n\n"
    if workers:
        lines = []
        for w in workers:
            info = roles.get(w.lower()) or {}
            role = info.get("role") or ""
            admin_mark = " 👑" if info.get("is_admin") else ""
            tag = f" — {role}{admin_mark}" if role or admin_mark else ""
            lines.append(f"• @{w}{tag}")
        text += "\n".join(lines)
    else:
        text += "<i>пусто</i>"
    rows = [[InlineKeyboardButton(text=f"🗑 @{w}", callback_data=f"adm:worker_del:{w}")] for w in workers]
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="adm:worker_add")])
    rows.append([InlineKeyboardButton(text="🎭 Роли", callback_data="adm:roles")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def render_roles(call: CallbackQuery):
    """Меню ролей: каждый worker — отдельная строка с кнопками
    [🎭 <role>] [👑/👤 admin] [🗑]. Один клик переключает админ-флаг
    мгновенно. Клик по роли — FSM на 1 шаг (ввод названия)."""
    workers = storage.get_workers()
    roles = storage.list_worker_roles()
    lines = [
        "🎭 <b>Роли работников</b>",
        "",
        "При создании новой рабочей беседы юзербот:",
        "1. Приглашает каждого работника",
        "2. Если 👑 — выдаёт админ-права с rank = роль",
        "",
        "<i>Чтобы задать роль — нажми кнопку 🎭 рядом с ником.</i>",
        "<i>Чтобы переключить админку — нажми 👑/👤.</i>",
        "",
    ]
    rows = []
    if not workers:
        lines.append("<i>Работников нет — добавь в 👥 Работники.</i>")
    else:
        for w in workers:
            info = roles.get(w.lower()) or {}
            role = info.get("role") or "—"
            is_admin = bool(info.get("is_admin"))
            admin_icon = "👑" if is_admin else "👤"
            # одна строка кнопок на работника
            rows.append([
                InlineKeyboardButton(
                    text=f"🎭 {role} @{w}",
                    callback_data=f"adm:role_setname:{w}",
                ),
                InlineKeyboardButton(
                    text=admin_icon,
                    callback_data=f"adm:role_toggle:{w}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"adm:role_del:{w}",
                ),
            ])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")])
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def render_welcome(call: CallbackQuery):
    welcome = storage.get_welcome()
    safe = welcome.replace("<", "&lt;").replace(">", "&gt;")
    text = f"💬 <b>Приветствие</b>\n\nТекущий текст:\n\n<blockquote>{safe}</blockquote>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="adm:welcome_edit")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")],
    ])
    await call.message.edit_text(text, reply_markup=kb)


async def render_invite(call: CallbackQuery):
    """Меню настроек InviteWork-бота: GIF + premium emoji + текст вакансий."""
    gif_id = storage.get_invite_welcome_gif() or ""
    emoji_map = storage.get_invite_premium_emoji() or {}
    jobs_text = storage.get_invite_jobs_text()
    gif_status = f"✅ задан (<code>{gif_id[:20]}...</code>)" if gif_id else "❌ не задан"
    emoji_lines = ""
    if emoji_map:
        emoji_lines = "\n".join(f"  {k} → <code>{v}</code>" for k, v in emoji_map.items())
    else:
        emoji_lines = "<i>(дефолтные эмодзи)</i>"
    jobs_safe = (jobs_text[:200].replace("<", "&lt;").replace(">", "&gt;"))
    body = (
        "📨 <b>InviteWork-бот — welcome</b>\n\n"
        f"🎞 <b>GIF:</b> {gif_status}\n\n"
        f"🎨 <b>Premium-эмодзи:</b>\n{emoji_lines}\n\n"
        f"💼 <b>Вакансии (preview):</b>\n<blockquote>{jobs_safe}...</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎞 Задать GIF",      callback_data="adm:invite_setgif")],
        [InlineKeyboardButton(text="🎨 Premium-эмодзи", callback_data="adm:invite_setemoji")],
        [InlineKeyboardButton(text="🧹 Очистить эмодзи", callback_data="adm:invite_clearemoji")],
        [InlineKeyboardButton(text="💼 Текст вакансий", callback_data="adm:invite_setjobs")],
        [InlineKeyboardButton(text="🔙 Назад",          callback_data="adm:main")],
    ])
    await call.message.edit_text(body, reply_markup=kb, disable_web_page_preview=True)


async def render_cooldown(call: CallbackQuery):
    cd = storage.get_cooldown_minutes()
    text = f"⏱ <b>Кулдаун</b>\n\nТекущий: <b>{cd}</b> мин"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="adm:cooldown_edit")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")],
    ])
    await call.message.edit_text(text, reply_markup=kb)


async def render_stats(call: CallbackQuery):
    stats = storage.get_stats()
    total = stats.get("total_chats_created", 0)
    by_user = stats.get("creations_by_user", {})
    top = sorted(by_user.items(), key=lambda x: -x[1])[:10]
    text = f"📊 <b>Статистика</b>\n\nВсего бесед создано: <b>{total}</b>\n\n"
    text += "<b>Топ клиентов:</b>\n"
    text += "\n".join(f"• <code>{uid}</code> — {cnt}" for uid, cnt in top) if top else "<i>пусто</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")],
    ])
    await call.message.edit_text(text, reply_markup=kb)


async def render_ai(call: CallbackQuery):
    """🧠 AI panel: статус, модель, brain_chat, статистика."""
    enabled = storage.is_ai_enabled()
    model = storage.get_ai_model() or config.DEFAULT_AI_MODEL
    brain_id = storage.get_brain_chat_id()
    idle_min = storage.get_client_idle_minutes()
    stats = storage.get_ai_stats()
    api_key_set = bool(config.ANTHROPIC_API_KEY)

    status_emoji = "🟢" if enabled else "🔴"
    status_text = "ВКЛ" if enabled else "ВЫКЛ"
    api_warn = (
        "" if api_key_set
        else "\n\n⚠️ <b>ANTHROPIC_API_KEY не задан в env</b> — AI не сможет отвечать."
    )

    # Writeback (auto-commit в knowledge/)
    wb_enabled = storage.is_writeback_enabled()
    wb_stats = storage.get_writeback_stats()
    wb_emoji = "🟢" if wb_enabled else "🔴"
    wb_status = "ВКЛ" if wb_enabled else "ВЫКЛ"
    has_gh_token = bool(config.GITHUB_TOKEN)
    wb_warn = (
        "" if has_gh_token
        else "\n⚠️ <b>GITHUB_TOKEN не задан</b> — writeback не сработает."
    )

    text = (
        f"🧠 <b>AI brain (Claude)</b>\n\n"
        f"Статус: {status_emoji} <b>{status_text}</b>\n"
        f"Модель: <code>{html.escape(model)}</code>\n"
        f"Brain chat: <code>{brain_id or '— не задан —'}</code>\n"
        f"Тишина сотрудников: <b>{idle_min}</b> мин "
        f"(0 = всегда отвечать)\n\n"
        f"<b>Reply-статистика:</b>\n"
        f"• Ответов: <b>{stats.get('replies_total', 0)}</b>\n"
        f"• Tokens in/out: {stats.get('input_tokens_total', 0)} / "
        f"{stats.get('output_tokens_total', 0)}\n"
        f"• Ошибок: {stats.get('errors_total', 0)}\n"
        f"• Пропущено (worker активен): {stats.get('skipped_worker_active', 0)}"
        f"{api_warn}\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📝 <b>Writeback в knowledge/</b>: {wb_emoji} {wb_status}\n"
        f"<i>Каждое сообщение в брейн-чате (не команда, не от юзербота) "
        f"AI попробует сохранить в graph через GitHub API.</i>\n"
        f"• Коммитов: <b>{wb_stats.get('commits_total', 0)}</b>\n"
        f"• Пропущено (не факт): {wb_stats.get('skipped_total', 0)}\n"
        f"• Ошибок: {wb_stats.get('errors_total', 0)}"
        f"{wb_warn}"
    )
    # Координаторская беседа (для эскалаций)
    coord_id = storage.get_coordination_chat_id()
    esc_stats = storage.get_escalate_stats()
    by_spec = esc_stats.get("by_specialist", {}) or {}
    by_spec_str = ", ".join(f"@{u}: {c}" for u, c in by_spec.items()) if by_spec else "—"

    text += (
        f"\n\n━━━━━━━━━━━━━━\n"
        f"🆘 <b>Координаторская беседа</b>: "
        f"<code>{coord_id or '— не задана —'}</code>\n"
        f"<i>Куда AI пишет вызовы специалистам команды (TimonSkupCL, "
        f"pride_sys01, pride_manager1).</i>\n"
        f"• Эскалаций: <b>{esc_stats.get('calls_total', 0)}</b>\n"
        f"• По специалистам: {by_spec_str}\n"
        f"• Ошибок: {esc_stats.get('errors_total', 0)}"
    )

    # Система учёта сделок (V2)
    lk_id = storage.get_lk_group_id()
    accounting_id = storage.get_accounting_group_id()
    deals_stats = storage.get_deals_stats()
    by_status = deals_stats.get("by_status", {}) or {}
    by_status_str = ", ".join(f"{s}: {c}" for s, c in by_status.items()) if by_status else "—"
    text += (
        f"\n\n━━━━━━━━━━━━━━\n"
        f"💼 <b>Система учёта сделок V2</b>\n"
        f"Группа 1 «Личные кабинеты»: <code>{lk_id or '— не задана —'}</code>\n"
        f"Группа 2 «Бухгалтерия»: <code>{accounting_id or '— не задана —'}</code>\n"
        f"• Сделок создано: <b>{deals_stats.get('created_total', 0)}</b>\n"
        f"• По статусам: {by_status_str}"
    )

    toggle_label = "🔴 Выключить AI" if enabled else "🟢 Включить AI"
    wb_label = "🔴 Выключить writeback" if wb_enabled else "🟢 Включить writeback"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data="adm:ai_toggle")],
        [InlineKeyboardButton(text=wb_label, callback_data="adm:wb_toggle")],
        [InlineKeyboardButton(text="🆔 Brain chat ID", callback_data="adm:ai_set_chat")],
        [InlineKeyboardButton(text="🆘 Координат. чат ID", callback_data="adm:ai_set_coord")],
        [InlineKeyboardButton(text="📊 ЛК-Группа ID", callback_data="adm:ai_set_lk")],
        [InlineKeyboardButton(text="📊 Бухгалтерия чат ID", callback_data="adm:ai_set_accounting")],
        [InlineKeyboardButton(text="🤖 Модель Claude", callback_data="adm:ai_set_model")],
        [InlineKeyboardButton(text="⏱ Тишина сотрудников", callback_data="adm:ai_set_idle")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")],
    ])
    await call.message.edit_text(text, reply_markup=kb)


async def render_traffic(call: CallbackQuery):
    """Список источников трафика, отсортированный по количеству ответов."""
    sources = storage.get_source_stats()
    total = sum(sources.values())
    if not sources:
        text = (
            "📈 <b>Источники трафика</b>\n\n"
            "<i>Пока никто не выбрал источник.</i>\n\n"
            "Источник записывается автоматически при первом ответе клиента "
            "на опрос после /start."
        )
    else:
        sorted_src = sorted(sources.items(), key=lambda x: -x[1])
        rows = []
        for src, cnt in sorted_src:
            pct = (cnt * 100) // total if total else 0
            rows.append(
                f"• <b>{html.escape(src)}</b> — {cnt} ({pct}%)"
            )
        text = (
            f"📈 <b>Источники трафика</b>\n\n"
            f"Всего ответов: <b>{total}</b>\n\n"
            + "\n".join(rows)
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")],
    ])
    await call.message.edit_text(text, reply_markup=kb)


async def render_admins(call: CallbackQuery):
    admins = storage.get_admins()
    text = f"🔐 <b>Админы ({len(admins)})</b>\n\n"
    text += "\n".join(f"• <code>{a}</code>" for a in admins) if admins else "<i>пусто</i>"
    text += f"\n\n<b>Секретная команда:</b>\n<code>/{storage.get_secret_command()}</code>\n"
    text += "<i>Кто отправит её боту — станет админом. Меняется только пересозданием storage.</i>"
    rows = [[InlineKeyboardButton(text=f"🗑 {a}", callback_data=f"adm:admin_del:{a}")] for a in admins]
    rows.append([InlineKeyboardButton(text="➕ Добавить по ID", callback_data="adm:admin_add")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# === FSM handlers ===

@router.message(AdminFSM.add_worker)
async def fsm_add_worker(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    username = (message.text or "").strip().lstrip("@")
    if not username or " " in username or len(username) > 64:
        await message.answer("Некорректный username. Пришлите ещё раз или /admin для отмены.")
        return
    await storage.add_worker(username)
    await state.clear()
    await message.answer(f"✅ Добавлен @{username}", reply_markup=main_menu_kb())


# --- FSM роли: 1 шаг (ввод названия) ---
# username сохраняется в state.data при клике на «🎭» рядом с работником.
# is_admin не меняется этим FSM — для него отдельная toggle-кнопка 👑/👤.

@router.message(AdminFSM.set_role_name)
async def fsm_set_role_name(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    role = (message.text or "").strip()
    if not role:
        await message.answer("Пустая роль. Пришлите название или /admin.")
        return
    if len(role) > 16:
        await message.answer("Telegram-лимит rank: 16 символов. Сократите.")
        return
    data = await state.get_data()
    uname = data.get("role_username")
    await state.clear()
    if not uname:
        await message.answer("Сессия потеряна. Открой /admin → 🎭 Роли заново.")
        return
    # Сохраняем роль, is_admin не трогаем (управляется toggle-кнопкой).
    existing = storage.get_worker_role(uname) or {}
    is_admin = bool(existing.get("is_admin"))
    await storage.set_worker_role(uname, role, is_admin)
    admin_mark = " 👑" if is_admin else ""
    await message.answer(
        f"✅ Роль для <b>@{uname}</b>: <b>{role}</b>{admin_mark}\n\n"
        f"<i>Переключить админку — кнопка 👑/👤 рядом с ником в /admin → 🎭 Роли.</i>",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_welcome)
async def fsm_set_welcome(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("Текст пустой. Пришлите ещё раз или /admin.")
        return
    # Capture entities (custom_emoji / bold / italic / etc.) for forwarded or styled messages.
    raw_entities = message.entities or message.caption_entities or []
    serialized = []
    for e in raw_entities:
        try:
            serialized.append(e.model_dump(mode="json", exclude_none=True))
        except Exception:
            try:
                serialized.append(e.dict(exclude_none=True))
            except Exception:
                pass
    await storage.set_welcome(text, serialized)
    await state.clear()
    note = f"✅ Приветствие обновлено (символов: {len(text)}, entities: {len(serialized)})"
    await message.answer(note, reply_markup=main_menu_kb())


@router.message(AdminFSM.set_cooldown)
async def fsm_set_cooldown(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        minutes = int((message.text or "").strip())
        if minutes < 0 or minutes > 100000:
            raise ValueError()
    except ValueError:
        await message.answer("Нужно число (минуты, 0-100000). Пришлите ещё раз или /admin.")
        return
    await storage.set_cooldown_minutes(minutes)
    await state.clear()
    await message.answer(f"✅ Кулдаун: {minutes} мин", reply_markup=main_menu_kb())


@router.message(AdminFSM.add_admin)
async def fsm_add_admin(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        new_id = int((message.text or "").strip())
        if new_id <= 0:
            raise ValueError()
    except ValueError:
        await message.answer(
            "Нужно числовой Telegram ID (положительное целое). "
            "Пришлите ещё раз или /admin для отмены."
        )
        return
    await storage.add_admin(new_id)
    await state.clear()
    await message.answer(
        f"✅ Добавлен админ <code>{new_id}</code>",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_brain_chat)
async def fsm_set_brain_chat(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        await message.answer(
            "Нужно целое число (chat_id). Пришлите ещё раз или /admin для отмены."
        )
        return
    await storage.set_brain_chat_id(chat_id)
    await state.clear()
    if chat_id == 0:
        await message.answer("✅ Brain chat очищен (логи отключены).", reply_markup=main_menu_kb())
    else:
        await message.answer(
            f"✅ Brain chat установлен: <code>{chat_id}</code>\n\n"
            f"<i>Убедитесь, что юзербот участник этого чата.</i>",
            reply_markup=main_menu_kb(),
        )


@router.message(AdminFSM.set_ai_model)
async def fsm_set_ai_model(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    raw = (message.text or "").strip()
    if raw and not raw.startswith("claude-"):
        await message.answer(
            "Имя модели должно начинаться с <code>claude-</code>. "
            "Пришлите ещё раз или /admin для отмены."
        )
        return
    await storage.set_ai_model(raw)
    await state.clear()
    effective = raw or config.DEFAULT_AI_MODEL
    await message.answer(
        f"✅ Модель AI: <code>{effective}</code>",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_idle_minutes)
async def fsm_set_idle_minutes(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        minutes = int((message.text or "").strip())
        if minutes < 0 or minutes > 1440:
            raise ValueError
    except ValueError:
        await message.answer("Число от 0 до 1440. Пришлите ещё раз или /admin.")
        return
    await storage.set_client_idle_minutes(minutes)
    await state.clear()
    await message.answer(
        f"✅ Тишина сотрудников: <b>{minutes}</b> мин",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_coord_chat)
async def fsm_set_coord_chat(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число. Пришлите ещё раз или /admin.")
        return
    await storage.set_coordination_chat_id(chat_id)
    await state.clear()
    if chat_id == 0:
        await message.answer(
            "✅ Координаторская беседа очищена (эскалация отключена).",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer(
            f"✅ Координаторская беседа: <code>{chat_id}</code>",
            reply_markup=main_menu_kb(),
        )


@router.message(AdminFSM.set_accounting_chat)
async def fsm_set_accounting_chat(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число. Пришлите ещё раз или /admin.")
        return
    if hasattr(storage, "set_accounting_group_id"):
        await storage.set_accounting_group_id(chat_id)
    await state.clear()
    await message.answer(
        f"✅ Чат «Бухгалтерия»: <code>{chat_id}</code>",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_lk_chat)
async def fsm_set_lk_chat(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число. Пришлите ещё раз или /admin.")
        return
    if hasattr(storage, "set_lk_group_id"):
        await storage.set_lk_group_id(chat_id)
    await state.clear()
    await message.answer(
        f"✅ Группа 1 «Личные кабинеты»: <code>{chat_id}</code>",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_role_name)
async def fsm_set_role_name(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    uname = data.get("role_username") or ""
    role = (message.text or "").strip()[:16]
    if not role:
        await message.answer("Пустое название. Пришлите ещё раз или /admin.")
        return
    cur = storage.get_worker_role(uname) or {}
    await storage.set_worker_role(uname, role, bool(cur.get("is_admin")))
    await state.clear()
    await message.answer(
        f"✅ Роль @{uname}: <b>{role}</b>",
        reply_markup=main_menu_kb(),
    )


# ─── InviteWork-бот: FSM-обработчики ─────────────────────────────

@router.message(AdminFSM.set_invite_gif)
async def fsm_set_invite_gif(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    if (message.text or "").strip() == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    if (message.text or "").strip() == "-":
        await storage.set_invite_welcome_gif("")
        await state.clear()
        await message.answer("✅ GIF welcome очищен.", reply_markup=main_menu_kb())
        return
    file_id = ""
    if message.animation:
        file_id = message.animation.file_id
    elif message.document and (message.document.mime_type or "").startswith("video/"):
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    if not file_id:
        await message.answer(
            "Не похоже на GIF/анимацию. Пришлите GIF одним сообщением или /admin для отмены."
        )
        return
    await storage.set_invite_welcome_gif(file_id)
    await state.clear()
    await message.answer(
        f"✅ GIF сохранён.\n<code>{file_id}</code>",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_invite_emoji)
async def fsm_set_invite_emoji(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Формат: <code>EMOJI DOCUMENT_ID</code>\nПример: <code>🔥 5462863737368090301</code>\n\n"
            "Или /admin для отмены."
        )
        return
    emoji, doc = parts[0], parts[1].strip()
    await storage.set_invite_premium_emoji_one(emoji, doc)
    await state.clear()
    if doc == "-":
        await message.answer(
            f"✅ Эмодзи {emoji} удалён из premium-маппинга.",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer(
            f"✅ Premium-эмодзи: {emoji} → <code>{doc}</code>",
            reply_markup=main_menu_kb(),
        )


@router.message(AdminFSM.set_invite_jobs)
async def fsm_set_invite_jobs(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or message.html_text or "").strip()
    if text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    if text == "-":
        await storage.set_invite_jobs_text("")
        await state.clear()
        await message.answer(
            "✅ Текст вакансий сброшен на дефолт.",
            reply_markup=main_menu_kb(),
        )
        return
    if not text:
        await message.answer("Пустой текст. Пришлите ещё раз или /admin.")
        return
    await storage.set_invite_jobs_text(text)
    await state.clear()
    await message.answer(
        f"✅ Текст вакансий обновлён ({len(text)} симв.).",
        reply_markup=main_menu_kb(),
    )

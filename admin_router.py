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
    set_deals_chat = State()
    set_accounts_chat = State()
    set_accounting_chat = State()


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Работники", callback_data="adm:workers")],
        [InlineKeyboardButton(text="💬 Приветствие", callback_data="adm:welcome")],
        [InlineKeyboardButton(text="⏱ Кулдаун", callback_data="adm:cooldown")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📈 Источники трафика", callback_data="adm:traffic")],
        [InlineKeyboardButton(text="🧠 AI (Claude)", callback_data="adm:ai")],
        [InlineKeyboardButton(text="🔐 Админы", callback_data="adm:admins")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="adm:close")],
    ])


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        return  # silent ignore
    await state.clear()
    await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())


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
    elif action == "ai_set_deals":
        await call.message.edit_text(
            "Пришлите ID чата «Сделки и выплаты» (число, например "
            "<code>-1001234567890</code>, или <code>0</code> чтобы отключить).\n\n"
            "<i>Юзербот должен быть участником. Туда он будет логировать новые "
            "сделки и обновления статусов в формате «@ник — банк — fee — сумма "
            "— дата — id — СТАТУС».</i>\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_deals_chat)
    elif action == "ai_set_accounting":
        await call.message.edit_text(
            "Пришлите ID чата «Бухгалтерия» (число или <code>0</code> чтобы отключить).\n\n"
            "<i>Юзербот должен быть участником этой беседы. Туда будут приходить "
            "ежедневные отчёты + менеджер вводит туда команды (приход/расход/курс/ЛК/etc).</i>\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_accounting_chat)
    elif action == "ai_set_accounts":
        await call.message.edit_text(
            "Пришлите ID чата «Отработка аккаунтов» (число или <code>0</code>).\n\n"
            "<i>Юзербот должен быть участником. AI будет читать оттуда сообщения "
            "формата «[ФИО] — [БАНК] — ОТРАБОТАНО» и обновлять статусы сделок.</i>\n\n"
            "Или /admin для отмены.",
            reply_markup=back_kb(),
        )
        await state.set_state(AdminFSM.set_accounts_chat)
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
    text = f"👥 <b>Работники ({len(workers)})</b>\n\n"
    text += "\n".join(f"• @{w}" for w in workers) if workers else "<i>пусто</i>"
    rows = [[InlineKeyboardButton(text=f"🗑 @{w}", callback_data=f"adm:worker_del:{w}")] for w in workers]
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="adm:worker_add")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def render_welcome(call: CallbackQuery):
    welcome = storage.get_welcome()
    safe = welcome.replace("<", "&lt;").replace(">", "&gt;")
    text = f"💬 <b>Приветствие</b>\n\nТекущий текст:\n\n<blockquote>{safe}</blockquote>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="adm:welcome_edit")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm:main")],
    ])
    await call.message.edit_text(text, reply_markup=kb)


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

    # Система учёта сделок
    deals_id = storage.get_deals_group_id()
    accounts_id = storage.get_accounts_group_id()
    accounting_id = storage.get_accounting_group_id()
    deals_stats = storage.get_deals_stats()
    by_status = deals_stats.get("by_status", {}) or {}
    by_status_str = ", ".join(f"{s}: {c}" for s, c in by_status.items()) if by_status else "—"
    text += (
        f"\n\n━━━━━━━━━━━━━━\n"
        f"💼 <b>Система учёта сделок</b>\n"
        f"Чат «Сделки и выплаты»: <code>{deals_id or '— не задан —'}</code>\n"
        f"Чат «Отработка аккаунтов»: <code>{accounts_id or '— не задан —'}</code>\n"
        f"Чат «Бухгалтерия»: <code>{accounting_id or '— не задан —'}</code>\n"
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
        [InlineKeyboardButton(text="💼 Чат сделок ID", callback_data="adm:ai_set_deals")],
        [InlineKeyboardButton(text="💼 Чат отработки ID", callback_data="adm:ai_set_accounts")],
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
    # Простая валидация: только claude-* модели или пустая строка для дефолта
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
        f"✅ Модель: <code>{effective}</code>",
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
            raise ValueError()
    except ValueError:
        await message.answer(
            "Нужно целое число от 0 до 1440. Пришлите ещё раз или /admin для отмены."
        )
        return
    await storage.set_client_idle_minutes(minutes)
    await state.clear()
    descr = (
        "AI всегда отвечает (без проверки активности сотрудников)"
        if minutes == 0
        else f"AI молчит, если worker писал в последние <b>{minutes}</b> мин"
    )
    await message.answer(
        f"✅ Тишина сотрудников: <b>{minutes}</b> мин\n{descr}",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_deals_chat)
async def fsm_set_deals_chat(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear(); return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число (chat_id). Пришлите ещё раз или /admin.")
        return
    await storage.set_deals_group_id(chat_id)
    await state.clear()
    label = "очищен (логирование сделок отключено)" if chat_id == 0 else f"<code>{chat_id}</code>"
    await message.answer(
        f"✅ Чат «Сделки и выплаты»: {label}",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_accounts_chat)
async def fsm_set_accounts_chat(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear(); return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число (chat_id). Пришлите ещё раз или /admin.")
        return
    await storage.set_accounts_group_id(chat_id)
    await state.clear()
    label = "очищен" if chat_id == 0 else f"<code>{chat_id}</code>"
    await message.answer(
        f"✅ Чат «Отработка аккаунтов»: {label}",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFSM.set_accounting_chat)
async def fsm_set_accounting_chat(message: Message, state: FSMContext):
    if not storage.is_admin(message.from_user.id):
        await state.clear(); return
    if message.text == "/admin":
        await state.clear()
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=main_menu_kb())
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число (chat_id). Пришлите ещё раз или /admin.")
        return
    await storage.set_accounting_group_id(chat_id)
    await state.clear()
    label = "очищен" if chat_id == 0 else f"<code>{chat_id}</code>"
    await message.answer(
        f"✅ Чат «Бухгалтерия»: {label}\n\n"
        f"<i>Команды в чате — пришлите «помощь» / «?» / «/help».</i>",
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
        await message.answer(
            "Нужно целое число (chat_id). Пришлите ещё раз или /admin для отмены."
        )
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
            f"✅ Координаторская беседа: <code>{chat_id}</code>\n\n"
            f"<i>Убедитесь, что юзербот участник этой беседы и что в ней "
            f"присутствуют @TimonSkupCL, @pride_sys01, @pride_manager1.</i>",
            reply_markup=main_menu_kb(),
        )
"""Admin panel: /admin command, inline-keyboard menu, FSM for inputs."""
import logging
from aiogram import Router, F
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


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Работники", callback_data="adm:workers")],
        [InlineKeyboardButton(text="💬 Приветствие", callback_data="adm:welcome")],
        [InlineKeyboardButton(text="⏱ Кулдаун", callback_data="adm:cooldown")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
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
            "Пришлите новый текст приветствия. Можно с эмодзи и переносами.\n\n"
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
    await storage.set_welcome(text)
    await state.clear()
    await message.answer("✅ Приветствие обновлено", reply_markup=main_menu_kb())


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
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число (Telegram ID). Пришлите ещё раз или /admin.")
        return
    await storage.add_admin(uid)
    await state.clear()
    await message.answer(f"✅ Добавлен админ <code>{uid}</code>", reply_markup=main_menu_kb())

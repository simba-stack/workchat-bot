"""Bot commands /wallet /p2p /checks /swap — open Mini-App на нужном экране."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from core.config import settings

router = Router(name="commands")


def _btn(label: str, view: str) -> InlineKeyboardMarkup:
    url = f"{settings.miniapp_url}{settings.miniapp_path}"
    if view:
        url = f"{url}?view={view}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url)),
    ]])


@router.message(Command("wallet"))
async def cmd_wallet(message: Message):
    await message.answer("💼 <b>Кошелёк</b>\nБалансы всех монет", reply_markup=_btn("💼 Открыть кошелёк", ""))


@router.message(Command("p2p"))
async def cmd_p2p(message: Message):
    """P2P в режиме чатов — inline-меню в боте, не Mini-App."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Купить крипту", callback_data="p2p_buy")],
        [InlineKeyboardButton(text="📉 Продать крипту", callback_data="p2p_sell")],
        [InlineKeyboardButton(text="📦 Мои сделки", callback_data="p2p_my_deals")],
        [InlineKeyboardButton(text="🦄 Создать объявление", callback_data="p2p_create")],
        [InlineKeyboardButton(text="⚙ Оплата и валюта", callback_data="p2p_settings")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="p2p_profile")],
    ])
    await message.answer(
        "💱 <b>P2P Маркет</b>\n\n"
        "Здесь вы можете <a href=\"#\">купить</a> или <a href=\"#\">продать</a> "
        "криптовалюту переводом на карту или электронный кошелёк.\n\n"
        "<i>Скоро: выбор криптовалюты → способ оплаты → объявления → чат-сделка.</i>",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("p2p_"))
async def cb_p2p(call):
    """Заглушка P2P callback'ов — пока скоро будет."""
    actions = {
        "p2p_buy": "📈 Купить крипту",
        "p2p_sell": "📉 Продать крипту",
        "p2p_my_deals": "📦 Мои сделки",
        "p2p_create": "🦄 Создать объявление",
        "p2p_settings": "⚙ Оплата и валюта",
        "p2p_profile": "👤 Мой профиль",
    }
    label = actions.get(call.data, "P2P")
    await call.answer(f"{label} — раздел в разработке, скоро откроем 🚀", show_alert=True)


@router.message(Command("checks"))
async def cmd_checks(message: Message):
    await message.answer("📜 <b>Чеки</b>\nСоздай чек — отправь ссылку любому", reply_markup=_btn("📜 Открыть чеки", "checks"))


@router.message(Command("swap"))
async def cmd_swap(message: Message):
    await message.answer("🔄 <b>Обмен</b>\nОбмен между монетами", reply_markup=_btn("🔄 Открыть обмен", "swap"))

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
    await message.answer("💱 <b>P2P Маркет</b>\nКупить/продать крипту у других юзеров", reply_markup=_btn("💱 Открыть P2P", "p2p"))


@router.message(Command("checks"))
async def cmd_checks(message: Message):
    await message.answer("📜 <b>Чеки</b>\nСоздай чек — отправь ссылку любому", reply_markup=_btn("📜 Открыть чеки", "checks"))


@router.message(Command("swap"))
async def cmd_swap(message: Message):
    await message.answer("🔄 <b>Обмен</b>\nОбмен между монетами", reply_markup=_btn("🔄 Открыть обмен", "swap"))

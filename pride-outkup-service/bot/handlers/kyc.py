"""KYC flow в ЛС бота — stub под Phase A2.

Полная реализация в Phase A2:
- FSM: ФИО → паспорт серия/номер → КУЦ-видео-ссылка → confirm
- webhook в JARVIS
- ожидание модерации
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="kyc")


@router.message(Command("kyc"))
async def cmd_kyc(message: Message):
    await message.answer(
        "📋 Верификация запускается из Mini-App.\n"
        "Жми /start чтобы открыть приложение → Профиль → Пройти KYC."
    )

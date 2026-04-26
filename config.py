"""
Konfiguraciya bota.
Sekrety tyanutsya iz .env, ostalnoye pravitsya pryamo zdes.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# === Sekrety (iz .env / peremennykh Railway) ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
USERBOT_PHONE = os.getenv("USERBOT_PHONE", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# StringSession dlya userbot (@pride_invite)
STRING_SESSION = os.getenv("STRING_SESSION", "")

# === Spisok rabotnikov (bez @) ===
WORKERS = [
    "pride_sys01",
    "pride_manager1",
    "TimonSkupCL",
]

# === Nastroyki besedy ===
CHAT_TITLE_TEMPLATE = "Работа - {client_name}"
CHAT_DESCRIPTION_TEMPLATE = "Рабочая беседа с клиентом {client_name}"

TRIGGER_PHRASES = [
    "выдай рабочую беседу",
    "создай рабочую беседу",
    "новая беседа",
]

USERBOT_AS_ADMIN = True


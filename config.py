"""Static configuration. Dynamic settings live in storage.py."""
import os
from dotenv import load_dotenv

load_dotenv()

# === Secrets (env / Railway Variables) ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
USERBOT_PHONE = os.getenv("USERBOT_PHONE", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # bootstrap admin
STRING_SESSION = os.getenv("STRING_SESSION", "")

# Persistent JSON storage path (mount Railway Volume here)
STORAGE_PATH = os.getenv("STORAGE_PATH", "/app/data/state.json")

# === Defaults (used on first run; later editable via /admin) ===
DEFAULT_WELCOME = (
    "👋 Здравствуйте!\n\n"
    "Это ваша рабочая беседа. Наши специалисты уже здесь — опишите задачу, "
    "и мы свяжемся с вами в ближайшее время."
)
DEFAULT_COOLDOWN_MIN = 60
DEFAULT_TRIGGERS = ["выдай рабочую беседу", "создай рабочую беседу", "новая беседа"]
DEFAULT_WORKERS = ["pride_sys01", "pride_manager1", "TimonSkupCL", "SIMBA_PRIDE_ADM"]

# === Static chat settings ===
CHAT_TITLE_TEMPLATE = "[PRIDE] Поставки РС | {client_name}"
CHAT_DESCRIPTION_TEMPLATE = "[PRIDE] Поставки РС с клиентом {client_name}"
USERBOT_AS_ADMIN = True

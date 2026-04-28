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

# === AI brain (Anthropic Claude) ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Default model — admin can override at runtime via /admin → AI → model.
DEFAULT_AI_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
# Max output tokens per reply. Keeps responses short and predictable.
AI_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))
# How many recent chat messages to feed Claude as conversation history.
AI_HISTORY_LIMIT = int(os.getenv("CLAUDE_HISTORY_LIMIT", "30"))
# How many recent brain_chat messages to include as admin notes / extra context.
AI_BRAIN_NOTES_LIMIT = int(os.getenv("CLAUDE_BRAIN_NOTES_LIMIT", "30"))
# Random typing delay before sending reply (seconds, min..max). Realism.
AI_TYPING_DELAY_MIN = float(os.getenv("CLAUDE_TYPING_DELAY_MIN", "3"))
AI_TYPING_DELAY_MAX = float(os.getenv("CLAUDE_TYPING_DELAY_MAX", "8"))

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

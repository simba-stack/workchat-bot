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
# Default model — Haiku 4.5 (12× дешевле Sonnet, для 95% диалогов хватает).
# Можно переопределить через env или /admin для конкретных чатов.
DEFAULT_AI_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
# Sonnet включается через storage.ai_smart_routing — для сложных кейсов
# (claim/escalation/деньги). По умолчанию выкл — экономим.
SMART_ROUTING_MODEL = os.getenv("CLAUDE_SMART_MODEL", "claude-sonnet-4-6")
# Max output tokens per reply — короткие реплики экономят. Сократил с 1024 до 512.
AI_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "512"))
# History limit — было 30, теперь 15 (хватает контекста, токенов вдвое меньше).
AI_HISTORY_LIMIT = int(os.getenv("CLAUDE_HISTORY_LIMIT", "15"))
# Brain notes — было 30, теперь 10.
AI_BRAIN_NOTES_LIMIT = int(os.getenv("CLAUDE_BRAIN_NOTES_LIMIT", "10"))
# Random typing delay before sending reply (seconds, min..max). Realism.
AI_TYPING_DELAY_MIN = float(os.getenv("CLAUDE_TYPING_DELAY_MIN", "3"))
AI_TYPING_DELAY_MAX = float(os.getenv("CLAUDE_TYPING_DELAY_MAX", "8"))

# === Фильтр релевантности (экономия токенов) ===
# Перед основным AI-вызовом запускается дешёвый Haiku-классификатор:
# нужно ли вообще отвечать на это сообщение (или это болтовня/шутки/реакции).
# ~$0.0001 за вызов, отсекает ~30-40% бесполезных запросов.
AI_RELEVANCE_CHECK_ENABLED = os.getenv("AI_RELEVANCE_CHECK", "1") not in ("0", "false", "no")
# Подсказка отправляется один раз на чат при первом «skip»:
# «Если я вам понадоблюсь — напишите Ассистент и дальше свой вопрос»
AI_ASSISTANT_HINT_ENABLED = os.getenv("AI_ASSISTANT_HINT", "1") not in ("0", "false", "no")
AI_ASSISTANT_HINT_TEXT = os.getenv(
    "AI_ASSISTANT_HINT_TEXT",
    "Если я вам понадоблюсь — просто напишите «Ассистент» и дальше свой вопрос.",
)

# === GitHub writeback (memory.py) ===
# Token used to commit knowledge/*.md updates back to the repo.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO_OWNER = os.getenv("GITHUB_REPO_OWNER", "simba-stack")
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO_NAME", "workchat-bot")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
# Subdir inside the repo where knowledge files live.
KNOWLEDGE_SUBDIR = os.getenv("KNOWLEDGE_SUBDIR", "knowledge")

# Persistent JSON storage path (mount Railway Volume here)
STORAGE_PATH = os.getenv("STORAGE_PATH", "/app/data/state.json")

# === Credit (Кредитование) — фиксированные ID групп Telegram ===
# Можно переопределить через env. Если ID неверный — сервис продолжит работать,
# но захардкоженные main-чаты не будут распознаваться (доп. чаты через
# «Ассистент возьми этот чат под кредитование - менеджер @ник» работают всегда).
CREDIT_ACCESS_CHAT_ID = int(os.getenv("CREDIT_ACCESS_CHAT_ID", "-1005116975272"))
CREDIT_PASSWORD_CHAT_ID = int(os.getenv("CREDIT_PASSWORD_CHAT_ID", "-1005234590907"))

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

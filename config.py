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

# === Credit (Кредитование) — фиксированные ID централизованных групп Telegram ===
# Это «общие» группы куда CRM-бот пишет анкеты/ЛК всех кредитных клиентов
# (зеркало HARDCODED_ADMIN_CHAT_ID / HARDCODED_PASSWORD_CHAT_ID у поставщиков).
# Рабочие группы клиентов (где юрист общается) — отдельная история, они
# регистрируются через команду «Ассистент возьми этот чат под кредитование - менеджер @ник»
# и попадают в storage.credit_chats. Обе цепочки активируют credit-track.
CREDIT_ACCESS_CHAT_ID = int(os.getenv("CREDIT_ACCESS_CHAT_ID", "-1003457011118"))
CREDIT_PASSWORD_CHAT_ID = int(os.getenv("CREDIT_PASSWORD_CHAT_ID", "-1003945639230"))

# === Outsource bot (@marketplace_PRIDE_BOT) — лавка PRIDE для управляющих ===
# Токен от @BotFather. ВАЖНО: НЕ хардкодить в репо — задавать через Railway env.
OUTSOURCE_BOT_TOKEN = os.getenv("OUTSOURCE_BOT_TOKEN", "")

# === Defaults (used on first run; later editable via /admin) ===
DEFAULT_WELCOME = (
    "👋 Здравствуйте!\n\n"
    "Это ваша рабочая беседа. Наши специалисты уже здесь — опишите задачу, "
    "и мы свяжемся с вами в ближайшее время."
)
DEFAULT_COOLDOWN_MIN = 60
DEFAULT_TRIGGERS = ["выдай рабочую беседу", "создай рабочую беседу", "новая беседа"]
DEFAULT_WORKERS = ["pride_sys01", "pride_manager1", "TimonSkupCL", "SIMBA_PRIDE_ADM"]

# === Scripted texts (welcome flow — заскриптован БЕЗ Claude API) ===
# Админ редактирует через /admin → «📝 Скрипты сообщений» — пересылает боту
# сообщение с премиум-эмодзи, бот сохраняет text + entities в
# storage.state["scripted_texts"][key]. Юзербот отправляет их через Telethon
# `send_message(formatting_entities=)` — премиум эмодзи анимируются у клиента.
# Экономия: welcome+choice = 0 API calls к Claude, кредиты идут только на
# живой диалог после выбора направления.
# ═══════════════════════════════════════════════════════════════════════
# Интеграция с @PRIDE_AUDIT_BOT (stroy-crm-bot) — управление LK-карточками
# ═══════════════════════════════════════════════════════════════════════
# URL Express API audit-bot'а (Railway deployment URL, без trailing slash)
AUDIT_BOT_URL = os.getenv("AUDIT_BOT_URL", "")
# Bearer-токен для API аутентификации (тот же что задан в audit-bot Railway env)
AUDIT_BOT_API_TOKEN = os.getenv("AUDIT_BOT_API_TOKEN", "")
# HMAC-секрет для верификации webhook'ов от audit-bot (тот же секрет в обоих)
AUDIT_BOT_WEBHOOK_HMAC_SECRET = os.getenv("AUDIT_BOT_WEBHOOK_HMAC_SECRET", "")


SCRIPTED_TEXTS_DEFAULTS = {
    # 1) Первое сообщение клиенту после присоединения к workchat
    "welcome": {
        "title": "👋 Welcome с меню",
        "text": (
            "🤩 Вы попали в рабочую инфраструктуру корпорации PRIDE.\n\n"
            "🤝 Какой вариант сотрудничества Вас интересует?\n\n"
            "1. ИП/ООО\n"
            "3. Дебет\n\n"
            "Выберите одно из направлений написав его порядковый номер "
            "или название направления."
        ),
        "entities": [],
    },
    # 2) Ответ после выбора «1» / «ИП» — AI подключится по ИП дальше
    "reply_ip": {
        "title": "✅ Ответ на ИП/ООО",
        "text": (
            "Отлично! Направление ИП/ООО.\n\n"
            "Расскажите о вашем банке и обороте — я подскажу условия выкупа."
        ),
        "entities": [],
    },
    # 3) Ответ после выбора «3» / «Дебет» — оператор Дебет приглашается
    "reply_debet": {
        "title": "✅ Ответ на Дебет",
        "text": (
            "Понял, направление Дебет.\n\n"
            "Оператор скоро подключится и расскажет об условиях."
        ),
        "entities": [],
    },
    # 4) Fallback когда клиент написал что-то не «1» и не «3»
    "fallback_not_understood": {
        "title": "⚠️ Не понял выбор",
        "text": (
            "Не понял выбор. Напишите цифру 1 (ИП/ООО) или 3 (Дебет), "
            "либо название направления."
        ),
        "entities": [],
    },
    # ═══════════════════════════════════════════════════════════════════
    # Волна 2 — перенос хардкода из userbot.py в scripted_texts
    # Placeholder'ы: {bank} {fio} {deal_id} {price_usdt} {usdt_address}
    #                {client_tag} {client_username}
    # ═══════════════════════════════════════════════════════════════════

    # 5) После «✅ Перевязка ЛК <банк> успешно выполнена» юзербот просит
    #    метод оплаты. Раньше был hardcoded в _auto_ask_payment_method_after_perevyaz.
    "ask_payment_method_after_perevyaz": {
        "title": "💰 Спросить тип оплаты после перевязки",
        "text": (
            "{client_tag}Перевязка успешно завершена.\n\n"
            "Подскажите — как проведём сделку?\n\n"
            "🤝 Через гарант в Continental — пришлите номер сделки @PRIDE_BUHGALTERIA\n"
            "   • сейчас — мы пополним сделку, дальше работаем со счётом, "
            "отпускаем после отработки\n"
            "   • после отработки — пополним и отпустим по факту "
            "завершения работы со счётом\n\n"
            "💸 Без гаранта (USDT) — оплатим напрямую после отработки счёта. "
            "Адрес USDT спросим отдельно когда счёт будет готов."
        ),
        "entities": [],
    },

    # Новое (июль 2026): USDT-адрес спрашиваем только когда операционист
    # перевёл карточку в Гр2 «ОТРАБОТАНО» и метод = trc. До этого клиент
    # мог не знать точно на какой кошелёк принять — теперь у него есть время.
    "ask_usdt_address_after_done": {
        "title": "💸 Спросить USDT-адрес после отработки",
        "text": (
            "{client_tag}Ваш счёт по {bank} отработан ✅\n\n"
            "Пришлите ваш USDT TRC20-адрес для получения выплаты."
        ),
        "entities": [],
    },

    # 6-11) Уведомления клиенту о смене статуса сделки (из _client_status_message)
    "lk_status_popolnena": {
        "title": "💼 Статус сделки: ПОПОЛНЕНО",
        "text": "Сделка #{deal_id} пополнена ({bank}), начинаем работу.",
        "entities": [],
    },
    "lk_status_v_rabote": {
        "title": "⚙️ Статус сделки: В РАБОТЕ",
        "text": "Ваш аккаунт #{deal_id} ({bank}) в работе.",
        "entities": [],
    },
    "lk_status_gotovo_k_otpusku": {
        "title": "✅ Статус сделки: ГОТОВО К ОТПУСКУ",
        "text": "Сделка #{deal_id} ({bank}) почти готова к отпуску.",
        "entities": [],
    },
    "lk_status_zavershena": {
        "title": "🎉 Статус сделки: ЗАВЕРШЕНА",
        "text": "Сделка #{deal_id} завершена ({bank}), всё прошло успешно.",
        "entities": [],
    },
    "lk_status_zablokirovan": {
        "title": "⚠️ Статус сделки: ЗАБЛОКИРОВАН",
        "text": "По сделке #{deal_id} ({bank}) есть нюансы — оператор разбирается.",
        "entities": [],
    },
    "lk_status_otmena_sdelki": {
        "title": "🛑 Статус сделки: ОТМЕНА",
        "text": "Сделка #{deal_id} ({bank}) приостановлена. Менеджер свяжется.",
        "entities": [],
    },

    # 12-13) AI-hint под каждым AI-ответом (первый / последующие)
    "ai_hint_first": {
        "title": "💡 AI hint: первый ответ",
        "text": (
            "\n\n💬 Если я вам понадоблюсь — просто напишите «Ассистент» "
            "и дальше свой вопрос. Если хотите живого оператора — "
            "«Ассистент позови оператора»."
        ),
        "entities": [],
    },
    "ai_hint_next": {
        "title": "💡 AI hint: последующие ответы",
        "text": (
            "\n\n💬 Если нужен живой оператор — напишите "
            "«Ассистент позови оператора»."
        ),
        "entities": [],
    },

    # ═══════════════════════════════════════════════════════════════════
    # Волна 3 — regex-перехват частых FAQ ДО вызова Claude (экономия)
    # Placeholder'ы такие же. {pricing} — динамика из storage.pricing.
    # ═══════════════════════════════════════════════════════════════════

    # 14) Прайс — самый частый вопрос. ~$1080/мес экономия.
    "reply_pricing_generic": {
        "title": "💵 Прайс (без банка)",
        "text": (
            "Актуальные цены:\n\n"
            "{pricing}\n\n"
            "Если интересует конкретный банк — напишите его название."
        ),
        "entities": [],
    },

    # 15-18) Метод оплаты — типовой вопрос при регистрации. ~$210/мес.
    "reply_payment_options": {
        "title": "💳 Методы оплаты (общий)",
        "text": (
            "У нас 3 варианта выплаты:\n\n"
            "💸 USDT TRC20 — сразу после отработки счёта\n"
            "🤝 Гарант в Continental сейчас — пополняем сделку, "
            "работаем со счётом, отпускаем после\n"
            "🤝 Гарант в Continental после — пополним и отпустим по факту\n\n"
            "Что предпочитаете?"
        ),
        "entities": [],
    },
    "reply_payment_usdt_hint": {
        "title": "💸 USDT: попросить адрес",
        "text": (
            "Ок, USDT TRC20. Пришлите ваш TRC20-адрес, "
            "переведём сразу после отработки счёта операционистами."
        ),
        "entities": [],
    },
    "reply_payment_guarantor_before": {
        "title": "🤝 Гарант до (детали)",
        "text": (
            "Гарант в Continental сейчас — мы пополним сделку сразу, "
            "дальше работаем со счётом и отпускаем после отработки. "
            "Сделка с @PRIDE_BUHGALTERIA."
        ),
        "entities": [],
    },
    "reply_payment_guarantor_after": {
        "title": "🤝 Гарант после (детали)",
        "text": (
            "Гарант в Continental после отработки — пополним и отпустим "
            "по факту завершения работы со счётом. Сделка с @PRIDE_BUHGALTERIA."
        ),
        "entities": [],
    },

    # 19) Холд — редкий но всегда одинаковый вопрос
    "reply_hold": {
        "title": "⏱ Про холд",
        "text": "Срок холда — от 1 до 3 дней в зависимости от банка.",
        "entities": [],
    },

    # 20) «Чем занимаетесь?» / «Что покупаете?» — первый вопрос новичков
    "reply_what_we_do": {
        "title": "❓ Чем занимаетесь",
        "text": (
            "Мы работаем с ИП/ООО-счетами (перевязка, работа со счётом, "
            "отработка). Основные направления — Альфа, Сбер, Тинькофф, "
            "ВТБ, Точка, Модуль, Райффайзен, Открытие. "
            "Есть карточки Дебет — если это про них, оператор подключится. "
            "Что именно интересует?"
        ),
        "entities": [],
    },

    # 21) «Почему блок?» когда карточка в статусе БЛОК/БРАК — жёсткий шаблон
    "reply_lk_blocked_explain": {
        "title": "🚫 Объяснение почему блок",
        "text": (
            "Причину блокировки счёта знает только банк — обратитесь "
            "непосредственно в поддержку банка, они подскажут детали. "
            "Мы лишь работаем со счётом, влиять на решения банка не можем."
        ),
        "entities": [],
    },

    # Слоты для будущих скриптов (follow-up через 2ч, финализация, ...):
    # SIMBA добавляет новые ключи через /add в группе-админке юзербота.
}

# === Static chat settings ===
CHAT_TITLE_TEMPLATE = "[PRIDE] Поставки РС | {client_name}"
CHAT_DESCRIPTION_TEMPLATE = "[PRIDE] Поставки РС с клиентом {client_name}"
USERBOT_AS_ADMIN = True

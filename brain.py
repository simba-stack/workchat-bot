"""Anthropic Claude integration: knowledge loader + reply generator.

Architecture:
- System prompt = concatenated knowledge/*.md (excluding memories/) + brain_chat notes
- Each call: pass conversation history + new client message
- Returns (reply_text, usage_dict) or (None, None) on error.

Used by userbot.py when client writes in a managed chat.
"""
import logging
import re
from pathlib import Path
from typing import Optional

from anthropic import AsyncAnthropic, APIError

import config
from storage import storage

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
# Skip memories/ subdirectory and any file starting with _ or .
_SKIP_NAMES = {"memories", ".obsidian"}

# === Tools для AI (Claude tool_use) ===
# Каждый tool описывает одно атомарное действие, которое AI может вызвать
# через Anthropic API. Реальное выполнение делает userbot.py через Telethon.
PARTNER_TOOL = {
    "name": "add_partner_to_crm",
    "description": (
        "Регистрирует клиента как партнёра в ЦРМ. Выполняет 3 шага АВТОМАТИЧЕСКИ "
        "в текущей рабочей беседе: "
        "(1) добавляет бота @PrideCRMv4_bot в чат, "
        "(2) даёт ему права админа, "
        "(3) отправляет команду '+партнер @<username_клиента>'. "
        "ВНИМАНИЕ: тег ВСЕГДА '+партнер', НИКОГДА '+поставщик' (старая терминология). "
        "ТРИГГЕР ВЫЗОВА — только когда соблюдены ВСЕ условия:\n"
        "1) Клиент явно подтвердил, что готов продать/передать счёт/РС/ИП (не просто "
        "что у него есть, а именно готов передать).\n"
        "2) Если счёт чужой (на дропа/подопечного) — клиент САМ задал вопрос про "
        "выплату/гарант/разделение (например 'можно ли депнуть в гарант на двоих'). "
        "Если он не спросил — НЕ добавляй в ЦРМ, НЕ задавай вопрос про раздел выплат.\n"
        "3) Клиент готов начать оформление сейчас — не на этапе обсуждения цены или "
        "общих условий.\n"
        "Если хотя бы одно условие не выполнено — НЕ вызывай инструмент. Сначала уточни "
        "недостающее или продолжи переговоры словами."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "client_username": {
                "type": "string",
                "description": (
                    "Telegram username клиента БЕЗ @ префикса (например 'rfc_tasya'). "
                    "Возьми из блока 'ТЕКУЩИЙ КЛИЕНТ' в системном промпте."
                ),
            }
        },
        "required": ["client_username"],
    },
}

ESCALATE_TOOL = {
    "name": "escalate_to_team",
    "description": (
        "Вызывает специалиста команды на помощь в координаторскую беседу. "
        "Используй ТОЛЬКО когда:\n"
        "1) Клиент задал вопрос вне твоей базы знаний и ты не можешь ответить\n"
        "2) Клиент явно недоволен твоими ответами / просит человека\n"
        "3) Клиент полностью заполнил анкету в ЦРМ и она отправлена @PrideCRMv4_bot — "
        "нужен @pride_sys01 для перевяза ЛК\n"
        "4) Ситуация требует решения человека (цены вне прайса, скидки, исключения)\n\n"
        "Кто за что:\n"
        "• TimonSkupCL — самые сложные вопросы, что не знают другие специалисты\n"
        "• pride_sys01 — ТОЛЬКО когда в чате появилось буквально «✔️ Отправлено "
        "на обработку» от @PrideCRMv4_bot. НЕ путать с «Данные обновлены» — "
        "это промежуточный статус, на него @pride_sys01 НЕ зовётся.\n"
        "• pride_manager1 — рутинные вопросы по чату, ДО заполнения ЦРМ\n\n"
        "ЗАПРЕЩЕНО:\n"
        "- Эскалировать на ровном месте. Сначала попробуй ответить сам по базе знаний.\n"
        "- Эскалировать когда клиент просто ВЫБИРАЕТ опцию из предложенных тобой "
        "(например в ответ на «сейчас или после перевязки?» сказал «перевязки» — это "
        "выбор пути, а НЕ повод звать человека; используй add_partner_to_crm).\n"
        "- Эскалировать когда клиент дал короткий ответ типа «да/нет/окей/перевязки/"
        "сейчас» в ходе обычного флоу — это нормальный диалог, продолжай сам.\n"
        "Эскалируй только когда реально нужен человек (вне базы знаний или явная "
        "просьба клиента позвать менеджера)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "specialist": {
                "type": "string",
                "enum": ["TimonSkupCL", "pride_sys01", "pride_manager1"],
                "description": "Username специалиста БЕЗ @ префикса",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Краткая причина вызова (1 предложение, почему нужен именно "
                    "этот специалист)"
                ),
            },
            "client_question": {
                "type": "string",
                "description": (
                    "Что спросил/попросил клиент — дословно или кратко 1-2 "
                    "предложения. Помогает специалисту сразу понять контекст."
                ),
            },
        },
        "required": ["specialist", "reason", "client_question"],
    },
}

# === Tools для системы учёта сделок ===
RECORD_DEAL_TOOL = {
    "name": "record_deal",
    "description": (
        "Записывает НОВУЮ сделку в базу storage.deals. Используй после того как "
        "клиент подтвердил сумму И прислал ID сделки из гарант-системы. "
        "ВАЖНО: 1 аккаунт = 1 сделка — даже если у клиента несколько аккаунтов, "
        "вызывай этот инструмент отдельно для каждого. Сделка автоматически "
        "привязывается к карточке ЛК клиента в Группе 1, никаких ручных публикаций "
        "делать не нужно. См. knowledge/deals.md для полного флоу."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "deal_id": {"type": "string", "description": "Уникальный ID сделки от клиента (например ID12345)"},
            "client_username": {"type": "string", "description": "Telegram username клиента БЕЗ @"},
            "fio": {"type": "string", "description": "ФИО клиента/держателя счёта"},
            "bank": {"type": "string", "description": "Банк (Альфа, ОЗОН, Райффайзен, ВТБ, Точка, Уралсиб, ЛОКО, БКС, Дело, УБРИР)"},
            "amount": {"type": "string", "description": "Сумма к выплате клиенту (как строка с валютой, например '50000₽' или '500$')"},
            "fee": {"type": "string", "description": "Комиссия (например '5%' или '2500₽')"},
            "method": {
                "type": "string",
                "enum": ["USDT_TRC20", "GUARANTOR"],
                "description": "Способ выплаты"
            },
        },
        "required": ["deal_id", "client_username", "fio", "bank", "amount", "fee", "method"],
    },
}

UPDATE_DEAL_STATUS_TOOL = {
    "name": "update_deal_status",
    "description": (
        "Меняет статус существующей сделки. Используй когда:\n"
        "- Админ говорит «Сделка X пополнена» -> status=ПОПОЛНЕНО\n"
        "- Сделка отпущена -> status=ЗАВЕРШЕНА\n\n"
        "Карточка ЛК в Группе 1 обновляется автоматически — отдельная "
        "публикация не нужна."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "deal_id": {"type": "string", "description": "ID сделки"},
            "new_status": {
                "type": "string",
                "enum": [
                    "ПОПОЛНИТЬ", "ОЖИДАЕТ_ПОПОЛНЕНИЯ", "ПОПОЛНЕНО",
                    "В_РАБОТЕ", "ГОТОВО_К_ОТПУСКУ", "ЗАВЕРШЕНА",
                    "ЗАБЛОКИРОВАН", "ОТМЕНА_СДЕЛКИ",
                ],
                "description": (
                    "Новый статус сделки. ЗАБЛОКИРОВАН — банк/счёт заблокировал "
                    "операцию, требует внимания оператора. ОТМЕНА_СДЕЛКИ — клиент "
                    "или мы отказались от сделки."
                ),
            },
        },
        "required": ["deal_id", "new_status"],
    },
}

FIND_DEAL_TOOL = {
    "name": "find_deal",
    "description": (
        "Ищет сделки в ДВУХ реестрах одновременно:\n"
        "  1) storage.deals — реестр сделок (с номером сделки)\n"
        "  2) storage.lk_cards — карточки ЛК (могут быть БЕЗ номера сделки,\n"
        "     например GUARANTOR_AFTER_WORK до отработки)\n"
        "AND-логика, case-insensitive substring.\n\n"
        "ВАЖНО: в ответе каждая запись содержит `source`:\n"
        "  - `deal` — это реальная сделка\n"
        "  - `lk_card` — это карточка ЛК (сделка уже принята в работу,\n"
        "    но может не иметь номера — это нормально)\n"
        "Если нашлась карточка (source=lk_card) — клиенту НЕЛЬЗЯ говорить «не "
        "найдена». Сделка УЖЕ в системе. Подтверди клиенту что нашёл, и при "
        "необходимости уточни только номер сделки (если payment_method этого "
        "требует и его реально нет).\n\n"
        "Используй когда:\n"
        "- Клиент спрашивает статус -> запроси у него ФИО + банк, потом найди\n"
        "- Из группы отработки пришёл [ФИО] — [БАНК] — ОТРАБОТАНО -> найди по fio + bank\n"
        "- Нужно проверить существует ли уже сделка с этим ID\n\n"
        "Возвращает список найденных записей (deals + lk_cards). "
        "Хотя бы один из параметров обязателен."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "deal_id": {"type": "string", "description": "Точный ID сделки"},
            "username": {"type": "string", "description": "Telegram username клиента БЕЗ @"},
            "fio": {"type": "string", "description": "ФИО (case-insensitive substring)"},
            "bank": {"type": "string", "description": "Банк (case-insensitive substring)"},
        },
    },
}

CREATE_LK_CARD_TOOL = {
    "name": "create_lk_card",
    "description": (
        "Создаёт анкету ЛК в Группе 1 «Личные кабинеты» для текущего "
        "клиента. ВЫЗЫВАЙ ТОЛЬКО когда:\n"
        "1) Перевяз ЛК подтверждён (сообщение «Перевяз ЛК выполнен» либо "
        "от @sys01/@sys02 в работ-чате).\n"
        "2) Все данные собраны: банк, ФИО держателя, цена, метод оплаты "
        "(USDT_TRC20 или гарант). Если USDT_TRC20 — нужен usdt_address. "
        "Если гарант — нужен deal_id (если уже есть).\n\n"
        "Если данных не хватает — НЕ вызывай. Спроси клиента и подожди ответ.\n"
        "После create_lk_card статус анкеты автоматом В_РАБОТЕ."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bank": {"type": "string", "description": "Банк (ОЗОН, Альфа, ...)"},
            "fio": {"type": "string", "description": "ФИО держателя счёта"},
            "price_usdt": {
                "type": "number",
                "description": "Цена ЛК в USDT (по прайсу или согласованная)",
            },
            "payment_method": {
                "type": "string",
                "enum": [
                    "USDT_TRC20",
                    "GUARANTOR_BEFORE",
                    "GUARANTOR_AFTER",
                    "GUARANTOR_AFTER_WORK",
                ],
                "description": (
                    "Метод оплаты: USDT_TRC20 — выплата в USDT после отработки. "
                    "GUARANTOR_BEFORE — сделка в конте ДО перевязки. "
                    "GUARANTOR_AFTER — сделка в конте ПОСЛЕ перевязки. "
                    "GUARANTOR_AFTER_WORK — сделка в конте ПОСЛЕ ОТРАБОТКИ "
                    "(юзербот сам инициирует диалог)."
                ),
            },
            "deal_id": {
                "type": "string",
                "description": "Номер гарант-сделки (если метод GUARANTOR_*)",
            },
            "usdt_address": {
                "type": "string",
                "description": "USDT TRC20 адрес (если метод USDT_TRC20)",
            },
        },
        "required": ["bank", "fio", "price_usdt", "payment_method"],
    },
}

SET_PAYMENT_METHOD_TOOL = {
    "name": "set_payment_method",
    "description": (
        "Сохраняет метод оплаты (USDT_TRC20 или GUARANTOR) для текущего "
        "клиента в его рабочей беседе. Если USDT_TRC20 — обязательно "
        "передай адрес кошелька (usdt_address), который дал клиент.\n\n"
        "Вызывай этот инструмент СРАЗУ как только клиент выбрал способ "
        "оплаты — это нужно чтобы при перевязке ЛК юзербот написал в "
        "чат «Отработка аккаунтов» правильный шаблон с адресом "
        "(\"Номер сделки: выплата на USDT TRC20: <адрес>\")."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["USDT_TRC20", "GUARANTOR"],
                "description": "Метод выплаты выбранный клиентом",
            },
            "usdt_address": {
                "type": "string",
                "description": (
                    "USDT TRC20 адрес клиента (только если method=USDT_TRC20). "
                    "TRX адрес начинается с T, длина ~34 символа."
                ),
            },
        },
        "required": ["method"],
    },
}

ALL_TOOLS = [
    PARTNER_TOOL,
    ESCALATE_TOOL,
    RECORD_DEAL_TOOL,
    UPDATE_DEAL_STATUS_TOOL,
    FIND_DEAL_TOOL,
    SET_PAYMENT_METHOD_TOOL,
    CREATE_LK_CARD_TOOL,
]

# Strip Obsidian-style [[wiki links]] for cleaner Claude context.
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

_client: Optional[AsyncAnthropic] = None


def _get_client() -> Optional[AsyncAnthropic]:
    """Lazily build the Anthropic client. Returns None if API key missing."""
    global _client
    if _client is not None:
        return _client
    if not config.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY is not set — AI replies disabled.")
        return None
    _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _load_knowledge() -> str:
    """Read all .md files from knowledge/ root (recursive 1 level, skipping _SKIP_NAMES).

    Returns concatenated text with '# === filename ===' markers.
    Loaded fresh on every call (cheap) so admin edits to knowledge/ apply
    immediately without restart.
    """
    if not KNOWLEDGE_DIR.exists():
        return ""
    parts = []
    for p in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        rel = p.relative_to(KNOWLEDGE_DIR)
        if rel.parts and rel.parts[0] in _SKIP_NAMES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("knowledge read failed for %s: %s", p, e)
            continue
        # Cleanup obsidian wiki links
        text = _WIKI_LINK_RE.sub(r"\1", text)
        parts.append(f"# === {rel.as_posix()} ===\n{text.strip()}")
    return "\n\n".join(parts)


def _build_system_prompt(brain_notes: str = "", client_context: Optional[dict] = None) -> str:
    """Compose system prompt from knowledge + admin brain-chat notes + client info."""
    knowledge = _load_knowledge()
    intro = (
        "Ты — ассистент компании PRIDE (поставки РС). Ты общаешься с клиентом "
        "в Telegram-беседе от имени сотрудника компании. Не упоминай, что ты AI. "
        "Отвечай на «вы», вежливо, по делу, кратко. Опирайся ТОЛЬКО на факты из "
        "приведённой ниже базы знаний — если ответа нет, скажи что уточнишь у "
        "менеджера. Не выдумывай цены, сроки и обещания. Один ответ — одно "
        "сообщение, без markdown-разметки.\n\n"
        "Если у тебя есть инструменты (tools) и сценарий из базы знаний требует "
        "действий — ВЫЗЫВАЙ инструмент, не пересказывай шаги словами.\n\n"

        "🔴 КРИТИЧЕСКИЙ КОНТЕКСТ ЧАТА:\n"
        "Этот чат — РАБОЧАЯ БЕСЕДА С КЛИЕНТОМ. Это НЕ внутренний чат команды, "
        "НЕ team chat, НЕ обсуждение прайса между сотрудниками. Несмотря на то "
        "что в чате есть @pride_sys01, @pride_sys02, @pride_manager1, @SIMBA_PRIDE_ADM, "
        "@TimonSkupCL — это сотрудники PRIDE которые работают с клиентом. Они НЕ "
        "клиенты. Клиент — это любой ДРУГОЙ участник (обычно тот чьё имя указано "
        "в блоке `# === ТЕКУЩИЙ КЛИЕНТ ===` ниже).\n\n"

        "🔴 ТОН — СТРОГАЯ КОНКРЕТИКА, БЕЗ ВОДЫ:\n"
        "PRIDE — солидная команда, не дети. Отвечай как профессионал по делу. "
        "ЗАПРЕЩЕНО:\n"
        "- «братан», «бля», «ахаха», «)))», «:)», «😂», «🤫», «🤣», «ой/ёлки», "
        "  смайлики настроения, эмодзи в ответ на эмодзи клиента;\n"
        "- рассуждения вслух о чате, о ситуации, «давайте разберёмся почему», "
        "  «понимаю вас», «как менеджер скажу», «в нашей компании принято»;\n"
        "- повторение вопроса клиента перед ответом — сразу ответ;\n"
        "- рекомендация «обратитесь к менеджеру» если ты сам можешь ответить "
        "  по базе. Менеджера зовём ТОЛЬКО когда правда не знаем;\n"
        "- лирика, философия, эмоциональные реакции, троллинг клиента.\n\n"

        "🔴 НИКОГДА не обращайся к работникам PRIDE в этом чате:\n"
        "Клиент видит каждое твоё сообщение. Обращение к @TimonSkupCL, "
        "@pride_sys01, @SIMBA_PRIDE_ADM и любому @pride-сотруднику в "
        "присутствии клиента — выглядит непрофессионально и подрывает доверие. "
        "ЗАПРЕЩЕНО писать:\n"
        "- «Тимон, посмотри чат», «Симба, подтверди», «sys01, помоги»;\n"
        "- «@TimonSkupCL действительно...», «коллеги, что скажете?»;\n"
        "- любое сообщение адресованное работнику команды.\n"
        "Если нужно эскалировать — просто скажи клиенту «уточню и вернусь» "
        "и МОЛЧИ. Работники PRIDE и так видят чат и подключатся сами.\n\n"

        "🔴 НИКОГДА не пиши клиенту фразы типа:\n"
        "- «это внутренний чат команды»\n"
        "- «не участвую в таких обсуждениях»\n"
        "- «моя роль — общаться с клиентами в их рабочих беседах» (ТЫ УЖЕ В НЕЙ)\n"
        "- «если у вас есть клиент в чате» (клиент = собеседник, не «у вас»)\n"
        "- «на какой вопрос от клиента мне ответить?» (его и отвечай!)\n\n"

        "🔴 РЕЖИМ MUTE — ПОЛНОЕ МОЛЧАНИЕ:\n"
        "Если в чате прозвучало «ассистент молчи», «ассистент стоп», "
        "«ассистент тихо», «не лезь», «помолчи» — ты выходишь из диалога "
        "ПОЛНОСТЬЮ. До тех пор пока кто-то прямо не напишет слово "
        "«Ассистент» с явным обращением (например «Ассистент продолжай», "
        "«Ассистент не молчи», «Ассистент, ответь по X») — твой ответ "
        "ВСЕГДА пустая строка. Никаких «🤫», «ок молчу», «понял», "
        "подтверждений, реакций на эмодзи/стикеры клиента, никаких "
        "подколов в ответ. Полная тишина = единственно допустимое "
        "поведение. Клиент может сколько угодно слать «🤫», стикеры, "
        "провокации — ты молчишь. Без префикса «Ассистент» в сообщении — "
        "не отвечаешь вообще.\n\n"

        "Если ты не знаешь как ответить — лучше **пустая строка** (полное молчание), "
        "менеджер ответит сам. Объяснять что ты молчишь — ЗАПРЕЩЕНО."
    )
    parts = [intro]
    # === KNOWLEDGE OVERRIDES (SIMBA: ОБНОВИ ПРАЙС / ПРАВИЛА ЗАБОРА ЛК) ===
    # Эти тексты задаёт работник вручную через специальную беседу. Они
    # ОБЯЗАТЕЛЬНЫ к исполнению — приоритетнее статической базы знаний.
    try:
        from storage import storage as _stg
        ov = _stg.get_knowledge_overrides()
        pricing_ov = (ov.get("pricing") or "").strip()
        rules_ov = (ov.get("lk_rules") or "").strip()
        if pricing_ov or rules_ov:
            ov_block = [
                "# === 🔴 ПРИОРИТЕТНЫЕ ДАННЫЕ (АКТУАЛЬНЫЙ ПРАЙС И ПРАВИЛА) ===\n"
                "Эти данные заданы вручную руководством PRIDE. Они ОБЯЗАТЕЛЬНЫ к "
                "исполнению и ПЕРЕОПРЕДЕЛЯЮТ статическую базу знаний ниже. "
                "Когда клиент спрашивает про цену или возможность взять банк — "
                "отвечай СТРОГО по этим текстам. Не предлагай альтернатив кроме "
                "указанных. Если правила запрещают банк без пары — так и говори "
                "клиенту (например «Урал берём ТОЛЬКО в паре с Точкой»)."
            ]
            if pricing_ov:
                ov_block.append("\n## АКТУАЛЬНЫЙ ПРАЙС ЛК:\n" + pricing_ov)
            if rules_ov:
                ov_block.append("\n## ПРАВИЛА ЗАБОРА ЛК:\n" + rules_ov)
            parts.append("\n".join(ov_block))
    except Exception as _ov_err:
        pass  # не валим prompt если storage недоступен
    if knowledge:
        parts.append("# === БАЗА ЗНАНИЙ ===\n" + knowledge)
    if brain_notes.strip():
        parts.append(
            "# === ДОПОЛНИТЕЛЬНЫЕ ЗНАНИЯ И ПРАВКИ АДМИНА (свежие) ===\n"
            + brain_notes.strip()
        )
    if client_context:
        cn = client_context.get("name") or "?"
        cu = client_context.get("username") or ""
        cid = client_context.get("id") or ""
        block = f"# === ТЕКУЩИЙ КЛИЕНТ ===\nИмя: {cn}\n"
        if cu:
            block += f"Username: @{cu}\n"
        if cid:
            block += f"Telegram ID: {cid}\n"
        block += (
            "Используй эти данные когда инструменту нужен username клиента "
            "(передавай БЕЗ @ префикса).\n"
        )
        # Память клиента: прошлые предпочтения по методу оплаты
        prev = client_context.get("prev_preferences") or {}
        if prev.get("payment_method"):
            pm = prev["payment_method"]
            addr = prev.get("usdt_address") or ""
            lk_count = int(prev.get("lk_count") or 0)
            pref_block = (
                "\n# === 🔴 ПАМЯТЬ КЛИЕНТА (КРИТИЧНО) ===\n"
                f"Этот клиент УЖЕ работал с нами ({lk_count} ЛК ранее). "
                f"Прошлый выбранный метод оплаты: **{pm}**"
            )
            if addr:
                pref_block += f"\nUSDT адрес сохранён: {addr}"
            pref_block += (
                "\n\n🔴 ПРАВИЛО: при новой перевязке/новом ЛК **НЕ спрашивай** "
                "клиента «USDT или гарант?» с нуля. Вместо этого сразу "
                "ПРЕДЛОЖИ прошлый метод и уточни только согласие:\n"
                f"   «Оставляем выплату как в прошлый раз — {pm}"
                f"{' на ' + addr if addr else ''}? Или хотите по-другому?»\n"
                "Если клиент согласен — фиксируй тот же метод через "
                "`set_payment_method`. Если хочет другой — спрашивай детали "
                "только нового метода."
            )
            block += pref_block
        parts.append(block)
    return "\n\n".join(parts)


# Локальная утилита для срезания markdown ```json ... ``` если есть
def _strip_code_fences(text: str) -> str:
    if not text:
        return text
    t = text.strip()
    if t.startswith("```"):
        # снять первую и последнюю строки если это fence
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t


# Паттерны «meta-молчания» — фразы которыми AI объясняет почему он молчит
# или ошибочно идентифицирует work_chat как internal team chat.
# Если ответ содержит ЛЮБОЙ из них — заменяем на пустую строку (молчание).
# Это safety-net поверх system prompt'а который и так это запрещает.
_META_SILENCE_PATTERNS = [
    "внутренний чат команды",
    "внутренний диалог команды",
    "это внутренний чат",
    "это внутренний диалог",
    "не участвую в таких обсуждени",
    "не участвую в обсужден",
    "не вмешиваюсь",
    "не вмешиваться в диалог",
    "молчу, не вмешив",
    "молчу. не вмешив",
    "обсуждение прайса и организационн",
    "моя роль — общаться с клиентами",
    "моя роль общаться с клиентами",
    "если у вас есть клиент в чате",
    "если у вас есть клиент, который",
    "на какой вопрос от клиента мне ответить",
    "в диалог не вступаю",
    "пропускаю ответ",
    "жду пока клиент закончит",
    "[молчу]",
    "(молчу)",
    "это сообщение с номером телефона — внутренний",
]


def _filter_meta_silence(text: str) -> str:
    """Если AI выдал meta-молчание (объяснение почему он не отвечает) —
    превращаем в пустую строку. См. knowledge/style.md «КРИТИЧЕСКОЕ ПРАВИЛО»."""
    if not text:
        return text
    t_lc = text.lower()
    for pat in _META_SILENCE_PATTERNS:
        if pat in t_lc:
            # match — это meta-silence. Возвращаем пустую строку.
            return ""
    return text


_RELEVANCE_SYSTEM = (
    "Ты — фильтр релевантности для PRIDE workchat-bot (Telegram-CRM для скупки "
    "банковских счетов).\n\n"
    "Задача: посмотри ПОСЛЕДНЕЕ сообщение клиента (в контексте 3 предыдущих) и "
    "решить — нужно ли AI-ассистенту ответить на него или лучше промолчать.\n\n"
    "ОТВЕЧАЕМ (respond), если:\n"
    "• Клиент задаёт вопрос (явный или подразумеваемый)\n"
    "• Клиент сообщает что-то новое по сделке/счёту/банку (ФИО, банк, цена, статус)\n"
    "• Клиент просит инструкцию, статус, помощь, выплату\n"
    "• Упоминает 'Ассистент' / 'ассистент' / '@ассистент'\n"
    "• Упоминает банк (Альфа, Озон, Точка, ТБанк, ВТБ, Уралсиб, Райффайзен, Локо, БКС, ДЕЛО, Убрир)\n"
    "• Упоминает деньги, USDT, гарант, сделку, выплату, ИП, счёт, р/с\n"
    "• Упоминает блок/брак/отказ/задержку\n"
    "• Жалоба, претензия, недоумение по бизнесу\n\n"
    "МОЛЧИМ (skip), если:\n"
    "• Шутки, мемы, эмодзи без смысла: 😂😂, лол, кек, хаха, ыыы\n"
    "• Болтовня не по делу: про погоду, политику, личное без связи со сделкой\n"
    "• Реакции на чужие сообщения: 'ага', 'понятно', 'ну да', 'жесть', 'воу', 'окей'\n"
    "• Просто соглашается с тем что уже было сказано — без нового вопроса\n"
    "• Очень короткие реплики которые не требуют ответа\n"
    "• Случайные сообщения, опечатки, бессмысленный набор букв\n\n"
    "Верни СТРОГО JSON одной строкой, без markdown-обёртки, без объяснений:\n"
    '{"action": "respond"} или {"action": "skip"}'
)


async def classify_relevance(
    last_messages: list[dict],
    model: Optional[str] = None,
) -> str:
    """Дешёвый классификатор: нужно ли AI отвечать на последнее сообщение?

    Args:
        last_messages: список {role, content} последних 3-5 сообщений.
                       Последнее обязательно от клиента.
        model: можно переопределить (по умолчанию Haiku 4.5).

    Returns:
        "respond" — нужен полноценный AI-ответ
        "skip"    — пропустить, не отвечать (экономия токенов)
        "respond" по умолчанию при любых ошибках (fail-safe — лучше ответить
        чем промолчать когда нужно).
    """
    cli = _get_client()
    if not cli:
        return "respond"
    mdl = model or "claude-haiku-4-5-20251001"
    # Берём только последние 4 сообщения чтобы экономить токены
    msgs = last_messages[-4:] if last_messages else []
    if not msgs:
        return "respond"
    # Конвертируем в простой текст для промпта (без tool-use, без caching —
    # классификатор зовётся один раз перед основным AI)
    convo_lines = []
    for m in msgs:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            # вытащим текст из tool-use блоков
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif isinstance(b, str):
                    parts.append(b)
            content = " ".join(parts)
        content = str(content or "").strip()
        if not content:
            continue
        # Обрезаем длинные сообщения (релевантность можно понять из первых 200 символов)
        if len(content) > 300:
            content = content[:300] + "..."
        convo_lines.append(f"[{role}] {content}")
    if not convo_lines:
        return "respond"
    convo_text = "\n".join(convo_lines)
    user_prompt = (
        f"Диалог (последние сообщения):\n\n{convo_text}\n\n"
        f"Нужно ли AI отвечать на последнее сообщение клиента? Верни JSON."
    )
    try:
        resp = await cli.messages.create(
            model=mdl,
            max_tokens=32,
            system=_RELEVANCE_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except APIError as e:
        logger.warning("classify_relevance API error: %s — fail-safe respond", e)
        return "respond"
    except Exception as e:
        logger.warning("classify_relevance unexpected error: %s — fail-safe respond", e)
        return "respond"
    raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
    raw = _strip_code_fences(raw.strip())
    # Простой regex поиск action
    m_skip = re.search(r'"action"\s*:\s*"skip"', raw, re.I)
    if m_skip:
        # Метрика
        try:
            usage = getattr(resp, "usage", None)
            if usage:
                in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                # Haiku 4.5: input $1/MTok, output $5/MTok
                cost = (in_tok * 1.0 + out_tok * 5.0) / 1_000_000
                logger.debug(
                    "classify_relevance: SKIP (in=%d out=%d ~$%.5f)",
                    in_tok, out_tok, cost,
                )
        except Exception:
            pass
        return "skip"
    return "respond"


async def generate_reply(
    history: list[dict],
    brain_notes: str = "",
    model: Optional[str] = None,
    tools_executor=None,
    client_context: Optional[dict] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """Call Claude. history must be a non-empty list of {role, content}, last from user.

    Если передан tools_executor (async callable(tool_name, tool_input) -> dict),
    AI получает доступ к ALL_TOOLS и может их вызывать. Делается tool-use loop:
    AI запрашивает инструмент → исполняем → возвращаем результат → AI пишет
    финальный текст. Без tools_executor — обычный текстовый режим.

    client_context: {"name": "...", "username": "...", "id": ...} для системного
    промпта — AI знает кто текущий клиент (нужно для tool параметров).

    Returns (text, usage) on success, (None, None) on failure.
    usage = {"input_tokens": total, "output_tokens": total} — суммировано
    по всем итерациям tool-use loop.
    """
    cli = _get_client()
    if cli is None:
        return None, None
    if not history:
        logger.warning("generate_reply called with empty history")
        return None, None

    system = _build_system_prompt(brain_notes, client_context=client_context)
    use_model = model or storage.get_ai_model() or config.DEFAULT_AI_MODEL

    # PROMPT CACHING: system prompt и tools повторяются на каждый запрос —
    # помечаем их cache_control: ephemeral, чтобы Anthropic кешировал
    # префикс на ~5 минут. Цена закешированного префикса — 10% от обычного.
    # Это снижает стоимость одного запроса с ~$0.26 до ~$0.03 при стабильном
    # knowledge размером 305KB / ~76K токенов.
    system_blocks = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    api_kwargs = {
        "model": use_model,
        "max_tokens": config.AI_MAX_TOKENS,
        "system": system_blocks,
        "messages": list(history),  # копия — будем мутировать в tool-use loop
    }
    if tools_executor is not None:
        # Tools тоже кешируем — schemas стабильны.
        cached_tools = []
        for i, t in enumerate(ALL_TOOLS):
            tool_copy = dict(t)
            # cache_control ставится только на ПОСЛЕДНИЙ tool в списке —
            # Anthropic кеширует весь блок tools при таком маркере.
            if i == len(ALL_TOOLS) - 1:
                tool_copy["cache_control"] = {"type": "ephemeral"}
            cached_tools.append(tool_copy)
        api_kwargs["tools"] = cached_tools

    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_write = 0
    # Защита от бесконечного цикла tool-use
    for iteration in range(5):
        try:
            msg = await cli.messages.create(**api_kwargs)
        except APIError as e:
            logger.warning("Anthropic API error (%s): %s", type(e).__name__, e)
            return None, None
        except Exception as e:
            logger.exception("Unexpected Claude call failure: %s", e)
            return None, None

        total_in += getattr(msg.usage, "input_tokens", 0)
        total_out += getattr(msg.usage, "output_tokens", 0)
        # Anthropic возвращает cache_creation_input_tokens (первая запись в кеш —
        # стоит 125% от обычной цены) и cache_read_input_tokens (чтение из кеша —
        # 10% от обычной). Считаем для видимости в логах.
        total_cache_read += getattr(msg.usage, "cache_read_input_tokens", 0) or 0
        total_cache_write += getattr(msg.usage, "cache_creation_input_tokens", 0) or 0

        if msg.stop_reason != "tool_use":
            # Финальный ответ — собираем text из блоков
            text_parts = []
            for block in msg.content:
                if hasattr(block, "text") and block.text:
                    text_parts.append(block.text)
            text = "".join(text_parts).strip()
            # SAFETY-NET: фильтр meta-молчаний (когда AI вместо ответа объясняет
            # почему он молчит / ошибочно думает что это team chat).
            # См. knowledge/style.md «КРИТИЧЕСКОЕ ПРАВИЛО».
            filtered = _filter_meta_silence(text)
            if text and not filtered:
                logger.warning(
                    "AI: meta-silence filter сработал, ответ заблокирован: %r",
                    text[:200],
                )
                return None, None
            text = filtered
            if not text:
                # Возможно AI ответил только tool_use'ом без текста — не баг,
                # но в чат отправлять нечего. Возвращаем пустой ответ как пропуск.
                return None, None
            if total_cache_read or total_cache_write:
                logger.info(
                    "AI usage: input=%d output=%d cache_read=%d cache_write=%d "
                    "(cache savings: %d токенов читали из кеша вместо $$$ inference)",
                    total_in, total_out, total_cache_read, total_cache_write,
                    total_cache_read,
                )
            return text, {
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": total_cache_read,
                "cache_creation_tokens": total_cache_write,
                "model": use_model,
            }

        # stop_reason == "tool_use" → исполняем все tool_use блоки в этом ответе
        if tools_executor is None:
            # AI попросил tool, но executor не задан — не должно случаться
            logger.warning("AI returned tool_use but no executor provided")
            return None, None
        tool_results = []
        for block in msg.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input or {}
            tool_id = block.id
            logger.info("AI tool call: %s(%s)", tool_name, tool_input)
            try:
                result = await tools_executor(tool_name, tool_input)
            except Exception as e:
                logger.exception("tool %s failed: %s", tool_name, e)
                result = {"status": "error", "error": str(e)}
            # tool_result content должно быть строкой или списком блоков
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": str(result),
            })

        # Прокидываем диалог дальше: assistant message + tool results
        api_kwargs["messages"].append({"role": "assistant", "content": msg.content})
        api_kwargs["messages"].append({"role": "user", "content": tool_results})

    logger.warning("generate_reply: tool-use loop hit 5 iterations limit")
    return None, {"input_tokens": total_in, "output_tokens": total_out}

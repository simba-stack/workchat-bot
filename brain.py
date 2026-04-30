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
        "(1) добавляет бота @PrideCONTROLE_bot в чат, "
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
        "3) Клиент полностью заполнил анкету в ЦРМ и она отправлена @PrideCONTROLE_bot — "
        "нужен @pride_sys01 для перевяза ЛК\n"
        "4) Ситуация требует решения человека (цены вне прайса, скидки, исключения)\n\n"
        "Кто за что:\n"
        "• TimonSkupCL — самые сложные вопросы, что не знают другие специалисты\n"
        "• pride_sys01 — ТОЛЬКО когда в чате появилось буквально «✔️ Отправлено "
        "на обработку» от @PrideCONTROLE_bot. НЕ путать с «Данные обновлены» — "
        "это промежуточный статус, на него @pride_sys01 НЕ зовётся.\n"
        "• pride_manager1 — рутинные вопросы по чату, ДО заполнения ЦРМ\n\n"
        "ЗАПРЕЩЕНО: эскалировать на ровном месте. Сначала попробуй ответить сам по базе "
        "знаний. Если ответ есть — отвечай сам, не зови. Эскалируй только когда реально "
        "нужен человек."
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

ALL_TOOLS = [PARTNER_TOOL, ESCALATE_TOOL]

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
        "действий — ВЫЗЫВАЙ инструмент, не пересказывай шаги словами."
    )
    parts = [intro]
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
            "(передавай БЕЗ @ префикса)."
        )
        parts.append(block)
    return "\n\n".join(parts)


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

    api_kwargs = {
        "model": use_model,
        "max_tokens": config.AI_MAX_TOKENS,
        "system": system,
        "messages": list(history),  # копия — будем мутировать в tool-use loop
    }
    if tools_executor is not None:
        api_kwargs["tools"] = ALL_TOOLS

    total_in = 0
    total_out = 0
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

        if msg.stop_reason != "tool_use":
            # Финальный ответ — собираем text из блоков
            text_parts = []
            for block in msg.content:
                if hasattr(block, "text") and block.text:
                    text_parts.append(block.text)
            text = "".join(text_parts).strip()
            if not text:
                # Возможно AI ответил только tool_use'ом без текста — не баг,
                # но в чат отправлять нечего. Возвращаем пустой ответ как пропуск.
                return None, None
            return text, {"input_tokens": total_in, "output_tokens": total_out}

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

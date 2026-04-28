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


def _build_system_prompt(brain_notes: str = "") -> str:
    """Compose system prompt from knowledge + admin brain-chat notes."""
    knowledge = _load_knowledge()
    intro = (
        "Ты — ассистент компании PRIDE (поставки РС). Ты общаешься с клиентом "
        "в Telegram-беседе от имени сотрудника компании. Не упоминай, что ты AI. "
        "Отвечай на «вы», вежливо, по делу, кратко. Опирайся ТОЛЬКО на факты из "
        "приведённой ниже базы знаний — если ответа нет, скажи что уточнишь у "
        "менеджера. Не выдумывай цены, сроки и обещания. Один ответ — одно "
        "сообщение, без markdown-разметки."
    )
    parts = [intro]
    if knowledge:
        parts.append("# === БАЗА ЗНАНИЙ ===\n" + knowledge)
    if brain_notes.strip():
        parts.append(
            "# === ДОПОЛНИТЕЛЬНЫЕ ЗНАНИЯ И ПРАВКИ АДМИНА (свежие) ===\n"
            + brain_notes.strip()
        )
    return "\n\n".join(parts)


async def generate_reply(
    history: list[dict],
    brain_notes: str = "",
    model: Optional[str] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """Call Claude. history must be a non-empty list of {role, content}, last from user.

    Returns (text, usage) on success, (None, None) on failure.
    usage = {"input_tokens": int, "output_tokens": int}
    """
    client = _get_client()
    if client is None:
        return None, None
    if not history:
        logger.warning("generate_reply called with empty history")
        return None, None

    system = _build_system_prompt(brain_notes)
    use_model = model or storage.get_ai_model() or config.DEFAULT_AI_MODEL

    try:
        msg = await client.messages.create(
            model=use_model,
            max_tokens=config.AI_MAX_TOKENS,
            system=system,
            messages=history,
        )
    except APIError as e:
        logger.warning("Anthropic API error (%s): %s", type(e).__name__, e)
        return None, None
    except Exception as e:
        logger.exception("Unexpected Claude call failure: %s", e)
        return None, None

    text = ""
    for block in msg.content:
        if hasattr(block, "text"):
            text += block.text
    text = text.strip()
    if not text:
        return None, None

    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0),
        "output_tokens": getattr(msg.usage, "output_tokens", 0),
    }
    return text, usage

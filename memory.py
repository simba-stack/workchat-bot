"""Knowledge graph writeback to GitHub.

When admin types a fact in brain_chat, this module:
  1. Asks Claude to classify the fact into a target knowledge/*.md file
     and produce a structured markdown block to append.
  2. Calls GitHub Contents API (using GITHUB_TOKEN) to PUT the new
     file content — append to existing or create new.
  3. Returns the commit URL so the admin sees confirmation in brain_chat.

Why GitHub Contents API and not git: container is python:3.12-slim with
no git installed, and we don't want to clone the repo for every commit.
The HTTP API gives us atomic file create/update with version SHA check.

Railway is configured (railway.json watchPatterns) to ignore changes
under knowledge/** so these commits do NOT trigger redeploys.
"""
import asyncio
import base64
import json
import logging
import re
from typing import Optional

import httpx
from anthropic import AsyncAnthropic

import config
from storage import storage

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Pre-defined knowledge files admin can target. Description hints Claude.
KNOWN_FILES = {
    "about.md": "О компании: что делает, ключевые факты, контакты",
    "pricing.md": "Цены, услуги, прайс, доставка, оплата, скидки",
    "faq.md": "Частые вопросы клиентов и готовые ответы",
    "policy.md": "Что AI НЕ должен говорить, ограничения, запреты",
    "style.md": "Тон общения, манера речи",
    "memories/auto.md": "Свободные заметки, не подпадающие под категории",
}


def _existing_knowledge_notes() -> list[str]:
    """Уникальные имена нот из knowledge/ для подсказок AI [[wiki-links]].

    Исключаем:
      - .obsidian/ (системные настройки)
      - memories/ (мета-ноты для дампа разговоров — не для семантических ссылок)
      - README (бессмысленно линковать, плюс часто дублируется в подпапках)
    Дедупликация по stem (имени без .md) — Obsidian резолвит [[name]] в один файл.
    Загружается на лету: после нового коммита в knowledge/ список обновится сам.
    """
    from pathlib import Path
    root = Path(__file__).parent / "knowledge"
    if not root.exists():
        return []
    seen = set()
    notes = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in {".obsidian", "memories"}:
            continue
        stem = p.stem
        if stem.lower() == "readme":
            continue
        if stem in seen:
            continue
        seen.add(stem)
        notes.append(stem)
    return notes

_anthropic: Optional[AsyncAnthropic] = None


def _get_anthropic() -> Optional[AsyncAnthropic]:
    global _anthropic
    if _anthropic is None and config.ANTHROPIC_API_KEY:
        _anthropic = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic


def _classifier_prompt(text: str) -> str:
    files = "\n".join(f"- {n}: {d}" for n, d in KNOWN_FILES.items())
    notes = _existing_knowledge_notes()
    notes_str = ", ".join(f"[[{n}]]" for n in notes) if notes else "(пока пусто)"
    return (
        "Ты — librarian для базы знаний компании в Obsidian-vault. "
        "Админ присылает сообщение в обучающий чат. Задача: решить, "
        "сохранять ли это в knowledge, и если да — в какой файл и как.\n\n"
        f"Доступные файлы:\n{files}\n\n"
        f"Существующие ноты в графе: {notes_str}\n\n"
        "═══ ВСЕГДА СОХРАНЯЙ (status ok) — приоритет выше всего ═══\n\n"
        "1) ЯВНЫЕ КОМАНДЫ ОБУЧЕНИЯ (admin-trigger words). Если сообщение "
        "начинается с любого из этих слов или содержит их — это сигнал "
        "обучения, сохраняй ВСЁ что после команды:\n"
        "  • «запомни ...», «ЗАПОМНИ ...», «запиши ...», «сохрани ...», "
        "«зафиксируй ...», «учти что ...»\n"
        "  • «на такой вопрос отвечай ...», «когда спрашивают X — отвечай Y»\n"
        "  • «клиентам говори ...», «отвечай клиентам что ...»\n"
        "  • «у нас правило ...», «всегда ...», «никогда ...»\n"
        "  • «новое условие ...», «изменилось ...»\n"
        "Эти триггеры НИКОГДА не пропускаются, даже если содержание короткое.\n\n"
        "2) ДЕКЛАРАТИВНЫЕ ФАКТЫ о компании / процессе / услуге. Примеры:\n"
        "  • «Минимальный заказ — 10 000 ₽»\n"
        "  • «Доставка по Москве: 500 ₽, 1-2 дня»\n"
        "  • «Менеджер по поставкам — Алексей, @username»\n"
        "  • «Работаем пн-пт с 9:00 до 18:00»\n"
        "  • «Наш депозит — 5555$» — да, это факт о компании, сохраняй\n"
        "  • «Холд на средства — 1-3 дня»\n\n"
        "3) ИНСТРУКЦИИ ПО ПОВЕДЕНИЮ AI (style/policy/процедуры) — это "
        "тоже знания, сохраняй в style.md или policy.md.\n\n"
        "═══ SKIP только если сообщение очевидно мусорное ═══\n\n"
        "  • Чистые вопросы БЕЗ ответа в этом же сообщении («сколько?», "
        "«как тут это работает?»). Вопрос С ответом сохраняй («сколько холд? — "
        "1-3 дня» = сохранять).\n"
        "  • Приветствия БЕЗ контента: «привет», «ты тут», «ау», «тест», «ок», "
        "«угу», «попробую», «ща», «сек», «хм», «1», «👍»\n"
        "  • Эмоции без факта: «класс», «огонь», «лол», «🤔»\n"
        "  • Команды отмены/удаления: «забудь», «удали последнее», «не сохраняй»\n"
        "  • Чистая болтовня без полезной информации\n\n"
        "Если это явный teaching-trigger или декларативный факт — НЕ ПРОПУСКАЙ. "
        "Лучше сохранить лишнее, чем потерять знание. Админ пишет в обучающий "
        "чат не просто так.\n\n"
        "Формат skip: {\"skip\": true, \"reason\": \"короткое объяснение\"}\n"
        "Формат успеха: {\"file\": \"имя.md\", \"content\": \"markdown-блок\", "
        "\"commit_message\": \"ai-knowledge: краткое описание\"}\n"
        "  • content начинается с h2/h3 заголовка (## или ###), потом 1-3 строки\n"
        "  • file — одно из перечисленных или новое 'lowercase-name.md' (латиница)\n\n"
        "ОБЯЗАТЕЛЬНО — wiki-links для графа знаний:\n"
        "В content в конце добавь строку «Связано: [[нота1]] [[нота2]]» с 1-3 "
        "ссылками на существующие ноты которые семантически связаны. Если факт "
        "уникален — добавь хотя бы [[index]]. НЕ ссылайся на ноту в которую "
        "сейчас сохраняешь.\n\n"
        "ТОЛЬКО валидный JSON, никакого пояснительного текста до или после!\n\n"
        f"Сообщение админа: {text!r}"
    )


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # remove ```json or ``` and trailing ```
        s = re.sub(r"^```(?:json)?\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


async def classify_fact(text: str) -> Optional[dict]:
    """Returns {file, content, commit_message} or None if skipped/failed."""
    cli = _get_anthropic()
    if not cli:
        logger.warning("classify_fact: no Anthropic client (API key missing)")
        return None
    model = storage.get_ai_model() or config.DEFAULT_AI_MODEL
    # Длинный мануал может потребовать большой output (markdown-блок + commit msg).
    # 600 раньше обрезало JSON и парсер падал — поднимаем до 4096.
    try:
        msg = await cli.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": _classifier_prompt(text)}],
        )
    except Exception as e:
        logger.warning("classify_fact API error (text_len=%d): %s", len(text), e)
        return None
    raw = "".join(b.text for b in msg.content if hasattr(b, "text"))
    raw = _strip_code_fences(raw)
    stop = getattr(msg, "stop_reason", None)
    if stop == "max_tokens":
        logger.warning(
            "classify_fact: hit max_tokens limit (text_len=%d, raw_len=%d) — JSON может быть обрезан",
            len(text), len(raw),
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            "classify_fact JSON parse failed: %s | stop=%s raw_head=%r raw_tail=%r",
            e, stop, raw[:300], raw[-300:],
        )
        return None
    if data.get("skip"):
        logger.info("classify_fact: skipped — %s", data.get("reason"))
        return None
    file = (data.get("file") or "").lstrip("/").strip()
    content = (data.get("content") or "").strip()
    if not file or not content:
        return None
    if not file.endswith(".md"):
        file += ".md"
    return {
        "file": file,
        "content": content,
        "commit_message": data.get("commit_message", f"ai-knowledge: update {file}"),
    }


async def _gh_get(client: httpx.AsyncClient, path: str, branch: str, headers: dict):
    """Returns (sha, decoded_content) if file exists, (None, '') if 404."""
    url = f"{GITHUB_API}/repos/{config.GITHUB_REPO_OWNER}/{config.GITHUB_REPO_NAME}/contents/{path}"
    try:
        r = await client.get(url, headers=headers, params={"ref": branch})
    except Exception as e:
        logger.warning("github GET error: %s", e)
        return None, None  # signals network failure
    if r.status_code == 404:
        return None, ""  # file doesn't exist yet
    if r.status_code != 200:
        logger.warning("github GET %s -> %d: %s", path, r.status_code, r.text[:200])
        return None, None
    payload = r.json()
    sha = payload.get("sha")
    raw = payload.get("content") or ""
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except Exception:
        decoded = ""
    return sha, decoded


async def _gh_put(
    client: httpx.AsyncClient,
    path: str,
    content: str,
    commit_msg: str,
    branch: str,
    headers: dict,
    sha: Optional[str] = None,
) -> Optional[str]:
    """Returns commit URL on success, None on failure."""
    url = f"{GITHUB_API}/repos/{config.GITHUB_REPO_OWNER}/{config.GITHUB_REPO_NAME}/contents/{path}"
    body = {
        "message": commit_msg[:200],
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    try:
        r = await client.put(url, headers=headers, json=body)
    except Exception as e:
        logger.warning("github PUT error: %s", e)
        return None
    if r.status_code in (200, 201):
        commit = r.json().get("commit") or {}
        return commit.get("html_url") or ""
    logger.warning("github PUT %s -> %d: %s", path, r.status_code, r.text[:300])
    return None


async def commit_to_knowledge(
    file: str, append_block: str, commit_msg: str
) -> Optional[str]:
    """Append a markdown block to knowledge/<file> on GitHub. Creates if missing.

    Returns commit html_url on success, None on failure.
    Retries once on 409 conflict (stale SHA).
    """
    if not config.GITHUB_TOKEN:
        logger.warning("commit_to_knowledge: GITHUB_TOKEN not set")
        return None
    branch = config.GITHUB_BRANCH
    repo_path = f"{config.KNOWLEDGE_SUBDIR}/{file}"
    headers = {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "workchat-bot-memory/1.0",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        for attempt in (1, 2):
            sha, existing = await _gh_get(client, repo_path, branch, headers)
            if existing is None:
                return None  # network/api failure (not 404)
            # Compose new content: existing + separator + new block
            if existing and not existing.endswith("\n"):
                existing += "\n"
            new_content = (existing + "\n" + append_block.strip() + "\n") if existing else append_block.strip() + "\n"
            url = await _gh_put(
                client, repo_path, new_content, commit_msg, branch, headers, sha=sha
            )
            if url is not None:
                return url
            # Retry once on transient failure (likely SHA conflict)
            logger.info("commit_to_knowledge retry attempt %d", attempt)
            await asyncio.sleep(0.5)
    return None


async def process_brain_chat_message(text: str) -> dict:
    """End-to-end pipeline: classify → commit. Always returns a dict for logging.

    Possible outcomes:
      {"status": "skipped"}                 — Claude said this isn't a fact
      {"status": "no_token"}                — GITHUB_TOKEN missing in env
      {"status": "classify_fail"}           — Claude returned bad output
      {"status": "commit_fail", "file":...} — couldn't push to GitHub
      {"status": "ok", "file":..., "url":..., "preview":...} — success
    """
    if not config.GITHUB_TOKEN:
        return {"status": "no_token"}
    cls = await classify_fact(text)
    if cls is None:
        return {"status": "skipped"}
    url = await commit_to_knowledge(
        file=cls["file"],
        append_block=cls["content"],
        commit_msg=cls["commit_message"],
    )
    if not url:
        return {"status": "commit_fail", "file": cls["file"]}
    preview = cls["content"][:300]
    if len(cls["content"]) > 300:
        preview += "…"
    return {"status": "ok", "file": cls["file"], "url": url, "preview": preview}

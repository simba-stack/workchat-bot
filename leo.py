"""LEO — умный AI-агент дашборда.

Принимает свободные команды/вопросы пользователя дашборда, имеет:
  • Полный доступ к knowledge graph (knowledge/*.md)
  • Снимок текущего состояния системы (карточки, сделки, операторы)
  • Tool-use для выполнения действий через юзербот (рассылки, аудиты,
    смены статусов, поиск, отчёты и т.д.)

API:
  ask(user_text) -> dict {reply, actions[], plain_text}
  где actions = [{tool, args}] — список действий которые LEO предлагает
  выполнить. Если actions пусто — это просто ответ-сообщение.

Архитектура: одношаговый tool-use через Anthropic Claude. LEO решает что
делать, возвращает либо текст для пользователя, либо tool_use на одну из
зарегистрированных команд. Команды затем кладутся в storage.dashboard_commands
и подхватываются userbot._dashboard_command_worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from anthropic import AsyncAnthropic

import config
from storage import storage

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# Дешёвая, быстрая модель для Leo. Можно переопределить через env.
LEO_MODEL = os.getenv("LEO_MODEL", "claude-haiku-4-5-20251001")
LEO_MAX_TOKENS = int(os.getenv("LEO_MAX_TOKENS", "1024"))

_client: Optional[AsyncAnthropic] = None


def _get_client() -> Optional[AsyncAnthropic]:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            return None
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ===== Tools (действия которые Leo может выполнять) =====

LEO_TOOLS = [
    {
        "name": "broadcast_workchats",
        "description": (
            "Отправить рассылку во ВСЕ work-чаты клиентов (через юзербот). "
            "Используй когда пользователь просит сообщить что-то всем клиентам/"
            "всем поставщикам/всем рабочим беседам."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "broadcast_bot_users",
        "description": (
            "Отправить рассылку ВСЕМ пользователям бота (всем кто нажимал /start). "
            "Через aiogram бот."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "broadcast_inactive",
        "description": (
            "Рассылка ТОЛЬКО тем кто нажимал /start но НЕ зашёл в свою work-беседу. "
            "Используй для напоминаний/реактивации."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "pause_ai",
        "description": "Отключить AI глобально (юзербот не будет отвечать клиентам).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "resume_ai",
        "description": "Включить AI глобально.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_margin",
        "description": "Очистить заявки V2 и сбросить маржу до $0.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_ai_stats",
        "description": "Сбросить счётчики AI (replies, errors, эскалации).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "sync_lk_cards",
        "description": (
            "Просканировать историю сообщений Группы 1 ЛК и восстановить "
            "отсутствующие карточки в storage. Используй после потери state.json "
            "или если карточки на дашборде кажутся неполными."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Сколько последних сообщений сканировать (50-3000).",
                    "default": 500,
                },
            },
        },
    },
    {
        "name": "list_pricing",
        "description": "Показать текущий прайс ЛК.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_pricing",
        "description": "Установить/обновить цену банка в прайсе ЛК.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bank": {"type": "string"},
                "price_usdt": {"type": "number"},
            },
            "required": ["bank", "price_usdt"],
        },
    },
    {
        "name": "audit_system",
        "description": (
            "Аудит — выявить аномалии в системе: карточки в БЛОК слишком долго, "
            "клиенты без действий, AI errors, etc. Возвращает структурированный "
            "отчёт."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_card",
        "description": "Найти карточки ЛК по любым полям (банк, ФИО, supplier, статус).",
        "input_schema": {
            "type": "object",
            "properties": {
                "bank": {"type": "string"},
                "fio": {"type": "string"},
                "supplier": {"type": "string"},
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "change_lk_status",
        "description": "Сменить статус карточки ЛК.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "new_status": {
                    "type": "string",
                    "enum": [
                        "В_РАБОТЕ", "ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
                        "ЗАВЕРШЁН", "БРАК", "БЛОК", "БЛОК_БЕЗ_ОТРАБОТКИ",
                    ],
                },
            },
            "required": ["card_id", "new_status"],
        },
    },
    {
        "name": "delete_lk_card",
        "description": "Удалить карточку ЛК (необратимо). Требует подтверждения.",
        "input_schema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "daily_report",
        "description": (
            "Сводный отчёт за сегодня: маржа, новые карточки, эскалации, активность."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_report",
        "description": "Стата по конкретному оператору/работнику за всё время.",
        "input_schema": {
            "type": "object",
            "properties": {"username": {"type": "string"}},
            "required": ["username"],
        },
    },
    {
        "name": "show_state",
        "description": (
            "Полный снимок состояния системы (для аудита). Замен для большой "
            "команды 'stats' с подробной разбивкой."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


_KNOWLEDGE_CACHE = {"text": "", "loaded_ts": 0.0}


def _load_knowledge_summary(max_chars: int = 6000) -> str:
    """Загружает knowledge graph. Кеш 5 минут чтоб не читать файлы каждый раз.
    Урезано с 20K до 6K — Haiku не нужно всё, релевантные куски достаточны.
    """
    import time as _t
    now = _t.time()
    if _KNOWLEDGE_CACHE["text"] and (now - _KNOWLEDGE_CACHE["loaded_ts"]) < 300:
        return _KNOWLEDGE_CACHE["text"]
    if not KNOWLEDGE_DIR.exists():
        return ""
    # Приоритет — pricing, deals, faq (бизнес-критичные), потом остальные
    priority_files = ["pricing.md", "deals.md", "faq.md", "policy.md"]
    paths = list(KNOWLEDGE_DIR.rglob("*.md"))
    paths.sort(key=lambda p: (0 if p.name in priority_files else 1, str(p)))
    parts = []
    total = 0
    for p in paths:
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = p.relative_to(KNOWLEDGE_DIR)
        block = f"\n# {rel}\n{txt.strip()[:1800]}\n"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    result = "".join(parts)
    _KNOWLEDGE_CACHE["text"] = result
    _KNOWLEDGE_CACHE["loaded_ts"] = now
    return result


_SNAPSHOT_CACHE = {"data": None, "ts": 0.0}


def _build_state_snapshot_cached() -> dict:
    """Кеш снапшота на 30 сек — экономим reload_sync на каждый запрос."""
    import time as _t
    now = _t.time()
    if _SNAPSHOT_CACHE["data"] is not None and (now - _SNAPSHOT_CACHE["ts"]) < 30:
        return _SNAPSHOT_CACHE["data"]
    data = _build_state_snapshot()
    _SNAPSHOT_CACHE["data"] = data
    _SNAPSHOT_CACHE["ts"] = now
    return data


def _build_state_snapshot() -> dict:
    """Краткий снимок состояния — даём Leo контекст что сейчас в системе."""
    cards = storage.list_lk_cards() or {}
    managed = storage.state.get("managed_chats") or {}
    deals = storage.list_deals() or {}
    ai_stats = storage.state.get("ai_stats") or {}
    cards_by_status: dict = {}
    cards_by_bank: dict = {}
    for c in cards.values():
        st = c.get("status") or "—"
        bk = c.get("bank") or "—"
        cards_by_status[st] = cards_by_status.get(st, 0) + 1
        cards_by_bank[bk] = cards_by_bank.get(bk, 0) + 1
    return {
        "lk_cards_total": len(cards),
        "lk_by_status": cards_by_status,
        "lk_by_bank": cards_by_bank,
        "managed_chats": len(managed),
        "deals_total": len(deals),
        "ai_replies": ai_stats.get("replies_total", 0),
        "ai_errors": ai_stats.get("errors_total", 0),
        "bot_users": len(storage.list_bot_users() or {}),
        "pricing": dict(storage.state.get("pricing") or {}),
    }


def _system_prompt(state_snapshot: dict, knowledge: str) -> str:
    return (
        "Ты — LEO, голосовой AI-агент управления операционным центром PRIDE.\n"
        "PRIDE — компания по выкупу российских расчётных счетов ИП/ООО, оплата "
        "клиентам в USDT TRC20 либо через гарант-сделку Conte.\n\n"
        "Тебя зовёт админ (SIMBA) через дашборд J.A.R.V.I.S. — он задаёт вопросы "
        "или даёт команды на свободном языке. Ты:\n"
        "  1) Отвечаешь кратко и по делу (1-3 предложения).\n"
        "  2) Если нужно выполнить действие — вызываешь подходящий tool. "
        "Можешь вызвать НЕСКОЛЬКО tools если задача комплексная.\n"
        "  3) Используешь knowledge graph для бизнес-вопросов (цены, банки, "
        "процессы).\n"
        "  4) Использовать snapshot для оперативных вопросов (сколько ЛК, "
        "какая маржа, кто из операторов активен).\n"
        "  5) НЕ задавай уточняющих вопросов если можешь разумно догадаться. "
        "Действуй сразу.\n"
        "  6) Если команда деструктивная (delete, broadcast) — выполняй, но "
        "напиши в ответе чётко что делаешь.\n\n"
        f"=== ТЕКУЩЕЕ СОСТОЯНИЕ ===\n{json.dumps(state_snapshot, ensure_ascii=False, indent=2)}\n\n"
        f"=== KNOWLEDGE GRAPH ===\n{knowledge}\n"
    )


async def ask(user_text: str) -> dict:
    """Главный вход: задать вопрос/команду Льву.
    Возвращает {reply: str, actions: [{tool, args}], usage: {...}}.
    """
    cli = _get_client()
    if cli is None:
        return {
            "reply": "ANTHROPIC_API_KEY не задан — Лев молчит.",
            "actions": [],
            "usage": {},
        }
    user_text = (user_text or "").strip()
    if not user_text:
        return {"reply": "Пустой запрос.", "actions": [], "usage": {}}

    # Кешируем — не дёргаем reload каждый запрос
    state_snap = _build_state_snapshot_cached()
    knowledge = _load_knowledge_summary()
    system = _system_prompt(state_snap, knowledge)

    try:
        resp = await cli.messages.create(
            model=LEO_MODEL,
            max_tokens=LEO_MAX_TOKENS,
            system=system,
            tools=LEO_TOOLS,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        logger.warning("LEO API call failed: %s", e)
        return {
            "reply": f"⚠️ Ошибка обращения к Claude: {e}",
            "actions": [],
            "usage": {},
        }

    text_parts = []
    actions: list = []
    for block in (resp.content or []):
        btype = getattr(block, "type", "")
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "tool_use":
            actions.append({
                "tool": getattr(block, "name", ""),
                "args": dict(getattr(block, "input", {}) or {}),
            })
    reply = "\n".join(p.strip() for p in text_parts if p.strip())
    if not reply and actions:
        reply = f"⚙ Выполняю: {actions[0]['tool']}"
    usage = {
        "input_tokens": getattr(resp.usage, "input_tokens", 0),
        "output_tokens": getattr(resp.usage, "output_tokens", 0),
    }
    return {"reply": reply, "actions": actions, "usage": usage}


def tool_to_command_text(tool: str, args: dict) -> Optional[str]:
    """Конвертирует tool_use Leo в текст команды для
    userbot._execute_dashboard_command (чтоб переиспользовать существующий
    исполнитель команд).
    Возвращает строку команды или None если tool не известен."""
    if tool == "broadcast_workchats":
        return f"рассылка работчатам: {args.get('message', '')}"
    if tool == "broadcast_bot_users":
        return f"рассылка боту: {args.get('message', '')}"
    if tool == "broadcast_inactive":
        return f"рассылка незарегистрированным: {args.get('message', '')}"
    if tool == "pause_ai":
        return "pause ai"
    if tool == "resume_ai":
        return "resume ai"
    if tool == "clear_margin":
        return "очисти маржу"
    if tool == "clear_ai_stats":
        return "очисти ai"
    if tool == "sync_lk_cards":
        lim = int(args.get("limit") or 500)
        return f"/sync_lk {lim}"
    if tool == "list_pricing":
        return "прайс показать"
    if tool == "set_pricing":
        return f"прайс {args.get('bank', '')} {args.get('price_usdt', 0)}"
    if tool == "show_state":
        return "stats"
    if tool == "audit_system":
        return "/audit"
    if tool == "find_card":
        bank = args.get("bank") or ""
        fio = args.get("fio") or ""
        return f"/find_card {bank} {fio}".strip()
    if tool == "change_lk_status":
        return f"#{args.get('card_id', '')} статус {args.get('new_status', '')}"
    if tool == "delete_lk_card":
        return f"удалить #{args.get('card_id', '')}"
    if tool == "daily_report":
        return "/daily_report"
    if tool == "operator_report":
        return f"/operator {args.get('username', '')}"
    return None

"""Bulk-обучение AI из истории Telegram-чатов.

Использование (в брейн-чате):
  /learn                     — все managed_chats, limit 200 пар на чат
  /learn 12345               — конкретный чат
  /learn limit=500           — изменить лимит на чат
  /learn 12345 limit=500     — конкретный чат + лимит

Что делает:
1. Читает последние N сообщений в чате через iter_messages.
2. Группирует пары «клиент-вопрос → менеджер-ответ» (workers из storage).
3. Каждую пару пропускает через memory.process_brain_chat_message —
   Claude classifier решает сохранять ли факт в knowledge.
4. Сохранение через GitHub Contents API (как обычный writeback).

Throttling: 0.5с между парами чтобы не упереться в Anthropic rate limit.
"""
import asyncio
import logging
from typing import Optional

import config
import memory
from storage import storage

logger = logging.getLogger(__name__)


async def fetch_history(client, chat_id, limit: int = 500) -> list:
    """Возвращает список сообщений в хронологическом порядке.

    Каждое сообщение: {ts, sender_id, sender_username, is_worker, text}.
    Только текстовые сообщения. AI-логи (с маркером [AI-LOG]) пропускаются.
    """
    messages = []
    workers_lc = {w.lower() for w in storage.get_workers()}
    admins = set(storage.get_admins() or [])
    me = await client.get_me()
    self_id = getattr(me, "id", 0)

    try:
        async for m in client.iter_messages(chat_id, limit=limit):
            if not m.text:
                continue
            txt = m.text.strip()
            if not txt:
                continue
            if txt.startswith("[AI-LOG]"):
                continue
            try:
                sender = await m.get_sender()
            except Exception:
                sender = None
            sender_username = (getattr(sender, "username", "") or "").lower()
            sender_id = m.sender_id
            # «Worker» = в whitelist storage.get_workers() ИЛИ admin ИЛИ сам юзербот
            is_worker = (
                (sender_username and sender_username in workers_lc)
                or (sender_id in admins)
                or (sender_id == self_id)
            )
            messages.append({
                "ts": m.date.timestamp() if m.date else 0,
                "sender_id": sender_id,
                "sender_username": sender_username,
                "is_worker": is_worker,
                "text": txt,
            })
    except Exception as e:
        logger.warning("fetch_history chat=%s failed: %s", chat_id, e)
        return []

    messages.reverse()  # iter_messages даёт от свежих к старым
    return messages


def extract_qa_pairs(messages: list, max_lookahead: int = 5) -> list:
    """Группирует client_message → следующий worker_reply.

    max_lookahead: сколько сообщений вперёд просматривать в поисках ответа.
    Если до ответа было больше — пропускаем (вероятно нет связи).
    """
    pairs = []
    for i, m in enumerate(messages):
        if m["is_worker"]:
            continue
        # Поиск ближайшего worker-ответа
        for j in range(i + 1, min(i + 1 + max_lookahead, len(messages))):
            n = messages[j]
            if n["is_worker"]:
                pairs.append({
                    "q_ts": m["ts"],
                    "a_ts": n["ts"],
                    "q": m["text"],
                    "a": n["text"],
                    "worker": n["sender_username"],
                })
                break
    return pairs


def _format_pair_for_classifier(q: str, a: str) -> str:
    """Формат который понимает memory.classify_fact (через teaching-trigger)."""
    # Используем тег «запомни» — classify_fact ВСЕГДА сохраняет такие факты.
    # Так Claude classifier фокусируется на содержимом ответа менеджера.
    return (
        f"запомни как отвечает наш менеджер на такие вопросы:\n\n"
        f"Вопрос клиента: «{q[:600]}»\n"
        f"Ответ менеджера: «{a[:600]}»\n\n"
        f"Если в ответе есть полезное знание — сохрани его. "
        f"Если ответ односложный («да», «привет», «ок») — пропусти."
    )


async def learn_from_chat(
    client, chat_id, limit: int = 200, throttle: float = 0.5,
    progress_callback=None,
) -> dict:
    """Главная функция: читает чат, извлекает пары, классифицирует, сохраняет.

    progress_callback(stats) вызывается каждые 10 пар (если задан).
    Возвращает {chat_id, processed, saved, skipped, errors, pairs_count}.
    """
    msgs = await fetch_history(client, chat_id, limit=limit)
    pairs = extract_qa_pairs(msgs)

    stats = {
        "chat_id": chat_id,
        "messages": len(msgs),
        "pairs_count": len(pairs),
        "processed": 0,
        "saved": 0,
        "skipped": 0,
        "errors": 0,
    }

    if not pairs:
        return stats

    for idx, p in enumerate(pairs, 1):
        try:
            text = _format_pair_for_classifier(p["q"], p["a"])
            result = await memory.process_brain_chat_message(text)
            stats["processed"] += 1
            status = result.get("status") if isinstance(result, dict) else None
            if status == "ok":
                stats["saved"] += 1
            elif status == "skipped":
                stats["skipped"] += 1
            else:
                stats["errors"] += 1
        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "learn pair failed (chat=%s, idx=%d): %s", chat_id, idx, e
            )

        # Прогресс
        if progress_callback and idx % 10 == 0:
            try:
                await progress_callback(stats)
            except Exception:
                pass

        # Throttle (Anthropic + GitHub rate limits)
        if throttle:
            await asyncio.sleep(throttle)

    return stats


async def learn_from_all_chats(
    client, limit_per_chat: int = 200, throttle: float = 0.5,
    progress_callback=None,
) -> dict:
    """Проходит по всем managed_chats и обучается на каждом.

    progress_callback(chat_stats, overall) — после каждого чата.
    """
    chat_ids = storage.get_managed_chat_ids() or []
    overall = {
        "chats_total": len(chat_ids),
        "chats_processed": 0,
        "messages": 0,
        "pairs_count": 0,
        "processed": 0,
        "saved": 0,
        "skipped": 0,
        "errors": 0,
    }
    for cid in chat_ids:
        # iter_messages принимает signed/unsigned/-100 — нормализация на стороне Telethon
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            continue
        try:
            chat_stats = await learn_from_chat(
                client, cid_int, limit=limit_per_chat, throttle=throttle,
            )
            overall["chats_processed"] += 1
            for k in ("messages", "pairs_count", "processed",
                      "saved", "skipped", "errors"):
                overall[k] += chat_stats.get(k, 0)
            if progress_callback:
                try:
                    await progress_callback(chat_stats, overall)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("learn_from_all_chats: chat=%s failed: %s", cid, e)
            overall["errors"] += 1
    return overall


def parse_learn_command(text: str) -> dict:
    """Парсит «/learn [chat_id] [limit=N]».

    Возвращает {chat_id: int|None, limit: int}.
    """
    parts = (text or "").strip().split()
    chat_id = None
    limit = 200
    for p in parts[1:]:
        if p.startswith("limit="):
            try:
                limit = int(p[6:])
            except ValueError:
                pass
        else:
            try:
                chat_id = int(p)
            except ValueError:
                pass
    # Лимит: разумные границы
    limit = max(10, min(2000, limit))
    return {"chat_id": chat_id, "limit": limit}


def format_stats_short(stats: dict) -> str:
    """Краткая сводка в одну строку."""
    return (
        f"обработано {stats.get('processed', 0)}/{stats.get('pairs_count', 0)}, "
        f"сохранено {stats.get('saved', 0)}, "
        f"пропущено {stats.get('skipped', 0)}, "
        f"ошибок {stats.get('errors', 0)}"
    )

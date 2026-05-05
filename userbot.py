"""Userbot service: создаёт супергруппы, приглашает работников, шлёт welcome при входе клиента.

Welcome delivery has two channels:
1. Realtime: events.ChatAction handler (fires when Telegram pushes us the join event)
2. Fallback: a per-chat polling task that polls participants every 3s for up to 10 min,
   sends welcome as soon as the expected client_id appears.

Race condition protection: each chat has its own asyncio.Lock (_welcome_locks).
Only the first coroutine to acquire the lock will actually send the message;
the second will see welcome_sent=True and exit immediately.
"""
import logging
import asyncio
import random
import re
import time

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    EditAdminRequest,
    GetParticipantRequest,
)
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import (
    ChatAdminRights, PeerUser,
    MessageEntityBold, MessageEntityItalic, MessageEntityUnderline,
    MessageEntityStrike, MessageEntityCode, MessageEntityPre,
    MessageEntityBlockquote, MessageEntityTextUrl, MessageEntityCustomEmoji,
)
from telethon.errors import (
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    FloodWaitError,
    PeerFloodError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
)
try:
    from telethon.errors import UserAlreadyParticipantError  # type: ignore
except ImportError:  # старые версии
    class UserAlreadyParticipantError(Exception):
        pass

import config
from storage import storage
import brain
import memory
import accounting

logger = logging.getLogger(__name__)


def _split_text(text: str, limit: int = 3900) -> list:
    """Split long text into chunks <= limit. Tries to break on newlines/spaces."""
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def _entities_to_telethon(items: list) -> list:
    """Convert aiogram-style entity dicts -> Telethon entities. Unknown types skipped."""
    out = []
    for e in items or []:
        try:
            t = e.get("type"); off = int(e["offset"]); ln = int(e["length"])
        except Exception:
            continue
        if t == "custom_emoji":
            cid = e.get("custom_emoji_id") or e.get("customEmojiId")
            if cid:
                out.append(MessageEntityCustomEmoji(off, ln, int(cid)))
        elif t == "bold":
            out.append(MessageEntityBold(off, ln))
        elif t == "italic":
            out.append(MessageEntityItalic(off, ln))
        elif t == "underline":
            out.append(MessageEntityUnderline(off, ln))
        elif t == "strikethrough":
            out.append(MessageEntityStrike(off, ln))
        elif t == "code":
            out.append(MessageEntityCode(off, ln))
        elif t == "pre":
            lang = e.get("language") or ""
            out.append(MessageEntityPre(off, ln, lang))
        elif t in ("blockquote", "expandable_blockquote"):
            out.append(MessageEntityBlockquote(off, ln))
        elif t == "text_link":
            url = e.get("url") or ""
            if url:
                out.append(MessageEntityTextUrl(off, ln, url))
    return out


class UserbotService:
    def __init__(self):
        if config.STRING_SESSION:
            session = StringSession(config.STRING_SESSION)
        else:
            session = "userbot_session"
        self.client = TelegramClient(session, config.API_ID, config.API_HASH)
        self._me = None
        self._welcome_locks: dict[int, asyncio.Lock] = {}
        self._ai_locks: dict[int, asyncio.Lock] = {}
        self._last_worker_ts: dict[str, float] = {}
        # Время последнего сообщения от клиента в managed-чате (нормализованный
        # chat_id -> unix time). Используется в _handle_ai_message чтобы
        # понимать «отвечал ли worker на ПРЕДЫДУЩЕЕ сообщение клиента».
        self._last_client_msg_ts: dict[str, float] = {}
        self._chat_entity_cache: dict[int, object] = {}

    def _get_welcome_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._welcome_locks:
            self._welcome_locks[chat_id] = asyncio.Lock()
        return self._welcome_locks[chat_id]

    async def start(self):
        await self.client.start(phone=config.USERBOT_PHONE)
        self._me = await self.client.get_me()
        logger.info(
            "Userbot started: %s (@%s, id=%s)",
            self._me.first_name, self._me.username, self._me.id,
        )
        for label, cid in (
            ("brain_chat", storage.get_brain_chat_id()),
            ("coord_chat", storage.get_coordination_chat_id()),
        ):
            if not cid:
                continue
            try:
                ent = await self.client.get_entity(cid)
                self._chat_entity_cache[int(cid)] = ent
                logger.info("%s entity primed: id=%s type=%s", label, cid, type(ent).__name__)
            except Exception as e:
                logger.warning(
                    "%s entity prime FAILED for id=%s: %s — sends в этот чат могут падать с InvalidPeer",
                    label, cid, e,
                )

        try:
            count = 0
            async for dialog in self.client.iter_dialogs(limit=30):
                ent = dialog.entity
                etype = type(ent).__name__
                title = getattr(ent, "title", None) or getattr(ent, "first_name", "") or "?"
                show_id = dialog.id
                logger.info(
                    "DIALOG[%d]: chat_id=%s title=%r type=%s",
                    count, show_id, title[:60], etype,
                )
                count += 1
            logger.info("DIALOG: listed %d chats", count)
        except Exception as e:
            logger.warning("dialog listing failed: %s", e)

        @self.client.on(events.ChatAction)
        async def _on_chat_action(event):
            try:
                await self._handle_chat_action(event)
            except Exception as e:
                logger.warning("ChatAction handler error: %s", e)

        @self.client.on(events.NewMessage(incoming=True))
        async def _on_new_message(event):
            try:
                await self._handle_ai_message(event)
            except Exception as e:
                logger.exception("AI message handler error: %s", e)

    async def _handle_chat_action(self, event):
        try:
            uid = getattr(event, "user_id", None)
            logger.info(
                "ChatAction received: chat_id=%s user_id=%s joined=%s added=%s",
                event.chat_id, uid, event.user_joined, event.user_added,
            )
        except Exception:
            pass

        if not (event.user_joined or event.user_added):
            return
        info = storage.get_chat_info(event.chat_id)
        if not info:
            logger.info("ChatAction: chat=%s not in managed_chats — skip", event.chat_id)
            return
        if info.get("welcome_sent"):
            logger.info("ChatAction: chat=%s welcome already sent — skip", event.chat_id)
            return
        expected = info.get("client_id")
        if not expected:
            return

        joining_ids = set()
        if getattr(event, "user_id", None):
            joining_ids.add(event.user_id)
        try:
            users = await event.get_users()
            for u in users or []:
                joining_ids.add(getattr(u, "id", None))
        except Exception:
            pass

        if expected not in joining_ids:
            logger.info(
                "ChatAction: chat=%s expected=%s joining=%s — skip",
                event.chat_id, expected, joining_ids,
            )
            return

        await self._send_welcome(event.chat_id, expected, source="event")

    async def _send_welcome(self, chat_id, expected_client_id: int, source: str = "?"):
        lock = self._get_welcome_lock(int(chat_id))
        async with lock:
            info = storage.get_chat_info(chat_id)
            if not info or info.get("welcome_sent"):
                return False
            await asyncio.sleep(1)
            info = storage.get_chat_info(chat_id)
            if not info or info.get("welcome_sent"):
                return False

            welcome = storage.get_welcome()
            entities_raw = storage.get_welcome_entities()
            try:
                if entities_raw:
                    ents = _entities_to_telethon(entities_raw)
                    await self.client.send_message(chat_id, welcome, formatting_entities=ents)
                else:
                    for chunk in _split_text(welcome, 3900):
                        await self.client.send_message(chat_id, chunk)
                        await asyncio.sleep(0.3)
                await storage.mark_welcome_sent(chat_id)
                logger.info(
                    "Welcome sent (source=%s, entities=%d, len=%d) to chat=%s for client=%s",
                    source, len(entities_raw), len(welcome), chat_id, expected_client_id,
                )
                return True
            except FloodWaitError as e:
                logger.warning(
                    "Welcome send flood wait %ds (source=%s, chat=%s) — retrying after wait",
                    e.seconds, source, chat_id,
                )
                await asyncio.sleep(e.seconds + 1)
                try:
                    await self.client.send_message(chat_id, welcome[:3900])
                    await storage.mark_welcome_sent(chat_id)
                    logger.info("Welcome sent after flood wait (chat=%s)", chat_id)
                    return True
                except Exception as retry_e:
                    logger.warning("Welcome retry failed (chat=%s): %s", chat_id, retry_e)
                    return False
            except Exception as e:
                logger.warning(
                    "Welcome send failed (source=%s, chat=%s): %s",
                    source, chat_id, e,
                )
                return False

    async def _watch_for_client_join(self, channel, client_id: int, timeout_sec: int = 600):
        deadline = asyncio.get_event_loop().time() + timeout_sec
        try:
            while asyncio.get_event_loop().time() < deadline:
                info = storage.get_chat_info(channel.id)
                if info and info.get("welcome_sent"):
                    logger.info("watch chat=%s: welcome already sent, exiting watcher", channel.id)
                    return
                try:
                    await self.client(GetParticipantRequest(
                        channel=channel,
                        participant=PeerUser(client_id),
                    ))
                    logger.info("watch chat=%s: client %s joined, sending welcome", channel.id, client_id)
                    await self._send_welcome(channel.id, client_id, source="poll")
                    return
                except UserNotParticipantError:
                    pass
                except FloodWaitError as e:
                    logger.warning("watch chat=%s flood wait %ss", channel.id, e.seconds)
                    await asyncio.sleep(e.seconds + 1)
                    continue
                except Exception as e:
                    logger.warning("watch chat=%s poll error: %s", channel.id, e)
                await asyncio.sleep(3)
            logger.info("watch chat=%s: timeout (%ss), client never joined", channel.id, timeout_sec)
        except Exception as e:
            logger.warning("watch chat=%s: unexpected error: %s", channel.id, e)

    async def _resolve_chat_target(self, chat_id):
        try:
            cid_int = int(chat_id)
        except Exception:
            return chat_id
        if cid_int in self._chat_entity_cache:
            return self._chat_entity_cache[cid_int]
        try:
            ent = await self.client.get_entity(cid_int)
            self._chat_entity_cache[cid_int] = ent
            return ent
        except Exception as e:
            logger.warning("resolve_chat_target failed for %s: %s", cid_int, e)
            return cid_int

    # === AI brain handlers ===

    async def _handle_ai_message(self, event):
        chat_id = event.chat_id
        bid = storage.get_brain_chat_id()

        from storage import _norm_chat_id

        try:
            sender_id_dbg = event.sender_id
        except Exception:
            sender_id_dbg = "?"
        logger.info(
            "userbot event: chat_id=%s norm=%s brain_id=%s norm_brain=%s sender=%s",
            chat_id, _norm_chat_id(chat_id), bid,
            (_norm_chat_id(bid) if bid else "—"), sender_id_dbg,
        )

        if bid and _norm_chat_id(chat_id) == _norm_chat_id(bid):
            await self._handle_brain_chat_writeback(event)
            return

        deals_id = storage.get_deals_group_id()
        if deals_id and _norm_chat_id(chat_id) == _norm_chat_id(deals_id):
            await self._handle_deals_group_message(event)
            return

        accounts_id = storage.get_accounts_group_id()
        if accounts_id and _norm_chat_id(chat_id) == _norm_chat_id(accounts_id):
            await self._handle_accounts_group_message(event)
            return

        accounting_id = storage.get_accounting_group_id()
        if accounting_id and _norm_chat_id(chat_id) == _norm_chat_id(accounting_id):
            await self._handle_accounting_group_message(event)
            return

        chat_info = storage.get_chat_info(chat_id)
        if not chat_info:
            return
        if not event.message or not (event.message.text or "").strip():
            return

        if await self._maybe_handle_perevyaz(event, chat_info):
            return

        sender_id = event.sender_id
        if self._me and sender_id == self._me.id:
            return

        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        sender_username = (getattr(sender, "username", "") or "").lower()
        workers_lc = {w.lower() for w in storage.get_workers()}
        is_worker = (
            (sender_username and sender_username in workers_lc)
            or (sender_id in storage.get_admins())
        )

        from storage import _norm_chat_id  # noqa: F811
        chat_key = _norm_chat_id(chat_id)

        if is_worker:
            self._last_worker_ts[chat_key] = time.time()
            logger.info("AI: worker activity in chat=%s by @%s", chat_id, sender_username)
            return

        if not storage.is_ai_enabled():
            return
        if not config.ANTHROPIC_API_KEY:
            return

        client_id = chat_info.get("client_id")
        if client_id and sender_id != client_id:
            return

        idle_min = max(0, storage.get_client_idle_minutes())
        idle_sec = idle_min * 60
        # Логика «не вмешиваться в живой диалог»:
        # - last_worker_ts: когда worker последний раз писал в этом чате
        # - prev_client_ts: когда клиент писал ДО текущего сообщения
        # - now: текущее сообщение клиента
        # AI молчит ТОЛЬКО если worker ответил на предыдущее сообщение клиента
        # (worker_ts > prev_client_ts) И этот ответ был недавно (< idle_sec).
        # Если worker не отписался после предыдущего сообщения клиента — AI
        # подхватывает СРАЗУ, не дожидаясь idle_sec.
        prev_client_ts = self._last_client_msg_ts.get(chat_key, 0)
        last_worker_ts = self._last_worker_ts.get(chat_key, 0)
        worker_replied_to_prev = last_worker_ts > prev_client_ts
        # Обновляем штамп клиента (после фиксации prev_client_ts).
        self._last_client_msg_ts[chat_key] = time.time()

        if idle_sec > 0 and worker_replied_to_prev and time.time() - last_worker_ts < idle_sec:
            await storage.bump_ai_stats(skipped_worker_active=1)
            logger.info(
                "AI: skip chat=%s — worker active (replied to prev client msg) in last %dm",
                chat_id, idle_min,
            )
            return
        if not worker_replied_to_prev and last_worker_ts > 0:
            logger.info(
                "AI: chat=%s — worker НЕ ответил на предыдущее сообщение клиента, AI подхватывает",
                chat_id,
            )

        lock = self._ai_locks.setdefault(chat_key, asyncio.Lock())
        if lock.locked():
            logger.info("AI: chat=%s already processing — skip", chat_id)
            return
        async with lock:
            await self._do_ai_reply(event, chat_info, idle_sec, chat_key)

    async def _handle_learn_command(self, event, text: str):
        """Bulk-обучение из истории чатов. /learn [chat_id] [limit=N]."""
        cmd = learn.parse_learn_command(text)
        chat_id = cmd["chat_id"]
        limit = cmd["limit"]

        if not config.ANTHROPIC_API_KEY:
            await event.reply("⚠️ ANTHROPIC_API_KEY не задан — обучение невозможно.")
            return
        if not config.GITHUB_TOKEN:
            await event.reply("⚠️ GITHUB_TOKEN не задан — нечего сохранять.")
            return

        if chat_id:
            await event.reply(
                f"📚 Обучение: chat_id={chat_id}, limit={limit}.\n"
                f"Это может занять несколько минут."
            )
            asyncio.create_task(self._learn_task(event, chat_id, limit))
        else:
            chats = storage.get_managed_chat_ids() or []
            await event.reply(
                f"📚 Обучение из {len(chats)} managed-чатов, "
                f"limit={limit} пар на чат.\nОтчёт по завершении."
            )
            asyncio.create_task(self._learn_all_task(event, limit))

    async def _learn_task(self, event, chat_id, limit):
        try:
            stats = await learn.learn_from_chat(self.client, chat_id, limit=limit)
            text = (
                f"✅ chat={chat_id} завершён.\n"
                f"Сообщений: {stats.get('messages', 0)}, "
                f"пар: {stats.get('pairs_count', 0)}\n"
                f"{learn.format_stats_short(stats)}"
            )
            await event.reply(text)
        except Exception as e:
            logger.exception("learn_task failed for chat=%s", chat_id)
            try:
                await event.reply(f"⚠️ Ошибка: {e}")
            except Exception:
                pass

    async def _learn_all_task(self, event, limit):
        try:
            overall = await learn.learn_from_all_chats(self.client, limit_per_chat=limit)
            text = (
                f"✅ Обучение завершено: {overall['chats_processed']}/"
                f"{overall['chats_total']} чатов.\n"
                f"Сообщений: {overall['messages']}, "
                f"пар: {overall['pairs_count']}, "
                f"обработано: {overall['processed']}\n"
                f"💎 Сохранено: <b>{overall['saved']}</b> | "
                f"пропущено: {overall['skipped']} | "
                f"ошибок: {overall['errors']}"
            )
            await event.reply(text, parse_mode="html")
        except Exception as e:
            logger.exception("learn_all_task failed")
            try:
                await event.reply(f"⚠️ Ошибка: {e}")
            except Exception:
                pass

    async def _handle_brain_chat_writeback(self, event):
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return
        if text.startswith("[AI-LOG]"):
            return
        # /learn — bulk-обучение из истории чатов
        if text.lower().startswith("/learn"):
            await self._handle_learn_command(event, text)
            return
        if text.startswith("/"):
            return
        if not storage.is_writeback_enabled():
            return
        if not config.ANTHROPIC_API_KEY:
            return

        logger.info("brain writeback: processing %d chars from chat=%s", len(text), event.chat_id)
        try:
            result = await memory.process_brain_chat_message(text)
        except Exception as e:
            logger.exception("brain writeback unexpected error: %s", e)
            await storage.bump_writeback_stats(errors=1)
            return

        status = result.get("status")
        if status == "ok":
            await storage.bump_writeback_stats(commits=1)
            url = result.get("url") or ""
            file = result.get("file")
            preview = result.get("preview", "")
            reply = (
                f"✅ Сохранено в `knowledge/{file}`\n"
                f"Commit: {url}\n\n"
                f"```\n{preview}\n```"
            )
            try:
                await event.reply(reply, link_preview=False)
            except Exception as e:
                logger.warning("brain writeback ack failed: %s", e)
        elif status == "skipped":
            await storage.bump_writeback_stats(skipped=1)
            try:
                await event.reply("📝 Принял к сведению, но это не похоже на факт для сохранения.")
            except Exception:
                pass
        elif status == "commit_fail":
            await storage.bump_writeback_stats(errors=1)
            file = result.get("file") or "?"
            try:
                await event.reply(
                    f"⚠️ Не смог закоммитить в `knowledge/{file}` "
                    f"(GitHub API). Проверь GITHUB_TOKEN и логи."
                )
            except Exception:
                pass
        elif status == "no_token":
            try:
                await event.reply("⚠️ GITHUB_TOKEN не задан в env — writeback в граф невозможен.")
            except Exception:
                pass
        elif status == "classify_fail":
            await storage.bump_writeback_stats(errors=1)
            try:
                await event.reply("⚠️ Claude не смог разобрать сообщение в JSON.")
            except Exception:
                pass

    async def _do_ai_reply(self, event, chat_info: dict, idle_sec: int, chat_key: str):
        chat_id = event.chat_id

        delay = random.uniform(config.AI_TYPING_DELAY_MIN, config.AI_TYPING_DELAY_MAX)
        try:
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(delay)
        except Exception:
            await asyncio.sleep(delay)

        # Повторная проверка: если worker написал пока мы «печатали» — отступаем,
        # но только если его ответ свежий (last_worker_ts > last_client_msg_ts).
        last_worker_ts = self._last_worker_ts.get(chat_key, 0)
        last_client_ts = self._last_client_msg_ts.get(chat_key, 0)
        if (
            idle_sec > 0
            and last_worker_ts > last_client_ts
            and time.time() - last_worker_ts < idle_sec
        ):
            await storage.bump_ai_stats(skipped_worker_active=1)
            logger.info("AI: chat=%s — worker came in during typing delay, skip", chat_id)
            return

        client_id = chat_info.get("client_id") or 0
        try:
            history = await self._fetch_history_for_claude(chat_id, client_id)
        except Exception as e:
            logger.warning("AI: history fetch failed for chat=%s: %s", chat_id, e)
            history = []
        if not history or history[-1]["role"] != "user":
            logger.info("AI: chat=%s — empty/invalid history", chat_id)
            return

        brain_notes = await self._fetch_brain_notes()

        client_username = None
        if client_id:
            try:
                client_entity = await self.client.get_entity(client_id)
                client_username = getattr(client_entity, "username", None)
            except Exception as e:
                logger.warning("client entity resolve failed: %s", e)
        client_context = {
            "id": client_id,
            "name": chat_info.get("client_name") or "",
            "username": client_username or "",
        }

        last_msg_id = getattr(getattr(event, "message", None), "id", None)
        async def _executor(name, inp):
            return await self._execute_ai_tool(name, inp, chat_id=chat_id, last_msg_id=last_msg_id)

        async with self.client.action(chat_id, "typing"):
            reply, usage = await brain.generate_reply(
                history,
                brain_notes=brain_notes,
                tools_executor=_executor,
                client_context=client_context,
            )
        if reply is None:
            await storage.bump_ai_stats(errors=1)
            logger.warning("AI: chat=%s — claude returned None", chat_id)
            return

        try:
            for chunk in _split_text(reply, 3900):
                await self.client.send_message(chat_id, chunk)
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning("AI: send failed chat=%s: %s", chat_id, e)
            await storage.bump_ai_stats(errors=1)
            return

        await storage.bump_ai_stats(
            replies=1,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        logger.info(
            "AI: replied chat=%s in=%s out=%s",
            chat_id, usage.get("input_tokens"), usage.get("output_tokens"),
        )

        client_text = (event.message.text or "").strip()
        await self._log_to_brain(
            chat_id=chat_id,
            chat_info=chat_info,
            client_text=client_text,
            ai_text=reply,
            usage=usage,
        )

    async def _fetch_history_for_claude(self, chat_id, client_id: int) -> list[dict]:
        msgs: list[dict] = []
        try:
            async for m in self.client.iter_messages(chat_id, limit=config.AI_HISTORY_LIMIT):
                txt = (m.text or "").strip()
                if not txt:
                    continue
                if self._me and m.sender_id == self._me.id:
                    msgs.insert(0, {"role": "assistant", "content": txt})
                elif client_id and m.sender_id == client_id:
                    msgs.insert(0, {"role": "user", "content": txt})
                else:
                    try:
                        s = await m.get_sender()
                    except Exception:
                        s = None
                    name = getattr(s, "first_name", None) or "Сотрудник"
                    msgs.insert(0, {"role": "user", "content": f"[{name}]: {txt}"})
        except Exception as e:
            logger.warning("history iter failed for chat=%s: %s", chat_id, e)
            return []

        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        while msgs and msgs[-1]["role"] != "user":
            msgs.pop()
        return msgs

    async def _fetch_brain_notes(self) -> str:
        bid = storage.get_brain_chat_id()
        if not bid:
            return ""
        parts: list[str] = []
        try:
            async for m in self.client.iter_messages(bid, limit=config.AI_BRAIN_NOTES_LIMIT):
                txt = (m.text or "").strip()
                if not txt:
                    continue
                if txt.startswith("[AI-LOG]"):
                    continue
                ts = m.date.strftime("%Y-%m-%d %H:%M") if m.date else ""
                parts.insert(0, f"[{ts}] {txt}")
        except Exception as e:
            logger.warning("brain notes fetch failed: %s", e)
            return ""
        return "\n".join(parts)

    async def _log_to_brain(self, chat_id, chat_info: dict, client_text: str, ai_text: str, usage: dict):
        bid = storage.get_brain_chat_id()
        if not bid:
            return
        client_name = chat_info.get("client_name") or "—"
        in_t = usage.get("input_tokens", 0)
        out_t = usage.get("output_tokens", 0)
        ct = client_text if len(client_text) <= 600 else client_text[:600] + "…"
        at = ai_text if len(ai_text) <= 1500 else ai_text[:1500] + "…"
        log_msg = (
            f"[AI-LOG] 💬 {client_name}\n"
            f"chat_id={chat_id}, tokens in={in_t} out={out_t}\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 {ct}\n\n"
            f"🤖 {at}"
        )
        try:
            target = await self._resolve_chat_target(bid)
            await self.client.send_message(target, log_msg)
        except Exception as e:
            logger.warning("brain log send failed: %s", e)

    # === AI tool-use ===
    CRM_BOT_USERNAME = "PrideCONTROLE_bot"

    async def _execute_ai_tool(self, tool_name: str, tool_input: dict, chat_id, last_msg_id=None) -> dict:
        logger.info(
            "AI tool exec: %s input=%s chat=%s msg=%s",
            tool_name, tool_input, chat_id, last_msg_id,
        )
        if tool_name == "add_partner_to_crm":
            return await self._tool_add_partner_to_crm(
                chat_id=chat_id,
                client_username=tool_input.get("client_username", ""),
            )
        if tool_name == "escalate_to_team":
            return await self._tool_escalate_to_team(
                work_chat_id=chat_id,
                last_msg_id=last_msg_id,
                specialist=tool_input.get("specialist", ""),
                reason=tool_input.get("reason", ""),
                client_question=tool_input.get("client_question", ""),
            )
        if tool_name == "record_deal":
            return await self._tool_record_deal(work_chat_id=chat_id, **tool_input)
        if tool_name == "update_deal_status":
            return await self._tool_update_deal_status(**tool_input)
        if tool_name == "find_deal":
            return await self._tool_find_deal(**tool_input)
        if tool_name == "post_deals_group":
            return await self._tool_post_deals_group(**tool_input)
        return {"status": "error", "error": f"unknown_tool:{tool_name}"}

    async def _tool_add_partner_to_crm(self, chat_id, client_username: str) -> dict:
        username = (client_username or "").lstrip("@").strip()
        if not username:
            return {"status": "error", "error": "client_username_empty"}

        try:
            bot_entity = await self.client.get_entity(self.CRM_BOT_USERNAME)
        except UsernameNotOccupiedError:
            return {"status": "error", "step": "resolve", "error": "crm_bot_not_found"}
        except Exception as e:
            return {"status": "error", "step": "resolve", "error": str(e)}

        try:
            await self.client(InviteToChannelRequest(chat_id, [bot_entity]))
            invite_status = "added"
        except UserAlreadyParticipantError:
            invite_status = "already_in_chat"
        except UserPrivacyRestrictedError:
            return {"status": "error", "step": "invite", "error": "privacy_restricted"}
        except FloodWaitError as e:
            return {"status": "error", "step": "invite", "error": f"flood_wait_{e.seconds}s"}
        except Exception as e:
            logger.warning("CRM invite warning: %s", e)
            invite_status = f"warn:{type(e).__name__}"

        try:
            rights = ChatAdminRights(
                change_info=False, post_messages=False, edit_messages=False,
                delete_messages=True, ban_users=False, invite_users=True,
                pin_messages=False, add_admins=False, anonymous=False, manage_call=False,
            )
            await self.client(EditAdminRequest(
                channel=chat_id, user_id=bot_entity, admin_rights=rights, rank="CRM",
            ))
            admin_status = "granted"
        except Exception as e:
            logger.warning("CRM admin grant non-fatal: %s", e)
            admin_status = f"skipped:{type(e).__name__}"

        try:
            await self.client.send_message(chat_id, f"+партнер @{username}")
        except FloodWaitError as e:
            return {"status": "error", "step": "command", "error": f"flood_wait_{e.seconds}s"}
        except Exception as e:
            return {"status": "error", "step": "command", "error": str(e)}

        return {
            "status": "ok",
            "invite": invite_status,
            "admin": admin_status,
            "command_sent": f"+партнер @{username}",
        }

    async def _tool_escalate_to_team(self, work_chat_id, last_msg_id, specialist: str, reason: str, client_question: str) -> dict:
        coord_id = storage.get_coordination_chat_id()
        if not coord_id:
            return {"status": "error", "error": "coordination_chat_not_set"}

        allowed = {"TimonSkupCL", "pride_sys01", "pride_manager1"}
        spec = (specialist or "").lstrip("@").strip()
        if spec not in allowed:
            return {"status": "error", "error": f"unknown_specialist:{spec}"}

        from storage import _norm_chat_id
        bare = _norm_chat_id(work_chat_id)
        chat_link = f"https://t.me/c/{bare}"
        msg_link = f"{chat_link}/{last_msg_id}" if last_msg_id else chat_link

        info = storage.get_chat_info(work_chat_id) or {}
        client_name = info.get("client_name") or "—"

        text = (
            f"🆘 @{spec}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>Причина:</b> {reason}\n\n"
            f"<b>Клиент:</b> {client_name}\n"
            f"<b>Вопрос клиента:</b>\n«{client_question}»\n\n"
            f"📎 Сообщение: {msg_link}\n"
            f"💬 Чат: {chat_link}"
        )
        try:
            target = await self._resolve_chat_target(coord_id)
            await self.client.send_message(target, text, parse_mode="html", link_preview=False)
        except Exception as e:
            await storage.bump_escalate_stats(error=True)
            logger.warning("escalate send failed: %s", e)
            return {"status": "error", "step": "send", "error": str(e)}

        await storage.bump_escalate_stats(specialist=spec)
        logger.info("AI escalated to @%s in coord_chat=%s", spec, coord_id)
        return {"status": "ok", "specialist": spec, "coord_chat": coord_id, "msg_link": msg_link}

    # === Tools для системы учёта сделок ===

    async def _tool_record_deal(
        self,
        deal_id: str = "",
        client_username: str = "",
        fio: str = "",
        bank: str = "",
        amount: str = "",
        fee: str = "",
        method: str = "",
        work_chat_id=None,
    ) -> dict:
        deal_id = (deal_id or "").strip()
        if not deal_id:
            return {"status": "error", "error": "deal_id_empty"}
        if storage.get_deal(deal_id):
            return {"status": "error", "error": "deal_already_exists", "deal_id": deal_id}
        ok = await storage.add_deal(
            deal_id=deal_id,
            client_username=client_username,
            fio=fio,
            bank=bank,
            amount=amount,
            fee=fee,
            method=method,
            status="ПОПОЛНИТЬ",
            work_chat_id=work_chat_id,
        )
        if not ok:
            return {"status": "error", "error": "add_failed"}
        logger.info("deal recorded: %s | @%s | %s | %s", deal_id, client_username, bank, amount)

        # Если перевяз случился ДО record_deal — accounts_group уже содержит
        # пост с «Номер сделки: выплата после отработки» (или USDT_TRC20).
        # Подменяем deal_id на актуальный, обновляем статус.
        if work_chat_id is not None:
            pending_msg_id = await storage.pop_pending_accounts_post(work_chat_id)
            if pending_msg_id:
                await storage.set_accounts_group_msg_id(deal_id, pending_msg_id)
                await self._refresh_accounts_post(deal_id)
                logger.info(
                    "record_deal: linked PENDING accounts_msg=%s to deal=%s",
                    pending_msg_id, deal_id,
                )

        return {"status": "ok", "deal_id": deal_id, "initial_status": "ПОПОЛНИТЬ"}

    async def _tool_update_deal_status(self, deal_id: str = "", new_status: str = "") -> dict:
        ok = await storage.update_deal_status(deal_id, new_status)
        if not ok:
            return {"status": "error", "error": "deal_not_found_or_invalid", "deal_id": deal_id}
        d = storage.get_deal(deal_id) or {}
        logger.info("deal status updated: %s -> %s", deal_id, new_status)
        return {
            "status": "ok",
            "deal_id": deal_id,
            "new_status": new_status,
            "client_username": d.get("client_username"),
            "fio": d.get("fio"),
            "bank": d.get("bank"),
        }

    async def _tool_find_deal(self, deal_id: str = "", username: str = "", fio: str = "", bank: str = "") -> dict:
        if not any([deal_id, username, fio, bank]):
            return {"status": "error", "error": "no_query_params"}
        results = storage.find_deal_by(
            deal_id=deal_id or None,
            username=username or None,
            fio=fio or None,
            bank=bank or None,
        )
        clean = []
        for d in results:
            cd = {k: v for k, v in d.items() if k not in ("history", "created_at")}
            clean.append(cd)
        return {"status": "ok", "found": len(clean), "deals": clean}

    async def _tool_post_deals_group(self, deal_id: str = "", custom_text: str = "", **_kwargs) -> dict:
        gid = storage.get_deals_group_id()
        if not gid:
            return {"status": "error", "error": "deals_group_not_set"}

        from storage import _norm_deal_id
        deal_id_norm = _norm_deal_id(deal_id)
        d = storage.get_deal(deal_id_norm)
        if not d:
            return {"status": "error", "error": "deal_not_found", "deal_id": deal_id_norm}

        from datetime import datetime
        ts = datetime.fromtimestamp(d.get("created_at", 0)).strftime("%d.%m.%Y")
        uname = d.get("client_username") or "?"
        text = (
            f"@{uname} — {d.get('bank','?')} — {d.get('amount','?')} — "
            f"{ts} — {deal_id_norm} — {d.get('status','?')}"
        )

        existing_msg_id = d.get("deals_group_msg_id")
        target = await self._resolve_chat_target(gid)

        if existing_msg_id:
            try:
                await self.client.edit_message(target, existing_msg_id, text, link_preview=False)
                logger.info("edited deals_group msg=%s for deal=%s", existing_msg_id, deal_id_norm)
                return {"status": "ok", "deals_group": gid, "mode": "edited", "msg_id": existing_msg_id}
            except Exception as e:
                logger.warning("edit deals_group msg=%s failed (%s) — fallback to send", existing_msg_id, e)

        try:
            sent = await self.client.send_message(target, text, link_preview=False)
        except Exception as e:
            logger.warning("post_deals_group send failed: %s", e)
            return {"status": "error", "step": "send", "error": str(e)}
        new_msg_id = getattr(sent, "id", None)
        if new_msg_id:
            await storage.set_deals_group_msg_id(deal_id_norm, new_msg_id)
        logger.info("posted to deals_group: chat=%s msg_id=%s len=%d", gid, new_msg_id, len(text))
        return {"status": "ok", "deals_group": gid, "mode": "sent", "msg_id": new_msg_id}

    # === Авто-детект статусов в deals/accounts чатах ===

    _DEALS_QUERY_PATTERNS = [
        (
            re.compile(
                r"(?:список|дай|что|какие|покажи).{0,40}?"
                r"(?:для\s+|на\s+|нужно\s+)?попол(?:нен|нить|нения)",
                re.I | re.S,
            ),
            ("ПОПОЛНИТЬ", "ОЖИДАЕТ_ПОПОЛНЕНИЯ"),
            "📋 Сделки на пополнение",
        ),
        (
            re.compile(
                r"(?:список|дай|что|какие|покажи|\bлк\b).{0,40}?в\s+работе",
                re.I | re.S,
            ),
            ("ПОПОЛНЕНО", "В_РАБОТЕ", "ГОТОВО_К_ОТПУСКУ"),
            "🔧 ЛК в работе",
        ),
        (
            re.compile(
                r"(?:список|дай|что|какие|покажи).{0,40}?отработан"
                r"|^\s*отработанн",
                re.I | re.S | re.M,
            ),
            ("ЗАВЕРШЕНА",),
            "✅ Отработанные ЛК",
        ),
        (
            re.compile(
                r"(?:список|дай|что|какие|покажи).{0,40}?блок"
                r"|заблокирован"
                r"|^\s*блок(?:и|ов|ах)?\s*\??\s*$",
                re.I | re.S | re.M,
            ),
            ("ЗАБЛОКИРОВАН", "ОТМЕНА_СДЕЛКИ"),
            "🚫 Блоки и отмены",
        ),
    ]

    _DEALS_STATUS_PATTERNS = [
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?завершен", re.I | re.S), "ЗАВЕРШЕНА"),
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?отпущен", re.I | re.S), "ЗАВЕРШЕНА"),
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?(успешно\s+)?отработан", re.I | re.S), "ЗАВЕРШЕНА"),
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?(отмен|отказ)", re.I | re.S), "ОТМЕНА_СДЕЛКИ"),
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?блок", re.I | re.S), "ЗАБЛОКИРОВАН"),
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?готов[ао]?\s+к\s+отпуск", re.I | re.S), "ГОТОВО_К_ОТПУСКУ"),
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?в\s+работе", re.I | re.S), "В_РАБОТЕ"),
        (re.compile(r"сделк[ауи]\s+#?(\S+).*?пополнен", re.I | re.S), "ПОПОЛНЕНО"),
    ]

    @staticmethod
    def _format_deals_list(statuses: tuple, header: str) -> str:
        all_deals = storage.list_deals() or {}
        target = set(statuses)
        matching = [(did, d) for did, d in all_deals.items() if d.get("status") in target]
        if not matching:
            return f"{header}: пусто"
        matching.sort(key=lambda x: x[1].get("created_at", 0), reverse=True)
        lines = [f"{header} ({len(matching)}):", ""]
        for did, d in matching:
            fio = (d.get("fio") or "").strip() or "—"
            bank = (d.get("bank") or "").strip() or "—"
            amount = str(d.get("amount") or "").strip() or "—"
            status = d.get("status") or "—"
            uname_raw = (d.get("client_username") or "").lstrip("@").strip()
            uname_part = f" — @{uname_raw}" if uname_raw else ""
            lines.append(f"• {fio}{uname_part} — #{did} — {bank} — {amount} — {status}")
        return "\n".join(lines)

    async def _handle_deals_query(self, event, statuses: tuple, header: str) -> None:
        msg = self._format_deals_list(statuses, header)
        chunks = _split_text(msg, 3900)
        try:
            await event.reply(chunks[0], link_preview=False)
            if len(chunks) > 1:
                target = await self._resolve_chat_target(event.chat_id)
                for extra in chunks[1:]:
                    await asyncio.sleep(0.3)
                    await self.client.send_message(target, extra, link_preview=False)
            logger.info(
                "deals_chat list query: header=%r, statuses=%s, parts=%d, len=%d",
                header, statuses, len(chunks), len(msg),
            )
        except Exception as e:
            logger.warning("deals list reply failed: %s", e)

    async def _handle_deals_group_message(self, event):
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return
        logger.info("deals_chat msg from sender=%s, len=%d", event.sender_id, len(text))

        for rx, statuses, header in self._DEALS_QUERY_PATTERNS:
            if rx.search(text):
                logger.info("deals_chat query matched: header=%r statuses=%s", header, statuses)
                await self._handle_deals_query(event, statuses, header)
                return

        for rx, new_status in self._DEALS_STATUS_PATTERNS:
            m = rx.search(text)
            if not m:
                continue
            deal_id = m.group(1).lstrip("#").strip(".,;:!?")
            logger.info("deals_chat pattern matched: deal=%s -> status=%s", deal_id, new_status)
            await self._apply_status_change(deal_id, new_status)
            return
        logger.info("deals_chat: no status pattern matched in %r", text[:120])

    async def _handle_accounts_group_message(self, event):
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return

        if "отработано" not in text.lower() and "отработан" not in text.lower():
            return
        # Снимаем markdown маркеры (Telegram копипаста)
        clean = re.sub(r"\*\*|__|~~|`+", "", text)
        parts = re.split(r"\s*[—–\-]\s*", clean)
        results = []
        if len(parts) >= 3:
            fio = parts[0].strip()
            bank = parts[1].strip()
            if fio and bank:
                results = storage.find_deal_by(fio=fio, bank=bank)
                # Fallback если по двум полям не нашли
                if not results:
                    results = storage.find_deal_by(fio=fio) or storage.find_deal_by(bank=fio)
        elif len(parts) == 2:
            # «X — отработано» — пробуем X как ФИО, либо как БАНК
            x = parts[0].strip()
            if x:
                results = storage.find_deal_by(fio=x) or storage.find_deal_by(bank=x)
                if not results:
                    logger.info("accounts_chat: 2-part fallback nothing for %r", x)
        else:
            logger.info("accounts_chat: malformed (parts<2): %r", text[:120])
            return
        active = [d for d in results if d.get("status") not in ("ЗАВЕРШЕНА", "ГОТОВО_К_ОТПУСКУ")]
        if len(active) == 0:
            logger.warning("accounts_chat: no active deal for fio=%r bank=%r (total=%d)", fio, bank, len(results))
            return
        if len(active) > 1:
            logger.warning("accounts_chat: multiple active deals for fio=%r bank=%r (count=%d)", fio, bank, len(active))
            active.sort(key=lambda d: d.get("created_at", 0), reverse=True)
        deal_id = active[0]["deal_id"]
        logger.info("accounts_chat ОТРАБОТАНО: deal=%s fio=%r bank=%r -> ГОТОВО_К_ОТПУСКУ", deal_id, fio, bank)
        await self._apply_status_change(deal_id, "ГОТОВО_К_ОТПУСКУ")
        # После «ОТРАБОТАНО» запрашиваем у Тимона отпуск/выплату по методу,
        # который выбрал клиент при оформлении (GUARANTOR / USDT_TRC20).
        try:
            await self._send_release_request(deal_id)
        except Exception as e:
            logger.warning("send_release_request failed for deal=%s: %s", deal_id, e)

    async def _send_release_request(self, deal_id: str) -> bool:
        """После статуса ГОТОВО_К_ОТПУСКУ шлёт запрос в чат «Сделки и выплаты»
        с тегом @TimonSkupCL — отпустить деньги в гаранте или отправить выплату
        на USDT TRC20 (по методу из deal.method).

        Если deals_group_id не задан — ничего не делает.
        """
        gid = storage.get_deals_group_id()
        if not gid:
            logger.info("release_request: deals_group_id не задан, пропуск")
            return False
        deal = storage.get_deal(deal_id) or {}
        method = (deal.get("method") or "").upper()
        bank = deal.get("bank") or "—"
        fio = (deal.get("fio") or "").strip() or "—"
        if method == "GUARANTOR":
            ask = "отпустить деньги в гаранте"
        elif method == "USDT_TRC20":
            ask = "отправить выплату на USDT TRC20"
        else:
            ask = f"отпустить (метод: {method or 'не указан'})"
        text = (
            f"❓ Сделка #{deal_id} ({bank}, {fio}) отработана — {ask}? "
            f"@TimonSkupCL"
        )
        try:
            target = await self._resolve_chat_target(gid)
            await self.client.send_message(target, text, link_preview=False)
            logger.info(
                "release request sent: deal=%s method=%s -> deals_group=%s",
                deal_id, method, gid,
            )
            return True
        except Exception as e:
            logger.warning("release request send failed for deal=%s: %s", deal_id, e)
            return False

    async def _refresh_accounts_post(self, deal_id: str) -> bool:
        """Перерисовывает пост в чате «Отработка аккаунтов» текущим состоянием
        сделки. Используется после record_deal (когда подцепили pending пост)
        и после смены статуса. Если у сделки нет accounts_group_msg_id —
        ничего не делает."""
        deal = storage.get_deal(deal_id) or {}
        msg_id = deal.get("accounts_group_msg_id")
        if not msg_id:
            return False
        accounts_id = storage.get_accounts_group_id()
        if not accounts_id:
            return False
        text = self._build_accounts_msg(
            bank=deal.get("bank") or "",
            deal_id=deal_id,
            method=deal.get("method") or "",
            client_username=deal.get("client_username") or "",
            status_internal=deal.get("status") or "В_РАБОТЕ",
            fio=deal.get("fio") or "",
        )
        try:
            target = await self._resolve_chat_target(accounts_id)
            await self.client.edit_message(target, msg_id, text, link_preview=False)
            logger.info("refreshed accounts_msg=%s for deal=%s status=%s", msg_id, deal_id, deal.get("status"))
            return True
        except Exception as e:
            logger.warning("refresh accounts_msg=%s for deal=%s failed: %s", msg_id, deal_id, e)
            return False

    async def _apply_status_change(self, deal_id: str, new_status: str):
        """Универсальная процедура: update_deal_status + post в deals_group +
        обновление поста в accounts_group + notify клиента в его work_chat."""
        ok = await storage.update_deal_status(deal_id, new_status)
        if not ok:
            logger.warning("apply_status_change: deal %s not found in storage", deal_id)
            return
        deal = storage.get_deal(deal_id) or {}

        try:
            await self._tool_post_deals_group(deal_id=deal_id)
        except Exception as e:
            logger.warning("post_deals_group failed after status change: %s", e)

        try:
            await self._refresh_accounts_post(deal_id)
        except Exception as e:
            logger.warning("refresh_accounts_post failed after status change: %s", e)

        # Авто-запись в бухгалтерию при завершении сделки.
        # turnover = amount сделки в рублях.
        # partner_payout = USDT-эквивалент по прайсу банка (берём deal.amount как
        # рублёвую сумму или fee если задано иначе — используем deal.amount как turnover).
        if new_status == "ЗАВЕРШЕНА":
            try:
                await self._accounting_record_deal_completion(deal_id, deal)
            except Exception as e:
                logger.warning("accounting auto-record failed for deal=%s: %s", deal_id, e)

        work_chat = deal.get("work_chat_id")
        client_msg = self._client_status_message(new_status, deal, deal_id=deal_id)
        if not work_chat or not client_msg:
            logger.info(
                "client notify skipped: deal=%s work_chat=%s msg=%r",
                deal_id, work_chat, bool(client_msg),
            )
            return
        try:
            target = await self._resolve_chat_target(work_chat)
            await self.client.send_message(target, client_msg, link_preview=False)
            logger.info("client notified deal=%s status=%s chat=%s", deal_id, new_status, work_chat)
        except Exception as e:
            logger.warning("client notify failed for deal=%s: %s", deal_id, e)

    @staticmethod
    def _client_status_message(status: str, deal: dict, deal_id: str = "") -> str:
        bank = deal.get("bank", "")
        did = f"#{deal_id}" if deal_id else ""
        if status == "ПОПОЛНЕНО":
            return f"Сделка {did} пополнена ({bank}), начинаем работу."
        if status == "В_РАБОТЕ":
            return f"Ваш аккаунт {did} ({bank}) в работе."
        if status == "ГОТОВО_К_ОТПУСКУ":
            return f"Сделка {did} ({bank}) почти готова к отпуску."
        if status == "ЗАВЕРШЕНА":
            return f"Сделка {did} завершена ({bank}), всё прошло успешно."
        if status == "ЗАБЛОКИРОВАН":
            return f"По сделке {did} ({bank}) есть нюансы — оператор разбирается."
        if status == "ОТМЕНА_СДЕЛКИ":
            return f"Сделка {did} ({bank}) приостановлена. Менеджер свяжется."
        return ""

    # === Перевяз ЛК — авто-форвард в Отработка аккаунтов ===

    # Маппинг внутренних статусов → строки для шаблона accounts_group.
    _ACCOUNTS_STATUS_MAP = {
        "ПОПОЛНИТЬ": "В РАБОТЕ",
        "ОЖИДАЕТ_ПОПОЛНЕНИЯ": "В РАБОТЕ",
        "ПОПОЛНЕНО": "В РАБОТЕ",
        "В_РАБОТЕ": "В РАБОТЕ",
        "ГОТОВО_К_ОТПУСКУ": "В РАБОТЕ",
        "ЗАВЕРШЕНА": "УСПЕШНО ОТРАБОТАНО",
        "ОТМЕНА_СДЕЛКИ": "ОТМЕНА СДЕЛКИ",
        "ЗАБЛОКИРОВАН": "БЛОК",
    }

    @classmethod
    def _accounts_status_label(cls, internal_status: str) -> str:
        return cls._ACCOUNTS_STATUS_MAP.get(internal_status, "В РАБОТЕ")

    @classmethod
    def _build_accounts_msg(
        cls,
        bank: str,
        deal_id: str = "",
        method: str = "",
        client_username: str = "",
        status_internal: str = "ПОПОЛНИТЬ",
        fio: str = "",
    ) -> str:
        """Шаблон поста для чата «Отработка аккаунтов»:
            Банк: ...
            ФИО: ... (ЛК-ФИО клиента, держатель счёта)
            Номер сделки: ...
            Статус: ...
            Поставщик: @...
        """
        if deal_id:
            deal_line = f"Номер сделки: #{deal_id}"
        elif method == "USDT_TRC20":
            deal_line = "Номер сделки: выплата на USDT TRC20"
        else:
            deal_line = "Номер сделки: выплата после отработки"
        status_label = cls._accounts_status_label(status_internal)
        uname = (client_username or "").lstrip("@").strip() or "—"
        fio_clean = (fio or "").strip() or "—"
        return (
            f"Банк: {bank or '—'}\n"
            f"ФИО: {fio_clean}\n"
            f"{deal_line}\n"
            f"Статус: {status_label}\n"
            f"Поставщик: @{uname}"
        )

    _PEREVYAZ_RE = re.compile(r"перевяз\s+лк\s+выполнен", re.I)
    _PEREVYAZ_FIO_RE = re.compile(r"фио\s*:?[\s]*(.+)", re.I)
    _PEREVYAZ_LK_RE = re.compile(r"лк\s*:?[\s]*(.+)", re.I)

    async def _maybe_handle_perevyaz(self, event, chat_info: dict) -> bool:
        text = (event.message.text or "")
        if not self._PEREVYAZ_RE.search(text):
            return False

        accounts_id = storage.get_accounts_group_id()
        if not accounts_id:
            logger.warning("perevyaz: accounts_group_id не задан, форвард пропущен")
            return False

        fio = ""
        lk = ""
        for line in text.splitlines():
            m = self._PEREVYAZ_FIO_RE.match(line.strip())
            if m and not fio:
                fio = m.group(1).strip()
                continue
            m = self._PEREVYAZ_LK_RE.match(line.strip())
            if m and not lk:
                lk = m.group(1).strip()
        logger.info("perevyaz detected: fio=%r lk=%r chat=%s", fio, lk, event.chat_id)

        client_id = chat_info.get("client_id")
        deal = None
        if client_id:
            for did, d in (storage.list_deals() or {}).items():
                if d.get("work_chat_id") and abs(int(d["work_chat_id"])) == abs(int(event.chat_id)):
                    if d.get("status") not in ("ЗАВЕРШЕНА", "ОТМЕНА_СДЕЛКИ"):
                        deal = {"deal_id": did, **d}
                        break

        bank = (deal or {}).get("bank") or lk or "—"
        deal_id = (deal or {}).get("deal_id", "")
        # Метод: сначала из deal, иначе из chat_info (AI установил через
        # set_payment_method ДО record_deal).
        method = (deal or {}).get("method") or chat_info.get("payment_method", "")
        # USDT адрес: из chat_info (для USDT_TRC20)
        usdt_address = chat_info.get("usdt_address", "") if method == "USDT_TRC20" else ""
        client_uname = (
            (deal or {}).get("client_username")
            or chat_info.get("client_username", "")
            or ""
        )
        status_internal = (deal or {}).get("status") or "В_РАБОТЕ"
        # ФИО берём из deal (там точно ФИО держателя ЛК), либо
        # из распарсенной из работ-чата строки «ФИО: …» (fallback).
        fio_value = (deal or {}).get("fio") or fio or ""

        msg = self._build_accounts_msg(
            bank=bank,
            deal_id=deal_id,
            method=method,
            client_username=client_uname,
            status_internal=status_internal,
            fio=fio_value,
            usdt_address=usdt_address,
        )
        sent = None
        try:
            target = await self._resolve_chat_target(accounts_id)
            sent = await self.client.send_message(target, msg, link_preview=False)
            logger.info("perevyaz forwarded to accounts_group=%s deal=%s", accounts_id, deal_id)
        except Exception as e:
            logger.warning("perevyaz forward failed: %s", e)
            return True

        sent_msg_id = getattr(sent, "id", None)
        if sent_msg_id:
            if deal_id:
                await storage.set_accounts_group_msg_id(deal_id, sent_msg_id)
                logger.info("perevyaz: linked accounts_msg=%s to deal=%s", sent_msg_id, deal_id)
            else:
                await storage.set_pending_accounts_post(event.chat_id, sent_msg_id)
                logger.info("perevyaz: stashed accounts_msg=%s as PENDING for chat=%s", sent_msg_id, event.chat_id)

        if deal_id:
            await self._apply_status_change(deal_id, "В_РАБОТЕ")
        return True

    # === Бухгалтерия ===

    async def _accounting_record_deal_completion(self, deal_id: str, deal: dict):
        """Авто-запись в storage.accounting при ЗАВЕРШЕНА.

        turnover_rub = deal.amount (если строка с цифрами — парсим).
        partner_payout_usdt = берётся из deal.fee если задано как USDT;
        иначе пропускаем (менеджер заполнит вручную через accounting_group).
        """
        date_str = accounting.today_str()

        # turnover (rub)
        amount_rub = self._parse_money_to_rub(deal.get("amount"))
        if amount_rub > 0:
            await storage.accounting_add_turnover(
                date_str=date_str,
                deal_id=deal_id,
                amount_rub=amount_rub,
                label=deal.get("bank") or "",
            )

        # partner payout — если в fee указано «400$» / «400 USDT» — добавим в выплаты
        fee_usdt = self._parse_money_to_usdt(deal.get("fee"))
        if fee_usdt > 0:
            client = deal.get("client_username") or ""
            if client and not client.startswith("@"):
                client = "@" + client.lstrip("@")
            await storage.accounting_add_partner_payout(
                date_str=date_str,
                deal_id=deal_id,
                amount_usdt=fee_usdt,
                client=client,
            )
        logger.info(
            "accounting auto-record: deal=%s rub=%s usdt=%s",
            deal_id, amount_rub, fee_usdt,
        )

    @staticmethod
    def _parse_money_to_rub(s) -> float:
        """50 000₽ / 50000 / '50 000 руб' → 50000.0. USDT → 0 (не наша единица)."""
        if not s:
            return 0.0
        txt = str(s).lower()
        if any(x in txt for x in ("usdt", "$", "usd", "trc20")):
            return 0.0
        digits = "".join(ch for ch in txt if ch.isdigit() or ch in ".,")
        digits = digits.replace(",", ".")
        # многоточек → берём первое
        if digits.count(".") > 1:
            parts = digits.split(".")
            digits = parts[0] + "." + "".join(parts[1:])
        try:
            return float(digits or 0)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_money_to_usdt(s) -> float:
        """'400$' / '400 USDT' → 400.0. Без явного USDT/$ → 0."""
        if not s:
            return 0.0
        txt = str(s).lower()
        if not any(x in txt for x in ("usdt", "$", "usd", "trc20")):
            return 0.0
        digits = "".join(ch for ch in txt if ch.isdigit() or ch in ".,")
        digits = digits.replace(",", ".")
        if digits.count(".") > 1:
            parts = digits.split(".")
            digits = parts[0] + "." + "".join(parts[1:])
        try:
            return float(digits or 0)
        except ValueError:
            return 0.0

    def _enrich_application_lk_prices(self, app: dict) -> None:
        """Заполняет lk_cost_usdt из storage.deals и/или прайса если оператор
        не указал «ЦЕНА ЛК» в строке заявки.

        Приоритет:
          1) Текст оператора (уже стоит lk_cost_usdt > 0 — не трогаем)
          2) storage.find_deal_by(fio=, bank=) — берём parse_money_to_usdt(amount)
          3) accounting.PRICING_TABLE_USDT[bank] — встроенный прайс
        """
        import accounting as acc

        def resolve(item: dict) -> float:
            if _f := acc._f:
                cur = _f(item.get("lk_cost_usdt"))
                if cur > 0:
                    return cur
            bank = (item.get("bank") or "").strip()
            fio = (item.get("fio") or "").strip()
            # 1) из deals (если AI записал при record_deal)
            if bank and fio:
                try:
                    matches = storage.find_deal_by(fio=fio, bank=bank) or []
                    for d in matches:
                        # amount может быть в USDT (400$) либо в рублях
                        amt_usdt = self._parse_money_to_usdt(d.get("amount"))
                        if amt_usdt > 0:
                            return amt_usdt
                except Exception:
                    pass
            # 2) встроенный прайс
            return acc.lookup_pricing(bank)

        for item in (app.get("intake") or []):
            v = resolve(item)
            if v > 0:
                item["lk_cost_usdt"] = v
        for item in (app.get("output") or []):
            v = resolve(item)
            if v > 0:
                item["lk_cost_usdt"] = v

    async def _auto_release_application_lks(self, app: dict) -> dict:
        """Для каждого output ЛК заявки находит активную сделку (по fio+bank
        либо только fio либо только bank) и переводит в ГОТОВО_К_ОТПУСКУ.

        После смены статуса вызывается _send_release_request — запрос Тимону
        отпустить выплату по методу.

        Возвращает: {matched: int, missed: int, missed_names: [str]}.
        """
        matched = 0
        missed = 0
        missed_names: list = []

        outputs = app.get("output") or []
        for o in outputs:
            fio = (o.get("fio") or "").strip()
            bank = (o.get("bank") or "").strip()
            if not fio:
                continue
            # Ищем сделку — сначала точное совпадение fio+bank, потом fallback
            results = []
            if bank:
                results = storage.find_deal_by(fio=fio, bank=bank)
            if not results:
                results = storage.find_deal_by(fio=fio)
            # Только активные (не уже завершённые)
            active = [
                d for d in results
                if d.get("status") not in ("ЗАВЕРШЕНА", "ГОТОВО_К_ОТПУСКУ")
            ]
            if not active:
                missed += 1
                label = f"{bank} {fio}".strip() or fio
                missed_names.append(label)
                continue
            # Берём самую свежую (если несколько активных)
            active.sort(key=lambda d: d.get("created_at", 0), reverse=True)
            deal_id = active[0]["deal_id"]
            try:
                await self._apply_status_change(deal_id, "ГОТОВО_К_ОТПУСКУ")
                await self._send_release_request(deal_id)
                matched += 1
                logger.info(
                    "auto-release: app=%s lk=%s/%s -> deal=%s ГОТОВО_К_ОТПУСКУ",
                    app.get("id"), bank, fio, deal_id,
                )
            except Exception as e:
                logger.warning(
                    "auto-release failed for deal=%s (lk=%s/%s): %s",
                    deal_id, bank, fio, e,
                )
                missed += 1
                missed_names.append(f"{bank} {fio} (ошибка)")

        return {
            "matched": matched,
            "missed": missed,
            "missed_names": missed_names,
        }

    async def _handle_accounting_group_message(self, event):
        """Обработчик сообщений в чате «Бухгалтерия». Парсит команды через
        accounting.parse_command. Если не команда — игнорим (можно дописать
        тегом @userbot help для справки)."""
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return  # своё сообщение

        # Help / help
        if text.lower() in ("/help", "help", "помощь", "?"):
            try:
                await event.reply(accounting.HELP_TEXT, parse_mode="html", link_preview=False)
            except Exception:
                pass
            return

        # Сначала пробуем парсер формата «СТАРТ» (мульти-строка с ПРИЕМ/Вывод/Выплата).
        # Telegram-копипаста содержит markdown-маркеры (**bold**, __italic__),
        # снимаем их перед парсингом — иначе регулярки не находят «Заявка».
        clean_text = re.sub(r"\*\*|__|~~|`+", "", text)
        if "\n" in clean_text and ("заявка" in clean_text.lower() or "прием" in clean_text.lower() or "приём" in clean_text.lower()):
            app = accounting.parse_application(clean_text)
            if app:
                try:
                    # Обогащаем цены ЛК если оператор не указал «ЦЕНА ЛК»:
                    # 1) ищем в storage.deals по fio+bank → amount
                    # 2) fallback в встроенный прайс accounting.PRICING_TABLE_USDT
                    self._enrich_application_lk_prices(app)
                    date_str = accounting.today_str()
                    await storage.accounting_add_application(date_str, {**app, "ts": time.time()})
                    report = accounting.format_application_report(app)

                    # Автоматом меняем статус всех ЛК заявки на ГОТОВО_К_ОТПУСКУ.
                    # Output ЛК = выводные счета поставщиков (наши клиенты-продавцы).
                    # Каждое такое ЛК — отдельная сделка в storage.deals.
                    # При смене статуса:
                    #   - пост в чате «Отработка аккаунтов» меняется на УСПЕШНО ОТРАБОТАНО
                    #   - в чат «Сделки и выплаты» уходит обновление статуса
                    #   - клиенту-поставщику в work_chat уходит уведомление
                    #   - + _send_release_request с тегом @TimonSkupCL.
                    auto_results = await self._auto_release_application_lks(app)
                    if auto_results["matched"]:
                        report += (
                            f"\n\n🔄 Автоматом переведено в ГОТОВО_К_ОТПУСКУ: "
                            f"<b>{auto_results['matched']}</b> сделок"
                        )
                        if auto_results["missed"]:
                            report += (
                                f"\n⚠️ Не найдено в storage.deals: "
                                f"{auto_results['missed']} ЛК "
                                f"({', '.join(auto_results['missed_names'])})"
                            )
                    elif auto_results["missed"]:
                        report += (
                            f"\n\n⚠️ Сделки не найдены в storage для ЛК: "
                            f"{', '.join(auto_results['missed_names'])}"
                        )

                    await event.reply(report, parse_mode="html", link_preview=False)
                    logger.info(
                        "accounting_chat: parsed application id=%s margin=%.0f$ "
                        "auto-released=%d missed=%d",
                        app.get("id"),
                        accounting.compute_application(app)["margin_usdt"],
                        auto_results["matched"], auto_results["missed"],
                    )
                except Exception as e:
                    logger.exception("application save failed: %s", e)
                    try:
                        await event.reply(f"⚠️ Ошибка сохранения заявки: {e}")
                    except Exception:
                        pass
                return

        cmd = accounting.parse_command(text)
        if not cmd:
            logger.info("accounting_chat: not a command: %r", text[:80])
            return

        kind = cmd["cmd"]
        date_str = cmd.get("date") or accounting.today_str()
        ack = ""

        try:
            if kind == "report":
                rec = storage.get_accounting_day(date_str)
                report = accounting.format_day_report(date_str, rec)
                await event.reply(report, parse_mode="html", link_preview=False)
                return
            elif kind == "courses":
                await storage.accounting_set_courses(
                    date_str, cmd["buy"], cmd["sell"]
                )
                ack = (
                    f"💱 Курс USDT за {date_str}: "
                    f"закуп {cmd['buy']:.2f} ₽ / партнёру {cmd['sell']:.2f} ₽"
                )
            elif kind == "manual":
                await storage.accounting_add_manual(
                    date_str, cmd["label"], cmd["amount_rub"]
                )
                sign = "📈 приход" if cmd["amount_rub"] >= 0 else "📉 расход"
                ack = (
                    f"{sign}: {cmd['label']} — "
                    f"{abs(cmd['amount_rub']):.0f} ₽ ({date_str})"
                )
            elif kind == "lk":
                await storage.accounting_add_lk_cost(
                    date_str, cmd["bank"], cmd["amount_usdt"], cmd.get("label", "")
                )
                ack = (
                    f"🛒 ЛК {cmd['bank']}: {cmd['amount_usdt']:.2f} USDT "
                    f"({date_str})"
                )
            elif kind == "turnover":
                await storage.accounting_add_turnover(
                    date_str,
                    cmd.get("deal_id", ""),
                    cmd["amount_rub"],
                    cmd.get("label", ""),
                )
                did = cmd.get("deal_id", "")
                did_part = f" (#{did})" if did else ""
                ack = f"💰 Оборот {cmd['amount_rub']:.0f} ₽{did_part} ({date_str})"
            elif kind == "partner":
                await storage.accounting_add_partner_payout(
                    date_str,
                    cmd.get("deal_id", ""),
                    cmd["amount_usdt"],
                    cmd["client"],
                )
                did = cmd.get("deal_id", "")
                did_part = f" (#{did})" if did else ""
                ack = (
                    f"💸 Партнёру {cmd['client']}: {cmd['amount_usdt']:.2f} USDT"
                    f"{did_part} ({date_str})"
                )
            elif kind == "remove_manual":
                ok = await storage.accounting_remove_manual(date_str, cmd["index"])
                ack = (
                    f"🗑 Правка #{cmd['index']+1} удалена ({date_str})"
                    if ok else
                    f"⚠️ Правка #{cmd['index']+1} не найдена ({date_str})"
                )
            else:
                return

            try:
                await event.reply(ack, link_preview=False)
            except Exception:
                pass
        except Exception as e:
            logger.exception("accounting handler failed: %s", e)
            try:
                await event.reply(f"⚠️ Ошибка: {e}")
            except Exception:
                pass

    async def stop(self):
        await self.client.disconnect()

    async def create_work_chat(self, client_name: str, client_id: int = 0) -> dict:
        title = config.CHAT_TITLE_TEMPLATE.format(client_name=client_name)
        about = config.CHAT_DESCRIPTION_TEMPLATE.format(client_name=client_name)

        result = await self.client(CreateChannelRequest(title=title, about=about, megagroup=True))
        channel = result.chats[0]
        logger.info("Created group '%s' (id=%s)", title, channel.id)

        if client_id:
            await storage.register_chat(channel.id, client_id, client_name)

        workers = storage.get_workers()
        statuses: dict = {}
        users_to_invite = []
        for username in workers:
            uname = username.lstrip("@").strip()
            if not uname:
                continue
            try:
                ent = await self.client.get_entity(uname)
                users_to_invite.append(ent)
                statuses[uname] = "найден"
            except UsernameNotOccupiedError:
                statuses[uname] = "не существует"
            except FloodWaitError as e:
                logger.warning("get_entity flood wait %ds for @%s", e.seconds, uname)
                statuses[uname] = f"flood wait {e.seconds}s"
            except Exception as e:
                statuses[uname] = f"ошибка резолва: {e}"

        for user in users_to_invite:
            uname_or_id = user.username or str(user.id)
            try:
                await self.client(InviteToChannelRequest(channel, [user]))
                statuses[uname_or_id] = "добавлен"
            except UserPrivacyRestrictedError:
                statuses[uname_or_id] = "запрещены приглашения (Privacy)"
            except UserNotMutualContactError:
                statuses[uname_or_id] = "нет в контактах"
            except PeerFloodError:
                statuses[uname_or_id] = "флуд-лимит Telegram"
            except FloodWaitError as e:
                logger.warning("invite flood wait %ds for @%s", e.seconds, uname_or_id)
                status
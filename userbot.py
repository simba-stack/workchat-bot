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
from typing import Optional, Tuple

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
import accounting2
import learn

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


def _fmt_username(uname: Optional[str], fallback: str = "не указан") -> str:
    """Единый формат @username для постов/уведомлений.
    Гарантирует ровно один префикс @, обрезает пробелы.
    Если username пустой — возвращает fallback."""
    if not uname:
        return fallback
    clean = str(uname).strip().lstrip("@")
    if not clean:
        return fallback
    return f"@{clean}"


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
        # Накладной режим редактирования заявки V2: chat_id -> (date_str, app_id).
        # Когда оператор пишет "редактировать заявку N" в Группе 2, мы запоминаем
        # пару, и при следующей валидной заявке от него — удаляем старую и применяем новую.
        self._editing_app: dict[int, tuple] = {}

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
        # Если ID положительный — это либо user, либо нормализованный channel/megagroup
        # без -100 префикса (что хранится в storage после _norm_chat_id).
        # Сначала пробуем как PeerChannel (для группового чата) — это покрывает
        # 99% наших чатов: рабочие беседы, ЛК-группа, бухгалтерия, сделки.
        # Если падает — fallback на обычный get_entity (вдруг это user).
        candidates = []
        if cid_int > 0:
            try:
                from telethon.tl.types import PeerChannel
                candidates.append(PeerChannel(cid_int))
            except Exception:
                pass
        candidates.append(cid_int)
        last_err = None
        for cand in candidates:
            try:
                ent = await self.client.get_entity(cand)
                self._chat_entity_cache[cid_int] = ent
                return ent
            except Exception as e:
                last_err = e
                continue
        logger.warning("resolve_chat_target failed for %s: %s", cid_int, last_err)
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

        # Группа 1 «Личные кабинеты» — анкеты ЛК + БРАК/БЛОК
        lk_id = storage.get_lk_group_id()
        if lk_id and _norm_chat_id(chat_id) == _norm_chat_id(lk_id):
            await self._handle_lk_group_message(event)
            return

        # Группа 2 «Бухгалтерия» — заявки v2
        accounting_id = storage.get_accounting_group_id()
        if accounting_id and _norm_chat_id(chat_id) == _norm_chat_id(accounting_id):
            await self._handle_accounting_v2_message(event)
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

        # Авто-фиксация @username клиента: если в managed_chats нет username
        # или он устарел — подтягиваем из event.sender и обновляем индекс.
        # Это «лечит» legacy-беседы, созданные до сохранения username.
        if sender_username:
            stored_uname = (chat_info.get("client_username") or "").lower().strip()
            if stored_uname != sender_username:
                try:
                    await storage.update_client_username(chat_id, sender_username)
                    logger.info(
                        "managed_chat=%s: client_username updated → @%s",
                        chat_id, sender_username,
                    )
                except Exception as e:
                    logger.warning("auto-update username failed for chat=%s: %s", chat_id, e)

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
        if tool_name == "create_lk_card":
            return await self._tool_create_lk_card(
                chat_id=chat_id,
                bank=tool_input.get("bank", ""),
                fio=tool_input.get("fio", ""),
                price_usdt=float(tool_input.get("price_usdt", 0) or 0),
                payment_method=tool_input.get("payment_method", ""),
                deal_id=tool_input.get("deal_id", ""),
                usdt_address=tool_input.get("usdt_address", ""),
            )
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

        moved_card_id = None
        if work_chat_id is not None:
            try:
                wc_norm = abs(int(work_chat_id))
                for cid, c in (storage.list_lk_cards() or {}).items():
                    if not c.get("work_chat_id"):
                        continue
                    if abs(int(c.get("work_chat_id"))) != wc_norm:
                        continue
                    if c.get("payment_method") != "GUARANTOR_AFTER_WORK":
                        continue
                    if c.get("status") != "ОТРАБОТАН":
                        continue
                    await storage.set_lk_card_status(
                        cid, "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
                        deal_id=deal_id, by="record_deal",
                    )
                    await self._refresh_lk_card_post(cid)
                    moved_card_id = cid
                    break
            except Exception as e:
                logger.warning("record_deal: lk-card auto-move failed: %s", e)

        result = {"status": "ok", "deal_id": deal_id, "initial_status": "ПОПОЛНИТЬ"}
        if moved_card_id:
            result["lk_card_moved"] = moved_card_id
            result["lk_new_status"] = "ПОПОЛНИТЬ_И_ОТПУСТИТЬ"
        return result

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

    async def _tool_create_lk_card(
        self,
        chat_id,
        bank: str = "",
        fio: str = "",
        price_usdt: float = 0.0,
        payment_method: str = "",
        deal_id: str = "",
        usdt_address: str = "",
    ) -> dict:
        """Создаёт карточку ЛК (анкету) в Группе 1 'Личные кабинеты'."""
        bank = (bank or "").strip()
        fio = (fio or "").strip()
        payment_method = (payment_method or "").strip().upper()
        deal_id = (deal_id or "").strip()
        usdt_address = (usdt_address or "").strip()

        if not bank:
            return {"status": "error", "error": "bank_required"}
        if not fio:
            return {"status": "error", "error": "fio_required"}
        if price_usdt <= 0:
            return {"status": "error", "error": "price_invalid"}
        if payment_method not in accounting2.PAYMENT_METHODS:
            return {
                "status": "error",
                "error": "payment_method_invalid",
                "allowed": list(accounting2.PAYMENT_METHODS),
            }
        if payment_method == "USDT_TRC20" and not usdt_address:
            return {"status": "error", "error": "usdt_address_required_for_usdt"}
        # GUARANTOR_AFTER_WORK — сделка создаётся ПОСЛЕ отработки клиентом,
        # на момент создания карточки её ещё нет, deal_id опционален.
        if (
            payment_method.startswith("GUARANTOR")
            and payment_method != "GUARANTOR_AFTER_WORK"
            and not deal_id
        ):
            return {"status": "error", "error": "deal_id_required_for_guarantor"}

        lk_group = storage.get_lk_group_id()
        if not lk_group:
            return {"status": "error", "error": "lk_group_not_set"}

        info = storage.get_chat_info(chat_id) or {}
        client_id = info.get("client_id") or ""
        client_username = info.get("client_username") or ""
        # Если username пуст в managed_chats — попробуем получить через Telethon
        if not client_username and client_id:
            try:
                ent = await self.client.get_entity(int(client_id))
                if getattr(ent, "username", None):
                    client_username = ent.username
                    # Заодно сохраним в managed_chats для будущего
                    await storage.set_chat_payment_info(
                        chat_id, client_username=client_username
                    )
            except Exception as e:
                logger.warning("create_lk_card: resolve username failed: %s", e)

        card_id = await storage.add_lk_card(
            supplier=client_username or "—",
            bank=bank,
            fio=fio,
            price_usdt=float(price_usdt),
            payment_method=payment_method,
            deal_id=deal_id,
            usdt_address=usdt_address,
            status="В_РАБОТЕ",
            client_id=client_id,
            client_username=client_username,
            work_chat_id=chat_id,
            created_by="ai_tool",
        )

        try:
            await self._refresh_lk_card_post(card_id)
        except Exception as e:
            logger.warning("refresh_lk_card_post failed for card=%s: %s", card_id, e)

        return {
            "status": "ok",
            "card_id": card_id,
            "lk_group_id": lk_group,
            "bank": bank,
            "fio": fio,
            "price_usdt": price_usdt,
            "payment_method": payment_method,
        }

    async def _apply_status_change(self, deal_id: str, new_status: str):
        """Внутренняя процедура: смена статуса сделки в storage и уведомление
        клиента в его work_chat. Видимость для команды теперь — через
        Группу 1 ЛК (карточки): deal_id хранится в карточке, статус меняет
        accounting_v2 при отчёте."""
        ok = await storage.update_deal_status(deal_id, new_status)
        if not ok:
            logger.warning("apply_status_change: deal %s not found in storage", deal_id)
            return
        deal = storage.get_deal(deal_id) or {}

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

    # === V2: Группа 1 «Личные кабинеты» (анкеты + БРАК/БЛОК) ===

    async def _handle_lk_group_message(self, event):
        """Анкета ЛК / команды БРАК / БЛОК в Группе 1."""
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return  # своё сообщение

        low = text.lower()

        # Массовый импорт существующих ЛК
        if low.startswith("/import_lk") or low.startswith("импорт лк"):
            await self._apply_import_lk(event, text)
            return

        # Синхронизация клиентов: проходим по managed_chats, наполняем индекс
        # @username -> work_chat. Чините legacy-беседы где client_username пустой.
        if (
            low.startswith("/sync_clients")
            or low.startswith("/checkupidgroup")
            or low.startswith("синхронизация клиентов")
        ):
            await self._apply_sync_clients(event)
            return

        # БРАК / БЛОК — короткие команды
        if low.startswith("брак"):
            cmd = accounting2.parse_brak_command(text)
            if cmd:
                await self._apply_brak_command(event, cmd)
            return
        if low.startswith("блок"):
            cmd = accounting2.parse_blok_command(text)
            if cmd:
                await self._apply_blok_command(event, cmd)
            return

        # Анкета (мульти-строка с банком/ценой/методом)
        if "\n" in text and ("банк" in low or "поставщик" in low):
            card_data = accounting2.parse_lk_card(text)
            if card_data:
                await self._apply_manual_lk_card(event, card_data)
            return

        # Компактный однострочный формат: БАНК ФИО ЦЕНА МЕТОД [@username] [#deal_id|USDT]
        if "\n" not in text:
            compact = accounting2.parse_lk_card_compact(text)
            if compact:
                await self._apply_manual_lk_card(event, compact)
                return

    async def _apply_brak_command(self, event, cmd: dict):
        """БРАК — найти карточку → статус БРАК → уведомить клиента → если
        был гарант-deal, попросить отменить + написать в чат сделок."""
        cards = storage.find_lk_card(bank=cmd["bank"], fio=cmd["fio"])
        active = [c for c in cards if c.get("status") not in ("БРАК", "ЗАВЕРШЁН")]
        if not active:
            await event.reply(
                f"⚠️ Не нашёл активную карточку: <b>{cmd['bank']} {cmd['fio']}</b>.",
                parse_mode="html",
            )
            return
        card = active[0]
        cid = card["card_id"]
        await storage.set_lk_card_status(
            cid, "БРАК",
            brak_reason=cmd.get("reason", ""),
            by="lk_group",
        )
        await self._refresh_lk_card_post(cid)

        # Уведомить клиента в work_chat
        wc = card.get("work_chat_id")
        msg_to_client = (
            f"⚠️ К сожалению, ваш ЛК <b>{card.get('bank')}</b> "
            f"({card.get('fio')}) не подошёл."
        )
        if cmd.get("reason"):
            msg_to_client += f"\n\n<b>Причина:</b> {cmd['reason']}"
        # Если был гарант-deal → попросить отменить
        deal_id = card.get("deal_id")
        method = card.get("payment_method", "")
        if deal_id and method.startswith("GUARANTOR"):
            msg_to_client += (
                f"\n\nПо вашей сделке #{deal_id} нужно отменить — "
                f"пришлите, пожалуйста, подтверждение из бота гаранта."
            )
        if wc:
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target, msg_to_client, parse_mode="html", link_preview=False,
                )
            except Exception as e:
                logger.warning("brak notify client failed: %s", e)

        # В Группу 1 ЛК — уведомление об отмене сделки (если был гарант).
        # Тимон в этой группе участник — увидит и заберёт деньги в Conte.
        if deal_id and method.startswith("GUARANTOR"):
            lk_gid = storage.get_lk_group_id()
            if lk_gid:
                try:
                    target = await self._resolve_chat_target(lk_gid)
                    await self.client.send_message(
                        target,
                        f"❌ <b>Сделка #{deal_id} ОТМЕНЕНА</b> "
                        f"(БРАК ЛК {card.get('bank')} {card.get('fio')})\n"
                        f"⚠️ Нужно ЗАБРАТЬ ДЕНЬГИ с этой сделки. @TimonSkupCL",
                        parse_mode="html",
                    )
                except Exception as e:
                    logger.warning("brak deal-cancel notify failed: %s", e)

        await event.reply(
            f"✅ ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) → <b>БРАК</b>.\n"
            f"Клиент уведомлён.",
            parse_mode="html",
        )

    async def _apply_blok_command(self, event, cmd: dict):
        """БЛОК — найти карточку → статус БЛОК + сумма + примечание →
        уведомить клиента + тэг Тимона."""
        cards = storage.find_lk_card(bank=cmd["bank"], fio=cmd["fio"])
        active = [c for c in cards if c.get("status") not in ("БРАК", "ЗАВЕРШЁН")]
        if not active:
            await event.reply(
                f"⚠️ Не нашёл активную карточку: <b>{cmd['bank']} {cmd['fio']}</b>.",
                parse_mode="html",
            )
            return
        card = active[0]
        cid = card["card_id"]
        await storage.set_lk_card_status(
            cid, "БЛОК",
            block_amount_rub=cmd.get("amount_rub", 0),
            block_note=cmd.get("note", ""),
            by="lk_group",
        )
        await self._refresh_lk_card_post(cid)

        wc = card.get("work_chat_id")
        msg = (
            f"🚫 На ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) "
            f"возник <b>БЛОК</b> на {accounting2._fmt_rub(cmd.get('amount_rub', 0))}."
        )
        if cmd.get("note"):
            msg += f"\n\n<b>Что нужно сделать:</b> {cmd['note']}"
        msg += "\n\n@TimonSkupCL — посмотри пожалуйста."
        if wc:
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target, msg, parse_mode="html", link_preview=False,
                )
            except Exception as e:
                logger.warning("blok notify client failed: %s", e)

        await event.reply(
            f"✅ ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) → <b>БЛОК</b> "
            f"{accounting2._fmt_rub(cmd.get('amount_rub', 0))}.",
            parse_mode="html",
        )

    def _resolve_work_chat_by_supplier(self, supplier: str) -> dict:
        """Ищет work_chat по @username поставщика (= username клиента в managed_chats).
        Возвращает {work_chat_id, client_id, client_username} или {}.

        Сначала через обратный индекс client_username_index (O(1)),
        затем fallback линейным проходом по managed_chats — на случай,
        если индекс ещё не наполнен (старые беседы до /sync_clients)."""
        if not supplier:
            return {}
        target = supplier.lstrip("@").lower().strip()
        if not target:
            return {}

        def _build(chat_key, info):
            try:
                wc_id = int(chat_key)
            except (TypeError, ValueError):
                wc_id = chat_key
            return {
                "work_chat_id": wc_id,
                "client_id": int(info.get("client_id") or 0),
                "client_username": info.get("client_username") or target,
            }

        # 1) Быстрый путь: обратный индекс
        idx_key = storage.find_chat_by_client_username(target)
        if idx_key:
            info = storage.get_chat_info(idx_key) or {}
            if info:
                return _build(idx_key, info)

        # 2) Fallback: линейный поиск (на случай если индекс пуст)
        for chat_key in storage.get_managed_chat_ids():
            info = storage.get_chat_info(chat_key) or {}
            uname = (info.get("client_username") or "").lstrip("@").lower().strip()
            if uname and uname == target:
                return _build(chat_key, info)
        return {}

    async def _apply_manual_lk_card(self, event, card_data: dict):
        """Менеджер вручную создал анкету — сохраняем в storage и сразу
        публикуем сам шаблон анкеты (а не короткое подтверждение)."""
        # Если в карточке указан @поставщик — попробуем привязать
        # её к work_chat клиента (нужно для авто-тегания при отработке).
        supplier = (card_data.get("supplier") or "").lstrip("@")
        if supplier and not card_data.get("work_chat_id"):
            resolved = self._resolve_work_chat_by_supplier(supplier)
            if resolved:
                card_data = {**card_data, **resolved}
                logger.info(
                    "manual_lk_card: linked work_chat=%s for supplier=@%s",
                    resolved.get("work_chat_id"), supplier,
                )
            else:
                logger.warning(
                    "manual_lk_card: no work_chat for supplier=@%s — "
                    "auto-tag on отработка будет недоступен",
                    supplier,
                )
        card_id = await storage.add_lk_card(**card_data, created_by="manual")
        # Получаем готовый рендер анкеты
        card = storage.get_lk_card(card_id) or {}
        text = accounting2.format_lk_card(card)
        # Публикуем анкету как reply на исходное сообщение оператора
        sent = None
        try:
            sent = await event.reply(text, parse_mode="html", link_preview=False)
        except Exception as e:
            logger.warning("manual_lk_card reply failed: %s", e)
        # Сохраняем msg_id шаблона (а НЕ исходной команды) — чтобы
        # _refresh_lk_card_post мог редактировать ту же анкету.
        sent_id = getattr(sent, "id", None)
        if sent_id:
            await storage.set_lk_card_msg_id(card_id, int(sent_id))

    async def _apply_import_lk(self, event, text: str):
        """Массовый импорт существующих ЛК.

        Формат сообщения:
            /import_lk
            АЛЬФА Иванов 400 USDT_TRC20 @ivanov TX...
            ОЗОН Петров 300 GUARANTOR_AFTER_WORK - @petrov

        Каждая строка после команды парсится как compact-формат и подвязывается
        к work_chat по @поставщику (через managed_chats).
        """
        lines = [ln.strip() for ln in (text or "").splitlines()]
        rows = [ln for ln in lines[1:] if ln]
        if not rows:
            usage = (
                "ℹ️ <b>Массовый импорт ЛК</b>\n\n"
                "Формат:\n"
                "<pre>/import_lk\n"
                "АЛЬФА Иванов Иван 400 USDT_TRC20 @ivanov TXxxx\n"
                "ОЗОН Петров 300 GUARANTOR_AFTER_WORK - @petrov\n"
                "ТОЧКА Сидоров 250 GUARANTOR_AFTER #12345 @sidorov</pre>\n"
                "Каждая строка = одна карточка ЛК.\n"
                "@поставщик нужен чтобы привязать карточку к рабочей беседе клиента."
            )
            try:
                await event.reply(usage, parse_mode="html", link_preview=False)
            except Exception:
                pass
            return

        ok_lines: list = []
        skip_lines: list = []
        for raw in rows:
            parsed = accounting2.parse_lk_card_compact(raw)
            if not parsed:
                skip_lines.append(f"❌ <code>{raw[:80]}</code> — формат не распознан")
                continue
            supplier = (parsed.get("supplier") or "").lstrip("@")
            resolved = (
                self._resolve_work_chat_by_supplier(supplier) if supplier else {}
            )
            if resolved:
                parsed.update(resolved)
            try:
                card_id = await storage.add_lk_card(**parsed, created_by="import")
            except Exception as e:
                skip_lines.append(f"❌ <code>{raw[:80]}</code> — ошибка: {e}")
                continue
            # Постим анкету в Группу 1
            try:
                card = storage.get_lk_card(card_id) or {}
                rendered = accounting2.format_lk_card(card)
                sent = await event.reply(
                    rendered, parse_mode="html", link_preview=False,
                )
                sent_id = getattr(sent, "id", None)
                if sent_id:
                    await storage.set_lk_card_msg_id(card_id, int(sent_id))
            except Exception as e:
                logger.warning("import_lk reply failed for %s: %s", card_id, e)
            mark = "🔗" if resolved else "⚠️"
            note = "" if resolved else " <i>(work_chat не найден)</i>"
            sup_disp = _fmt_username(supplier, fallback="—")
            ok_lines.append(
                f"{mark} <code>#{card_id}</code> {parsed.get('bank')} "
                f"{parsed.get('fio')} → {sup_disp}{note}"
            )

        report = "📦 <b>Импорт ЛК завершён</b>\n\n"
        if ok_lines:
            report += f"✅ Добавлено: <b>{len(ok_lines)}</b>\n"
            report += "\n".join(ok_lines[:30])
            if len(ok_lines) > 30:
                report += f"\n<i>… и ещё {len(ok_lines) - 30}</i>"
        if skip_lines:
            if ok_lines:
                report += "\n\n"
            report += f"⚠️ Пропущено: <b>{len(skip_lines)}</b>\n"
            report += "\n".join(skip_lines[:10])
            if len(skip_lines) > 10:
                report += f"\n<i>… и ещё {len(skip_lines) - 10}</i>"
        try:
            await event.reply(report, parse_mode="html", link_preview=False)
        except Exception as e:
            logger.warning("import_lk summary failed: %s", e)

    async def _apply_sync_clients(self, event):
        """Синхронизация client_username по всем managed_chats:
          1. Если client_username пустой — резолвим через get_entity(client_id).
          2. Наполняем обратный индекс client_username -> chat_id.
          3. Идём от старых бесед к новым (по created_at), чтобы у клиентов
             с несколькими беседами в индексе осталась самая свежая.
        """
        chat_ids = storage.get_managed_chat_ids()
        # Сортируем по created_at ASC — самая свежая беседа окажется в индексе последней.
        items = []
        for key in chat_ids:
            info = storage.get_chat_info(key) or {}
            items.append((info.get("created_at", 0), key, info))
        items.sort(key=lambda t: t[0])

        try:
            await event.reply(
                f"🔄 Синхронизирую <b>{len(items)}</b> рабочих чатов…",
                parse_mode="html", link_preview=False,
            )
        except Exception:
            pass

        resolved_n = 0
        already_n = 0
        no_username_n = 0
        errors_n = 0
        bullets: list = []
        for _, key, info in items:
            client_id = int(info.get("client_id") or 0)
            current = (info.get("client_username") or "").lstrip("@").strip()
            uname = current
            if not uname and client_id:
                try:
                    ent = await self.client.get_entity(client_id)
                    uname = (getattr(ent, "username", None) or "").lstrip("@").strip()
                except Exception as e:
                    errors_n += 1
                    logger.warning("sync_clients: get_entity %s failed: %s", client_id, e)
                    continue
            if not uname:
                no_username_n += 1
                continue
            ok = await storage.update_client_username(key, uname)
            if ok and not current:
                resolved_n += 1
                bullets.append(f"🆕 <code>{key}</code> → @{uname}")
            elif ok:
                resolved_n += 1
            else:
                already_n += 1

        report = (
            "✅ <b>Синхронизация завершена</b>\n\n"
            f"Всего чатов: <b>{len(items)}</b>\n"
            f"Привязано/обновлено: <b>{resolved_n}</b>\n"
            f"Уже актуальны: <b>{already_n}</b>\n"
            f"Без username: <b>{no_username_n}</b>\n"
            f"Ошибок резолва: <b>{errors_n}</b>"
        )
        if bullets:
            report += "\n\n<b>Резолвлено впервые:</b>\n" + "\n".join(bullets[:30])
            if len(bullets) > 30:
                report += f"\n<i>… и ещё {len(bullets) - 30}</i>"
        try:
            await event.reply(report, parse_mode="html", link_preview=False)
        except Exception as e:
            logger.warning("sync_clients summary failed: %s", e)

    async def _refresh_lk_card_post(self, card_id: str) -> bool:
        """Edit/post карточки в Группе 1 актуальным состоянием."""
        card = storage.get_lk_card(card_id)
        if not card:
            return False
        gid = storage.get_lk_group_id()
        if not gid:
            return False
        text = accounting2.format_lk_card(card)
        msg_id = card.get("lk_group_msg_id") or 0
        try:
            target = await self._resolve_chat_target(gid)
            if msg_id:
                try:
                    await self.client.edit_message(
                        target, msg_id, text, parse_mode="html", link_preview=False,
                    )
                    return True
                except Exception:
                    pass
            sent = await self.client.send_message(
                target, text, parse_mode="html", link_preview=False,
            )
            new_id = getattr(sent, "id", None)
            if new_id:
                await storage.set_lk_card_msg_id(card_id, new_id)
            return True
        except Exception as e:
            logger.warning("refresh_lk_card_post card=%s: %s", card_id, e)
            return False

    async def _create_lk_card_from_perevyaz(
        self, event, chat_info: dict, lk_text: str = "", fio_text: str = "",
    ) -> Optional[str]:
        """Триггер «Перевяз ЛК выполнен» — создаём карточку в Группе 1.

        Источники данных по приоритету:
          - bank: lk_text (из 'ЛК:' в перевязке) → deal.bank
          - fio:  fio_text (из 'ФИО:' в перевязке) → deal.fio
          - method, usdt_address: chat_info (что AI собрал через set_payment_method)
          - price: deal.amount → прайс по банку
          - deal_id: deal.deal_id (если уже создан до перевязки)
          - supplier/client_username: chat_info.client_username → resolve по client_id

        Если не хватает только метода — запрашиваем у клиента ТОЛЬКО метод.
        Банк/ФИО уже в самом сообщении CRM-бота, спрашивать их повторно нельзя.
        """
        wc = event.chat_id
        method = chat_info.get("payment_method", "")
        client_uname = chat_info.get("client_username") or ""
        # Резолв username через Telethon если в managed_chats пусто
        if not client_uname:
            client_id = chat_info.get("client_id")
            if client_id:
                try:
                    ent = await self.client.get_entity(int(client_id))
                    if getattr(ent, "username", None):
                        client_uname = ent.username
                        await storage.update_client_username(wc, client_uname)
                except Exception as e:
                    logger.warning("perevyaz: resolve username failed: %s", e)
        # Сделка для этого work_chat (для GUARANTOR_BEFORE/_AFTER уже создана)
        deal = None
        for did, d in (storage.list_deals() or {}).items():
            if d.get("work_chat_id") and abs(int(d["work_chat_id"])) == abs(int(wc)):
                if d.get("status") not in ("ЗАВЕРШЕНА", "ОТМЕНА_СДЕЛКИ"):
                    deal = {"deal_id": did, **d}
                    break

        # Bank: предпочитаем то что прислал CRM в перевязке
        bank = (lk_text or "").strip() or ((deal or {}).get("bank") or "")
        # FIO: то же самое
        fio = (fio_text or "").strip() or ((deal or {}).get("fio") or "")
        # Цена: deal.amount → прайс по банку
        price = float((deal or {}).get("amount") or 0)
        if not price and bank:
            price = accounting2.lookup_pricing(bank)

        deal_id = (deal or {}).get("deal_id") or ""
        usdt_addr = chat_info.get("usdt_address") or ""

        # Минимум для создания карточки: bank + method.
        # Если есть только bank — спрашиваем у клиента только метод.
        if not bank:
            logger.warning(
                "perevyaz: bank не определён (lk_text=%r, deal=%s) — пропускаем",
                lk_text, bool(deal),
            )
            return None
        if not method:
            await self._request_lk_data_from_client(
                event, chat_info, deal, bank=bank, fio=fio,
            )
            return None

        card_id = await storage.add_lk_card(
            supplier=client_uname,
            bank=bank,
            fio=fio,
            price_usdt=price,
            payment_method=method,
            deal_id=deal_id,
            usdt_address=usdt_addr,
            status="В_РАБОТЕ",
            client_id=chat_info.get("client_id") or 0,
            client_username=client_uname,
            work_chat_id=wc,
            created_by="perevyaz",
        )
        await self._refresh_lk_card_post(card_id)
        logger.info(
            "LK card created from perevyaz: %s for chat=%s bank=%s fio=%s method=%s",
            card_id, wc, bank, fio, method,
        )
        return card_id

    async def _request_lk_data_from_client(
        self, event, chat_info: dict, deal: Optional[dict],
        bank: str = "", fio: str = "",
    ):
        """Перевяз есть, но данных не хватает — спросить у клиента + reminder.

        ВАЖНО: банк и ФИО CRM-бот присылает прямо в тексте перевязки
        (`ЛК:` и `ФИО:`), повторно их спрашивать не нужно. Спрашиваем
        только реально недостающие поля (обычно — метод оплаты)."""
        wc = event.chat_id
        missing = []
        # bank/fio проверяем с учётом того что было распарсено из перевязки
        eff_bank = bank or (deal or {}).get("bank") or ""
        eff_fio = fio or (deal or {}).get("fio") or ""
        if not eff_bank:
            missing.append("банк")
        if not eff_fio:
            missing.append("ФИО держателя счёта")
        if not chat_info.get("payment_method"):
            missing.append("метод оплаты (USDT TRC20 или сделка в гаранте)")
        if not missing:
            return
        msg = (
            f"✅ Перевяз ЛК зафиксирован. Чтобы продолжить, уточните:\n"
            + "\n".join(f"• {x}" for x in missing)
        )
        try:
            target = await self._resolve_chat_target(wc)
            await self.client.send_message(target, msg, link_preview=False)
        except Exception as e:
            logger.warning("request_lk_data send failed: %s", e)
        # Запоминаем что нужны данные — reminder loop
        from storage import _norm_chat_id
        key = _norm_chat_id(wc)
        # Простой механизм: pending dict, проверяется фоновой задачей.
        # Здесь — отдельный launcher, без бесконечного создания тасок.
        if not hasattr(self, "_lk_pending"):
            self._lk_pending = {}
        if key not in self._lk_pending:
            self._lk_pending[key] = {
                "chat_id": wc,
                "reminder_count": 0,
                "started_at": time.time(),
            }
            asyncio.create_task(self._lk_reminder_loop(wc, key))

    async def _lk_reminder_loop(self, wc, key: str):
        """Раз в 5 минут пинаем клиента, пока данные не появятся (или 6 раз).

        Условия выхода (НЕ шлём напоминалку):
          1) Карточка ЛК уже существует.
          2) Метод оплаты собран → триггерим создание карточки и выходим.
          3) Клиент пишет в чате после старта pending — значит он не застрял,
             AI ведёт диалог сам, спамить ⏰-напоминаниями не нужно.
        """
        started_at = (self._lk_pending.get(key) or {}).get("started_at", time.time())
        from storage import _norm_chat_id  # noqa
        norm_key = _norm_chat_id(wc)
        for _ in range(6):
            await asyncio.sleep(300)  # 5 минут
            # 1) Карточка уже есть → нечего напоминать
            existing = storage.find_lk_card(work_chat_id=wc) or []
            if existing:
                logger.info(
                    "lk_reminder: chat=%s — карточка(и) уже есть (%d), выходим",
                    wc, len(existing),
                )
                self._lk_pending.pop(key, None)
                return
            chat_info = storage.get_chat_info(wc) or {}
            # 2) Все данные собраны → триггерим creation
            method = chat_info.get("payment_method")
            if method:
                fake_event = type("E", (), {"chat_id": wc, "message": None})()
                try:
                    await self._create_lk_card_from_perevyaz(fake_event, chat_info)
                except Exception:
                    pass
                self._lk_pending.pop(key, None)
                return
            # 3) Клиент уже общался после запроса — AI ведёт диалог сам
            last_client = (self._last_client_msg_ts or {}).get(norm_key, 0)
            if last_client > started_at:
                logger.info(
                    "lk_reminder: chat=%s — клиент активен (last_client_msg > started_at), "
                    "AI сам ведёт диалог, выходим без напоминания",
                    wc,
                )
                self._lk_pending.pop(key, None)
                return
            # Напомним
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target,
                    "⏰ Напоминаю — без указания метода оплаты и реквизитов "
                    "мы не сможем работать с вашим ЛК. Ответьте, пожалуйста.",
                    link_preview=False,
                )
            except Exception as e:
                logger.warning("lk reminder send failed: %s", e)
        self._lk_pending.pop(key, None)

    # === V2: Группа 2 «Бухгалтерия» (заявки v2 + расчёт + auto-update ЛК) ===

    async def _handle_accounting_v2_message(self, event):
        """Заявки V2 + команды управления (удалить/редактировать/help)."""
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return

        low = text.lower()

        # Команда: удалить заявку N
        m = re.match(r"^\s*удалить\s+заявку\s+#?(\d+)\s*$", text, re.I)
        if m:
            app_id = int(m.group(1))
            date_str = accounting2.today_str()
            ok = await storage.remove_application_v2(date_str, app_id)
            if ok:
                await event.reply(
                    f"🗑 Заявка #{app_id} за {date_str} удалена.",
                    parse_mode="html",
                )
            else:
                await event.reply(
                    f"⚠️ Заявка #{app_id} за {date_str} не найдена.",
                    parse_mode="html",
                )
            return

        # Команда: редактировать/исправить/изменить заявку N
        m = re.match(
            r"^\s*(?:редактировать|исправить|изменить)\s+заявку\s+#?(\d+)\s*$",
            text, re.I,
        )
        if m:
            app_id = int(m.group(1))
            date_str = accounting2.today_str()
            apps = storage.get_applications_v2(date_str) or []
            target = next(
                (a for a in apps if int(a.get("id", 0)) == app_id), None
            )
            if not target:
                await event.reply(
                    f"⚠️ Заявка #{app_id} за {date_str} не найдена.",
                    parse_mode="html",
                )
                return
            self._editing_app[event.chat_id] = (date_str, app_id)
            await event.reply(
                f"✏️ Жду новый текст заявки <b>#{app_id}</b> за {date_str}.\n\n"
                "Пришлите её в полном формате (<code>ЗАЯВКА N\nПРИЕМ: ...</code>).\n"
                "Старая заявка будет удалена и заменена новой.\n\n"
                "Чтобы отменить — напишите <code>отмена</code>.",
                parse_mode="html",
            )
            return

        # Команда: правка цены ЛК в заявке.
        # Формат: "заявка N <банк/ФИО любые слова> <цена>$"
        # Примеры:
        #   заявка 1 Альфа Иванов 350
        #   заявка 1 Иванов Иван Иванович Альфа 400$
        m = re.match(
            r"^\s*заявка\s+#?(\d+)\s+(.+?)\s+(\d+(?:\.\d+)?)\s*\$?\s*$",
            text, re.I,
        )
        if m and "\n" not in text:
            app_id = int(m.group(1))
            middle = m.group(2).strip()
            new_price = float(m.group(3))
            date_str = accounting2.today_str()
            apps = storage.get_applications_v2(date_str) or []
            target = next(
                (a for a in apps if int(a.get("id", 0)) == app_id), None
            )
            if not target:
                await event.reply(
                    f"⚠️ Заявка #{app_id} за {date_str} не найдена.",
                    parse_mode="html",
                )
                return
            # В middle ищем известный банк, остальное — ФИО
            BANKS = ("альфа", "озон", "точка", "втб", "райф", "райффайзен",
                     "уралсиб", "локо", "бкс", "дело", "убрир", "тинькофф")
            words = middle.split()
            bank_word = None
            fio_words = []
            for w in words:
                if w.lower() in BANKS or w.lower().startswith("райф"):
                    bank_word = w
                else:
                    fio_words.append(w)
            if not bank_word:
                await event.reply(
                    "⚠️ Не нашёл банк в команде. Формат: "
                    "<code>заявка N БАНК ФИО ЦЕНА</code>",
                    parse_mode="html",
                )
                return
            fio_q = " ".join(fio_words).strip()
            # Найти ЛК в заявке (intake или outputs) по банку+ФИО
            intake = target.get("intake") or {}
            outputs = target.get("outputs") or []
            updated_item = None
            for item in [intake] + outputs:
                if not item:
                    continue
                if (item.get("bank") or "").lower() != bank_word.lower():
                    continue
                if fio_q and fio_q.lower() not in (item.get("fio") or "").lower():
                    continue
                item["price_usdt_override"] = new_price
                updated_item = item
                break
            if not updated_item:
                await event.reply(
                    f"⚠️ ЛК <b>{bank_word} {fio_q}</b> в заявке #{app_id} не найден.",
                    parse_mode="html",
                )
                return
            # Пересчитать с учётом предыдущих заявок дня (без самой target)
            lk_cards = storage.list_lk_cards() or {}
            all_apps = storage.get_applications_v2(date_str) or []
            prev_apps = [
                p for p in all_apps if int(p.get("id", 0)) < int(app_id)
            ]
            new_computed = accounting2.compute_application_v2(
                target, lk_cards, prev_apps=prev_apps,
            )
            target["computed"] = new_computed
            await storage.update_application_v2(
                date_str, app_id,
                intake=intake,
                outputs=outputs,
                computed=new_computed,
            )
            # Перерисовать отчёт (edit) если знаем msg_id
            new_report = accounting2.format_application_report_v2(target, new_computed)
            new_report = (
                f"♻️ <i>Цена ЛК {bank_word} {fio_q} обновлена → "
                f"{new_price:.0f}$</i>\n\n" + new_report
            )
            report_msg_id = target.get("report_msg_id")
            edited = False
            if report_msg_id:
                try:
                    target_chat = await self._resolve_chat_target(event.chat_id)
                    await self.client.edit_message(
                        target_chat, int(report_msg_id), new_report,
                        parse_mode="html", link_preview=False,
                    )
                    edited = True
                except Exception as e:
                    logger.warning("edit report msg failed: %s", e)
            if not edited:
                await event.reply(new_report, parse_mode="html", link_preview=False)
            else:
                # Подтверждение оператору
                await event.reply(
                    f"✏️ Цена ЛК <b>{bank_word} {fio_q}</b> в заявке #{app_id} "
                    f"обновлена → <b>{new_price:.0f}$</b>. Отчёт пересчитан.",
                    parse_mode="html",
                )
            return

        # Команда: отмена редактирования
        if low in ("отмена", "cancel", "отменить") and event.chat_id in self._editing_app:
            ed_date, ed_id = self._editing_app.pop(event.chat_id)
            await event.reply(
                f"❎ Редактирование заявки #{ed_id} ({ed_date}) отменено.",
                parse_mode="html",
            )
            return

        # Заявка V2 (мульти-строка с «ЗАЯВКА N»)
        if "\n" in text and "заявка" in low:
            app = accounting2.parse_application_v2(text)
            if app:
                # Если был режим редактирования — удаляем старую перед применением
                editing = self._editing_app.pop(event.chat_id, None)
                replaced_info = None
                if editing:
                    ed_date, ed_id = editing
                    await storage.remove_application_v2(ed_date, ed_id)
                    replaced_info = (ed_date, ed_id)
                await self._apply_application_v2(event, app, replaced=replaced_info)
                return

        # Help
        if low in ("/help", "help", "помощь", "?"):
            await event.reply(
                "📊 <b>Бухгалтерия V2</b>\n\n"
                "<b>Заявка</b> — формат:\n"
                "<code>ЗАЯВКА 1\n"
                "ПРИЕМ:\n"
                "ОЗОН - Иванов - 1000000\n"
                "ВЫВЕДЕНО — 800000\n\n"
                "ВЫВОД СУММА:\n"
                "ОЗОН - Петров - 300000\n"
                "ТОЧКА - Сидоров - 500000\n\n"
                "Курс ВЫВОДА — 90\n"
                "Курс ВЫПЛАТЫ — 92\n"
                "ПРОЦЕНТ ВЫПЛАТЫ ПАРТНЕРУ: 40</code>\n\n"
                "После заявки юзербот посчитает маржу и автоматом переведёт "
                "ВСЕ ЛК (приёмный + выводные) в ОТРАБОТАН в Группе 1.\n\n"
                "<b>Команды управления:</b>\n"
                "• <code>удалить заявку N</code> — удалить заявку № N за сегодня\n"
                "• <code>редактировать заявку N</code> — заменить заявку № N "
                "(пришлите новый текст в полном формате)\n"
                "• <code>заявка N БАНК ФИО ЦЕНА</code> — изменить цену ЛК "
                "в заявке № N. Пример: <code>заявка 1 Альфа Иванов 350</code>\n"
                "• <code>отмена</code> — отменить ожидание редактирования",
                parse_mode="html",
            )
            return

    async def _apply_application_v2(self, event, app: dict, replaced=None):
        """Сохранить заявку, посчитать, ответить отчётом, авто-перевести ЛК.

        replaced: (date_str, app_id) если новая заявка заменяет старую (режим
        редактирования). Используется только для пометки в отчёте.
        """
        date_str = app.get("date") or accounting2.today_str()
        lk_cards = storage.list_lk_cards() or {}
        # Сначала сохраняем заявку чтобы получить id (нужен compute для current_app_id)
        full_app = {**app, "date": date_str}
        new_id = await storage.add_application_v2(date_str, full_app)
        full_app["id"] = new_id

        # prev_apps — все заявки за этот день, кроме текущей и заменяемой
        prev_apps_all = storage.get_applications_v2(date_str) or []
        replaced_id = replaced[1] if replaced else None
        prev_apps = [
            p for p in prev_apps_all
            if int(p.get("id", 0)) != int(new_id)
            and int(p.get("id", 0)) != int(replaced_id or 0)
        ]
        computed = accounting2.compute_application_v2(
            full_app, lk_cards, prev_apps=prev_apps,
        )
        full_app["computed"] = computed
        # Обновляем заявку с computed
        await storage.update_application_v2(date_str, new_id, computed=computed)

        report = accounting2.format_application_report_v2(full_app, computed)
        if replaced:
            ed_date, ed_id = replaced
            report = (
                f"♻️ <i>Заменена заявка #{ed_id} ({ed_date}).</i>\n\n" + report
            )

        # Auto-update ЛК → ОТРАБОТАН.
        # Учитываем И приёмный ЛК (intake), И выводные (outputs) — все они
        # реально отработали когда заявка закрыта.
        moved = 0
        intake = app.get("intake") or {}
        intake_list = []
        if intake.get("bank") and intake.get("fio"):
            intake_list.append(intake)
        for o in [*intake_list, *app.get("outputs", [])]:
            cards = storage.find_lk_card(
                bank=o.get("bank") or "", fio=o.get("fio") or ""
            )
            active = [
                c for c in cards
                if c.get("status") not in ("ОТРАБОТАН", "БРАК", "ЗАВЕРШЁН",
                                           "ПОПОЛНИТЬ_И_ОТПУСТИТЬ")
            ]
            if not active:
                continue
            card = active[0]
            cid = card["card_id"]
            method = card.get("payment_method", "")
            if method == "GUARANTOR_AFTER_WORK":
                # Особый случай — сделка после отработки.
                # Идём в work_chat клиента, тегаем, просим создать сделку.
                await storage.set_lk_card_status(
                    cid, "ОТРАБОТАН",
                    by="accounting_v2",
                    last_application_id=new_id,
                )
                await self._refresh_lk_card_post(cid)
                asyncio.create_task(
                    self._request_post_work_deal(card)
                )
            else:
                await storage.set_lk_card_status(
                    cid, "ОТРАБОТАН",
                    by="accounting_v2",
                    last_application_id=new_id,
                )
                await self._refresh_lk_card_post(cid)
            moved += 1

        if moved:
            report += f"\n\n🔄 Автоматом → ОТРАБОТАН: <b>{moved}</b> карточек."

        sent = await event.reply(report, parse_mode="html", link_preview=False)
        sent_id = getattr(sent, "id", None)
        if sent_id:
            await storage.update_application_v2(
                date_str, new_id, report_msg_id=int(sent_id)
            )
        logger.info(
            "applied app_v2 id=%s date=%s margin=%.0f$ moved=%d",
            new_id, date_str, computed.get("margin_usdt", 0), moved,
        )

    async def _request_post_work_deal(self, card: dict):
        """ЛК отработан, метод = GUARANTOR_AFTER_WORK → теги клиента в work_chat,
        просим создать сделку. Дальше AI получит номер и обновит карточку."""
        wc = card.get("work_chat_id")
        client_uname = card.get("client_username") or ""
        cid = card.get("card_id", "?")
        supplier = card.get("supplier") or ""
        if not wc:
            # Пытаемся резолвить work_chat прямо сейчас по @supplier —
            # если карточка создавалась вручную ДО регистрации клиента в managed_chats.
            resolved = self._resolve_work_chat_by_supplier(supplier)
            if resolved.get("work_chat_id"):
                wc = resolved["work_chat_id"]
                if not client_uname:
                    client_uname = resolved.get("client_username") or ""
                # Сохраним привязку чтобы в следующий раз сразу нашлось.
                try:
                    await storage.update_lk_card(
                        cid,
                        work_chat_id=wc,
                        client_id=resolved.get("client_id", 0),
                        client_username=client_uname,
                    )
                    logger.info(
                        "post-work deal: late-resolved work_chat=%s for card=%s "
                        "supplier=@%s", wc, cid, supplier,
                    )
                except Exception as e:
                    logger.warning("post-work deal: late-resolve save failed: %s", e)
            else:
                logger.warning(
                    "post-work deal SKIPPED for card=%s: no work_chat_id "
                    "(supplier=@%s — клиент с таким @username не зарегистрирован "
                    "в managed_chats; добавьте его через /start или используйте "
                    "/import_lk после того как клиент создаст рабочую беседу)",
                    cid, supplier or "—",
                )
                return
        msg = (
            f"✅ Ваш ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) "
            f"<b>отработан</b>.\n\n"
        )
        client_tag = _fmt_username(client_uname, fallback="")
        if client_tag:
            msg += f"{client_tag} "
        msg += (
            f"создайте, пожалуйста, гарант-сделку в Conte и пришлите номер — "
            f"оформим вашу выплату ({accounting2._fmt_usdt(card.get('price_usdt', 0))})."
        )
        try:
            target = await self._resolve_chat_target(wc)
            await self.client.send_message(
                target, msg, parse_mode="html", link_preview=False,
            )
            logger.info("post-work deal request sent for card=%s chat=%s", cid, wc)
        except Exception as e:
            logger.warning("post-work deal request failed for card=%s: %s", cid, e)

    # === Перевяз ЛК — авто-форвард в Отработка аккаунтов ===

    _PEREVYAZ_RE = re.compile(r"перевяз\s+лк\s+выполнен", re.I)
    _PEREVYAZ_FIO_RE = re.compile(r"фио\s*:?[\s]*(.+)", re.I)
    _PEREVYAZ_LK_RE = re.compile(r"лк\s*:?[\s]*(.+)", re.I)

    async def _maybe_handle_perevyaz(self, event, chat_info: dict) -> bool:
        """Триггер Перевяз ЛК выполнен — парсим банк и ФИО прямо из текста
        CRM-бота и создаём карточку в Группе 1 ЛК."""
        text = (event.message.text or "")
        if not self._PEREVYAZ_RE.search(text):
            return False
        lk_text = ""
        fio_text = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not lk_text:
                m_lk = self._PEREVYAZ_LK_RE.match(stripped)
                if m_lk:
                    lk_text = m_lk.group(1).strip()
                    continue
            if not fio_text:
                m_fio = self._PEREVYAZ_FIO_RE.match(stripped)
                if m_fio:
                    fio_text = m_fio.group(1).strip()
                    continue
        # Подчищаем lk_text — берём только первое слово (название банка),
        # вдруг CRM-бот в одной строке написал «Озон Альфа Точка» (мульти-LK).
        if lk_text:
            lk_text = lk_text.split()[0]
        logger.info(
            "perevyaz detected: lk=%r fio=%r chat=%s",
            lk_text, fio_text, event.chat_id,
        )
        try:
            await self._create_lk_card_from_perevyaz(
                event, chat_info, lk_text=lk_text, fio_text=fio_text,
            )
        except Exception as e:
            logger.warning("perevyaz: lk-card creation failed: %s", e)
        return True

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
                statuses[uname_or_id] = f"flood wait {e.seconds}s"
            except Exception as e:
                statuses[uname_or_id] = f"ошибка: {e}"

        for u, s in statuses.items():
            logger.info("invite chat=%s @%s -> %s", channel.id, u, s)

        if config.USERBOT_AS_ADMIN and self._me:
            try:
                rights = ChatAdminRights(
                    change_info=True, post_messages=True, edit_messages=True,
                    delete_messages=True, ban_users=True, invite_users=True,
                    pin_messages=True, add_admins=False, anonymous=False, manage_call=True,
                )
                await self.client(EditAdminRequest(
                    channel=channel, user_id=self._me, admin_rights=rights, rank="Owner"
                ))
            except Exception as e:
                logger.warning("Admin grant failed: %s", e)

        invite = await self.client(ExportChatInviteRequest(channel))

        if client_id:
            asyncio.create_task(self._watch_for_client_join(channel, client_id))

        return {
            "chat_id": channel.id,
            "title": title,
            "invite_link": invite.link,
            "statuses": statuses,
        }

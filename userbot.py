"""Userbot service: creates supergroups, invites workers, sends welcome on client join.

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
        # Per-chat locks для защиты от race condition в _send_welcome.
        # Только один путь (event или poll) может отправить welcome — второй увидит флаг.
        self._welcome_locks: dict[int, asyncio.Lock] = {}
        # AI: per-chat lock (не отвечаем на два сообщения параллельно в одном чате)
        self._ai_locks: dict[int, asyncio.Lock] = {}
        # AI: последний timestamp активности сотрудника/админа в managed-чате.
        # Ключ — нормализованный chat_id (str), значение — unix time.
        # Если worker писал в последние client_idle_minutes минут — AI молчит.
        self._last_worker_ts: dict[str, float] = {}
        # Кеш resolved-сущностей чатов для брейн/координаторской беседы.
        # Telethon send_message по сырому int ID иногда даёт InvalidPeer;
        # get_entity один раз за сессию решает проблему.
        self._chat_entity_cache: dict[int, object] = {}

    def _get_welcome_lock(self, chat_id: int) -> asyncio.Lock:
        """Возвращает (создаёт при необходимости) Lock для конкретного чата."""
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
        # Прогреваем сущности брейн / координаторского чатов, если они заданы.
        # Telethon после рестарта может не иметь их в кеше — get_entity заполнит.
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

        # Диагностика: листим топ-30 чатов, в которых состоит юзербот.
        # Помогает админу найти актуальный chat_id если чат был upgraded в
        # супергруппу или если ID в админке устарел.
        try:
            count = 0
            async for dialog in self.client.iter_dialogs(limit=30):
                ent = dialog.entity
                etype = type(ent).__name__
                title = getattr(ent, "title", None) or getattr(ent, "first_name", "") or "?"
                # event.chat_id Telethon выдаёт в Bot API форме (signed),
                # show_id отображаем в этом же формате.
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
        """Отправляет welcome-сообщение. Идемпотентна: защищена Lock'ом per chat_id.

        Порядок работы:
        1. Захватываем lock для этого chat_id — только один путь проходит одновременно.
        2. Проверяем флаг welcome_sent (быстрый выход если уже отправлено).
        3. Ждём 1 секунду (клиент должен увидеть чат).
        4. Снова проверяем (другой путь мог успеть пока мы спали).
        5. Отправляем и ставим флаг.
        """
        lock = self._get_welcome_lock(int(chat_id))
        async with lock:
            info = storage.get_chat_info(chat_id)
            if not info or info.get("welcome_sent"):
                return False
            # Небольшая задержка, чтобы клиент успел загрузить чат
            await asyncio.sleep(1)
            # Повторная проверка — другой путь мог отправить пока мы спали
            info = storage.get_chat_info(chat_id)
            if not info or info.get("welcome_sent"):
                return False

            welcome = storage.get_welcome()
            entities_raw = storage.get_welcome_entities()
            try:
                if entities_raw:
                    # Кастомные эмодзи / форматирование — отправляем одним сообщением
                    ents = _entities_to_telethon(entities_raw)
                    await self.client.send_message(chat_id, welcome, formatting_entities=ents)
                else:
                    # Обычный текст — режем на части при необходимости
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
        """Fallback: poll participants of `channel` every 3s for up to `timeout_sec`.
        As soon as `client_id` is in the chat — send welcome (if not already).
        """
        deadline = asyncio.get_event_loop().time() + timeout_sec
        try:
            while asyncio.get_event_loop().time() < deadline:
                # Досрочный выход если welcome уже отправлен (например, через ChatAction)
                info = storage.get_chat_info(channel.id)
                if info and info.get("welcome_sent"):
                    logger.info(
                        "watch chat=%s: welcome already sent, exiting watcher", channel.id
                    )
                    return
                try:
                    await self.client(GetParticipantRequest(
                        channel=channel,
                        participant=PeerUser(client_id),
                    ))
                    # Клиент в чате → отправляем welcome
                    logger.info(
                        "watch chat=%s: client %s joined, sending welcome",
                        channel.id, client_id,
                    )
                    await self._send_welcome(channel.id, client_id, source="poll")
                    return
                except UserNotParticipantError:
                    pass  # ещё не вошёл — продолжаем поллинг
                except FloodWaitError as e:
                    logger.warning("watch chat=%s flood wait %ss", channel.id, e.seconds)
                    await asyncio.sleep(e.seconds + 1)
                    continue
                except Exception as e:
                    logger.warning("watch chat=%s poll error: %s", channel.id, e)
                await asyncio.sleep(3)
            logger.info(
                "watch chat=%s: timeout (%ss), client never joined", channel.id, timeout_sec
            )
        except Exception as e:
            logger.warning("watch chat=%s: unexpected error: %s", channel.id, e)

    async def _resolve_chat_target(self, chat_id):
        """Возвращает entity для send_message (cache'им чтобы не дёргать API).

        Telethon при сыром int chat_id (особенно для не-prefixed форм) иногда
        выдаёт InvalidPeer. get_entity нормализует, кеш сохраняет результат
        на всё время жизни процесса. None при ошибке.
        """
        try:
            cid_int = int(chat_id)
        except Exception:
            return chat_id  # пусть Telethon сам что-то сделает
        if cid_int in self._chat_entity_cache:
            return self._chat_entity_cache[cid_int]
        try:
            ent = await self.client.get_entity(cid_int)
            self._chat_entity_cache[cid_int] = ent
            return ent
        except Exception as e:
            logger.warning("resolve_chat_target failed for %s: %s", cid_int, e)
            return cid_int  # fallback — пусть send попытается с raw ID

    # === AI brain handlers ===

    async def _handle_ai_message(self, event):
        """Реакция на новые сообщения в managed-чатах. Триггер AI-ответа.

        Логика:
        1. Игнорим всё, что не в managed_chats или в brain_chat.
        2. Если автор — worker/admin/сам юзербот: апдейтим _last_worker_ts и выходим.
        3. Если AI выключен — выходим.
        4. Если worker писал в последние client_idle_minutes — выходим (skipped_worker_active++).
        5. Sleep 3-8с (имитация набора). Повторная проверка worker activity.
        6. Fetch history + brain_notes → brain.generate_reply.
        7. Отправляем ответ. Логируем в brain_chat. Бампим ai_stats.
        """
        chat_id = event.chat_id
        bid = storage.get_brain_chat_id()

        # Импортируем нормализатор локально — он уже умеет ходить по разным
        # форматам chat_id (signed -100xxx supergroup, bare, signed group).
        from storage import _norm_chat_id

        # Диагностический лог — видно в Railway всё что прилетает в userbot
        try:
            sender_id_dbg = event.sender_id
        except Exception:
            sender_id_dbg = "?"
        logger.info(
            "userbot event: chat_id=%s norm=%s brain_id=%s norm_brain=%s sender=%s",
            chat_id, _norm_chat_id(chat_id), bid,
            (_norm_chat_id(bid) if bid else "—"), sender_id_dbg,
        )

        # Сообщение в брейн-чате — отдельный путь: writeback в граф знаний
        if bid and _norm_chat_id(chat_id) == _norm_chat_id(bid):
            await self._handle_brain_chat_writeback(event)
            return

        chat_info = storage.get_chat_info(chat_id)
        if not chat_info:
            return
        if not event.message or not (event.message.text or "").strip():
            return

        sender_id = event.sender_id
        if self._me and sender_id == self._me.id:
            return  # своё сообщение

        # Определяем кто прислал (worker/admin или клиент)
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

        # Используем нормализованный ключ для _last_worker_ts (как в storage)
        from storage import _norm_chat_id  # локальный импорт, чтобы не циклить
        chat_key = _norm_chat_id(chat_id)

        if is_worker:
            self._last_worker_ts[chat_key] = time.time()
            logger.info("AI: worker activity in chat=%s by @%s", chat_id, sender_username)
            return

        # === Это сообщение клиента ===
        if not storage.is_ai_enabled():
            return
        if not config.ANTHROPIC_API_KEY:
            return

        client_id = chat_info.get("client_id")
        # Параноя: отвечаем только если автор именно наш зарегистрированный клиент
        if client_id and sender_id != client_id:
            return

        idle_min = max(0, storage.get_client_idle_minutes())
        idle_sec = idle_min * 60
        # idle_sec == 0 -> AI всегда отвечает (без проверки worker activity)
        if idle_sec > 0 and time.time() - self._last_worker_ts.get(chat_key, 0) < idle_sec:
            await storage.bump_ai_stats(skipped_worker_active=1)
            logger.info("AI: skip chat=%s — worker active in last %dm", chat_id, idle_min)
            return

        # Per-chat lock: не запускаем второй AI-ответ пока не закончим первый
        lock = self._ai_locks.setdefault(chat_key, asyncio.Lock())
        if lock.locked():
            logger.info("AI: chat=%s already processing — skip", chat_id)
            return
        async with lock:
            await self._do_ai_reply(event, chat_info, idle_sec, chat_key)

    async def _handle_brain_chat_writeback(self, event):
        """Сообщение в брейн-чате → попытка записать факт в knowledge/ через GitHub.

        Игнорим:
          - сообщения юзербота (включая [AI-LOG])
          - команды (начинающиеся с /)
          - пустые / служебные
          - если writeback выключен в админке
        """
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        # свои логи и реплики не процессим
        if self._me and event.sender_id == self._me.id:
            return
        if text.startswith("[AI-LOG]"):
            return
        if text.startswith("/"):
            return  # команды бота — не наша епархия здесь
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
                await event.reply(
                    "⚠️ GITHUB_TOKEN не задан в env — writeback в граф невозможен."
                )
            except Exception:
                pass
        elif status == "classify_fail":
            await storage.bump_writeback_stats(errors=1)
            try:
                await event.reply("⚠️ Claude не смог разобрать сообщение в JSON.")
            except Exception:
                pass

    async def _do_ai_reply(self, event, chat_info: dict, idle_sec: int, chat_key: str):
        """Внутренняя часть AI-ответа: задержка набора, fetch контекста, Claude, send."""
        chat_id = event.chat_id

        # Имитация набора: случайная пауза перед запросом
        delay = random.uniform(config.AI_TYPING_DELAY_MIN, config.AI_TYPING_DELAY_MAX)
        try:
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(delay)
        except Exception:
            await asyncio.sleep(delay)

        # Повторная проверка worker activity (мог написать пока спали)
        if idle_sec > 0 and time.time() - self._last_worker_ts.get(chat_key, 0) < idle_sec:
            await storage.bump_ai_stats(skipped_worker_active=1)
            logger.info("AI: chat=%s — worker came in during typing delay, skip", chat_id)
            return

        # Сборка истории и brain_notes
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

        # Контекст клиента — AI должен знать его username для tools
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

        # Tool executor — bind chat_id и id текущего сообщения клиента (для линка
        # в эскалации). last_msg_id может быть None если message нет (служебка).
        last_msg_id = getattr(getattr(event, "message", None), "id", None)
        async def _executor(name, inp):
            return await self._execute_ai_tool(
                name, inp, chat_id=chat_id, last_msg_id=last_msg_id
            )

        # Запрос в Claude (с tools — может вызвать инструменты автоматически)
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

        # Отправка ответа
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

        # Лог в brain_chat (если задан)
        client_text = (event.message.text or "").strip()
        await self._log_to_brain(
            chat_id=chat_id,
            chat_info=chat_info,
            client_text=client_text,
            ai_text=reply,
            usage=usage,
        )

    async def _fetch_history_for_claude(self, chat_id, client_id: int) -> list[dict]:
        """Считывает последние config.AI_HISTORY_LIMIT сообщений и форматирует под API.

        Чужие сообщения (от не-клиента и не-юзербота) идут как user с префиксом имени —
        чтобы Claude видел контекст разговора, но не путался в ролях.
        """
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

        # Claude API требует, чтобы первое сообщение было role=user.
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        # И последнее тоже должно быть user (мы только что получили клиентское).
        while msgs and msgs[-1]["role"] != "user":
            msgs.pop()
        return msgs

    async def _fetch_brain_notes(self) -> str:
        """Свежие заметки админа из brain_chat. AI-логи (с маркером) пропускаем."""
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
                    continue  # это наши же логи — пропускаем
                ts = m.date.strftime("%Y-%m-%d %H:%M") if m.date else ""
                parts.insert(0, f"[{ts}] {txt}")
        except Exception as e:
            logger.warning("brain notes fetch failed: %s", e)
            return ""
        return "\n".join(parts)

    async def _log_to_brain(
        self, chat_id, chat_info: dict, client_text: str, ai_text: str, usage: dict
    ):
        """Пишет [AI-LOG] запись в brain_chat для аудита админа."""
        bid = storage.get_brain_chat_id()
        if not bid:
            return
        client_name = chat_info.get("client_name") or "—"
        in_t = usage.get("input_tokens", 0)
        out_t = usage.get("output_tokens", 0)
        # Обрезаем длинные тексты, чтобы не забивать brain_chat
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
    # Имя бота для CRM-флоу зашито здесь; легко вынести в config/storage если
    # появятся другие интеграции.
    CRM_BOT_USERNAME = "PrideCONTROLE_bot"

    async def _execute_ai_tool(
        self, tool_name: str, tool_input: dict, chat_id, last_msg_id=None
    ) -> dict:
        """Диспетчер AI-инструментов. Возвращает dict {status: ok|error, ...}."""
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
        return {"status": "error", "error": f"unknown_tool:{tool_name}"}

    async def _tool_add_partner_to_crm(self, chat_id, client_username: str) -> dict:
        """Tool: подключить @PrideCONTROLE_bot и отправить '+партнер @username'.

        Шаги:
          1. Резолв CRM-бота
          2. InviteToChannelRequest (если уже участник — игнорим UserAlreadyParticipantError)
          3. EditAdminRequest — выдаём права (не фатально если не получилось)
          4. send_message '+партнер @username'
        """
        username = (client_username or "").lstrip("@").strip()
        if not username:
            return {"status": "error", "error": "client_username_empty"}

        # 1. Резолв бота
        try:
            bot_entity = await self.client.get_entity(self.CRM_BOT_USERNAME)
        except UsernameNotOccupiedError:
            return {"status": "error", "step": "resolve", "error": "crm_bot_not_found"}
        except Exception as e:
            return {"status": "error", "step": "resolve", "error": str(e)}

        # 2. Инвайт в чат
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
            # Может быть уже в чате но другая ошибка — продолжим, дальше увидим
            logger.warning("CRM invite warning: %s", e)
            invite_status = f"warn:{type(e).__name__}"

        # 3. Админка (best effort — если не получилось, шаг 4 всё равно может пройти)
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

        # 4. Команда боту
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

    async def _tool_escalate_to_team(
        self,
        work_chat_id,
        last_msg_id,
        specialist: str,
        reason: str,
        client_question: str,
    ) -> dict:
        """Tool: пишет в координаторскую беседу формат эскалации.

        Шаги:
          1. Resolve coordination_chat_id из storage. Если 0 — error.
          2. Валидация specialist (whitelist).
          3. Строим t.me/c-ссылки на сообщение и беседу (private supergroup).
          4. Берём имя клиента из managed_chats[chat_id].client_name.
          5. send_message в координаторский чат.
          6. Бампим escalate_stats.
        """
        coord_id = storage.get_coordination_chat_id()
        if not coord_id:
            return {"status": "error", "error": "coordination_chat_not_set"}

        allowed = {"TimonSkupCL", "pride_sys01", "pride_manager1"}
        spec = (specialist or "").lstrip("@").strip()
        if spec not in allowed:
            return {"status": "error", "error": f"unknown_specialist:{spec}"}

        # Линки t.me/c/<bare_chat_id>/<msg_id>. _norm_chat_id снимает -100 префикс.
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
        return {
            "status": "ok",
            "specialist": spec,
            "coord_chat": coord_id,
            "msg_link": msg_link,
        }

    async def stop(self):
        await self.client.disconnect()

    async def create_work_chat(self, client_name: str, client_id: int = 0) -> dict:
        title = config.CHAT_TITLE_TEMPLATE.format(client_name=client_name)
        about = config.CHAT_DESCRIPTION_TEMPLATE.format(client_name=client_name)

        # 1. Создаём супергруппу
        result = await self.client(
            CreateChannelRequest(title=title, about=about, megagroup=True)
        )
        channel = result.chats[0]
        logger.info("Created group '%s' (id=%s)", title, channel.id)

        # 2. Регистрируем чат для welcome-флоу
        if client_id:
            await storage.register_chat(channel.id, client_id, client_name)

        # 3. Резолвим работников из текущего списка storage
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

        # 4. Инвайтим работников по одному
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
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                statuses[uname_or_id] = f"ошибка: {e}"

        # Логируем статусы инвайтов в Railway logs
        for u, s in statuses.items():
            logger.info("invite chat=%s @%s -> %s", channel.id, u, s)

        # 5. Делаем юзербота админом (чтобы мог отправлять сообщения)
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

        # 6. Экспортируем invite link
        invite = await self.client(ExportChatInviteRequest(channel))

        # 7. Fallback watcher в фоне.
        # _welcome_locks[channel.id] гарантирует, что только один из двух путей отправит welcome.
        if client_id:
            asyncio.create_task(self._watch_for_client_join(channel, client_id))

        return {
            "chat_id": channel.id,
            "title": title,
            "invite_link": invite.link,
            "statuses": statuses,
        }

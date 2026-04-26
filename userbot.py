"""Userbot service: creates supergroups, invites workers, sends welcome on client join.

Welcome delivery has two channels:
1. Realtime: events.ChatAction handler (fires when Telegram pushes us the join event)
2. Fallback: a per-chat polling task that polls participants every 3s for up to 10 min,
   sends welcome as soon as the expected client_id appears.
Whichever fires first wins; the other no-ops via storage.welcome_sent flag.
"""
import logging
import asyncio

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

import config
from storage import storage

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

    async def start(self):
        await self.client.start(phone=config.USERBOT_PHONE)
        self._me = await self.client.get_me()
        logger.info(
            "Userbot started: %s (@%s, id=%s)",
            self._me.first_name, self._me.username, self._me.id,
        )

        @self.client.on(events.ChatAction)
        async def _on_chat_action(event):
            try:
                await self._handle_chat_action(event)
            except Exception as e:
                logger.warning("ChatAction handler error: %s", e)

    async def _handle_chat_action(self, event):
        # Verbose debug — see exactly what events arrive
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
        """Send welcome message; idempotent via storage.welcome_sent flag."""
        info = storage.get_chat_info(chat_id)
        if not info or info.get("welcome_sent"):
            return False
        # Slight delay to ensure client sees the chat fully
        await asyncio.sleep(1)
        # Re-check (another path may have just sent)
        info = storage.get_chat_info(chat_id)
        if not info or info.get("welcome_sent"):
            return False
        welcome = storage.get_welcome()
        entities_raw = storage.get_welcome_entities()
        try:
            if entities_raw:
                # Custom-emoji / formatting present — send single message with entities.
                # (entities offsets are anchored to full text; splitting would corrupt them)
                ents = _entities_to_telethon(entities_raw)
                await self.client.send_message(chat_id, welcome, formatting_entities=ents)
            else:
                # Plain text — split into chunks if too long
                for chunk in _split_text(welcome, 3900):
                    await self.client.send_message(chat_id, chunk)
                    await asyncio.sleep(0.3)
            await storage.mark_welcome_sent(chat_id)
            logger.info(
                "Welcome sent (source=%s, entities=%d, len=%d) to chat=%s for client=%s",
                source, len(entities_raw), len(welcome), chat_id, expected_client_id,
            )
            return True
        except Exception as e:
            logger.warning(
                "Welcome send failed (source=%s, chat=%s): %s",
                source, chat_id, e,
            )
            return False

    async def _watch_for_client_join(self, channel, client_id: int, timeout_sec: int = 600):
        """Fallback: poll participants of `channel` every 3s for up to `timeout_sec`.
        As soon as `client_id` is in the chat — send welcome (if not already)."""
        deadline = asyncio.get_event_loop().time() + timeout_sec
        try:
            while asyncio.get_event_loop().time() < deadline:
                # Stop early if welcome already sent (e.g. by ChatAction)
                info = storage.get_chat_info(channel.id)
                if info and info.get("welcome_sent"):
                    logger.info(
                        "watch chat=%s: welcome already sent, exiting watcher", channel.id
                    )
                    return
                # Try to fetch participant — Telegram returns ChannelParticipant if present,
                # or raises UserNotParticipantError if not yet
                try:
                    await self.client(GetParticipantRequest(
                        channel=channel,
                        participant=PeerUser(client_id),
                    ))
                    # In chat → send welcome
                    logger.info(
                        "watch chat=%s: client %s joined, sending welcome",
                        channel.id, client_id,
                    )
                    await self._send_welcome(channel.id, client_id, source="poll")
                    return
                except UserNotParticipantError:
                    pass  # not yet — keep polling
                except FloodWaitError as e:
                    logger.warning("watch chat=%s flood wait %s s", channel.id, e.seconds)
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

    async def stop(self):
        await self.client.disconnect()

    async def create_work_chat(self, client_name: str, client_id: int = 0) -> dict:
        title = config.CHAT_TITLE_TEMPLATE.format(client_name=client_name)
        about = config.CHAT_DESCRIPTION_TEMPLATE.format(client_name=client_name)

        # 1. Create supergroup
        result = await self.client(
            CreateChannelRequest(title=title, about=about, megagroup=True)
        )
        channel = result.chats[0]
        logger.info("Created group '%s' (id=%s)", title, channel.id)

        # 2. Register chat for welcome flow
        if client_id:
            await storage.register_chat(channel.id, client_id, client_name)

        # 3. Resolve workers from current storage list
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
            except Exception as e:
                statuses[uname] = f"ошибка резолва: {e}"

        # 4. Invite workers one by one
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
                statuses[uname_or_id] = f"flood wait {e.seconds}s"
            except Exception as e:
                statuses[uname_or_id] = f"ошибка: {e}"

        # Log invite statuses so we can debug from Railway logs
        for u, s in statuses.items():
            logger.info("invite chat=%s @%s -> %s", channel.id, u, s)

        # 5. Make userbot admin (so it can send welcome)
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

        # 6. Export invite link
        invite = await self.client(ExportChatInviteRequest(channel))

        # 7. Fire-and-forget fallback watcher in background — wins if events.ChatAction
        # never delivers the join (which Telegram sometimes does for join-by-link)
        if client_id:
            asyncio.create_task(self._watch_for_client_join(channel, client_id))

        return {
            "chat_id": channel.id,
            "title": title,
            "invite_link": invite.link,
            "statuses": statuses,
        }

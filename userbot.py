"""Userbot service: creates supergroups, invites workers, sends welcome on client join."""
import logging
import asyncio

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    EditAdminRequest,
)
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import ChatAdminRights
from telethon.errors import (
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    FloodWaitError,
    PeerFloodError,
    UsernameNotOccupiedError,
)

import config
from storage import storage

logger = logging.getLogger(__name__)


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
        if not (event.user_joined or event.user_added):
            return
        info = storage.get_chat_info(event.chat_id)
        if not info or info.get("welcome_sent"):
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
            return

        # Slight delay to ensure client sees the chat fully
        await asyncio.sleep(1)
        welcome = storage.get_welcome()
        try:
            await self.client.send_message(event.chat_id, welcome)
            await storage.mark_welcome_sent(event.chat_id)
            logger.info("Welcome sent to chat=%s for client=%s", event.chat_id, expected)
        except Exception as e:
            logger.warning("Welcome send failed (chat=%s): %s", event.chat_id, e)

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

        return {
            "chat_id": channel.id,
            "title": title,
            "invite_link": invite.link,
            "statuses": statuses,
        }

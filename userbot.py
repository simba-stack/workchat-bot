"""
Userbot service on Telethon.
Creates supergroup, adds workers, returns invite link.
"""
import logging
from typing import Optional

from telethon import TelegramClient
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

logger = logging.getLogger(__name__)


class UserbotService:
    """Обёртка над Telethon-клиентом для создания рабочих бесед."""

    def __init__(self):
        # Если задан STRING_SESSION (cloud) — используем его, иначе файл-сессию.
        if config.STRING_SESSION:
            session = StringSession(config.STRING_SESSION)
        else:
            session = "userbot_session"
        self.client = TelegramClient(
            session,
            config.API_ID,
            config.API_HASH,
        )
        self._me = None

    async def start(self):
        """Запускает userbot. При первом запуске (без StringSession) спросит код из Telegram."""
        await self.client.start(phone=config.USERBOT_PHONE)
        self._me = await self.client.get_me()
        logger.info(
            "Userbot started: %s (@%s, id=%s)",
            self._me.first_name,
            self._me.username,
            self._me.id,
        )

    async def stop(self):
        await self.client.disconnect()

    async def create_work_chat(self, client_name: str) -> dict:
        """
        Создаёт супергруппу и добавляет туда работников.
        Возвращает: {chat_id, title, invite_link, statuses}.
        """
        title = config.CHAT_TITLE_TEMPLATE.format(client_name=client_name)
        about = config.CHAT_DESCRIPTION_TEMPLATE.format(client_name=client_name)

        # 1. Создаём супергруппу
        result = await self.client(
            CreateChannelRequest(
                title=title,
                about=about,
                megagroup=True,
            )
        )
        channel = result.chats[0]
        logger.info("Created group '%s' (id=%s)", title, channel.id)

        # 2. Резолвим username работников в entity
        statuses: dict[str, str] = {}
        users_to_invite = []
        for username in config.WORKERS:
            uname = username.lstrip("@").strip()
            if not uname:
                continue
            try:
                entity = await self.client.get_entity(uname)
                users_to_invite.append(entity)
                statuses[uname] = "найден"
            except UsernameNotOccupiedError:
                statuses[uname] = "не существует"
            except Exception as e:
                statuses[uname] = f"ошибка резолва: {e}"

        # 3. Добавляем по одному (чтобы не падать на первом запрете)
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

        # 4. Делаем userbot админом (опционально)
        if config.USERBOT_AS_ADMIN and self._me:
            try:
                rights = ChatAdminRights(
                    change_info=True,
                    post_messages=True,
                    edit_messages=True,
                    delete_messages=True,
                    ban_users=True,
                    invite_users=True,
                    pin_messages=True,
                    add_admins=False,
                    anonymous=False,
                    manage_call=True,
                )
                await self.client(
                    EditAdminRequest(
                        channel=channel,
                        user_id=self._me,
                        admin_rights=rights,
                        rank="Owner",
                    )
                )
            except Exception as e:
                logger.warning("Could not grant admin rights to userbot: %s", e)

        # 5. Выдаём invite-ссылку
        invite = await self.client(ExportChatInviteRequest(channel))
        invite_link = invite.link

        return {
            "chat_id": channel.id,
            "title": title,
            "invite_link": invite_link,
            "statuses": statuses,
        }


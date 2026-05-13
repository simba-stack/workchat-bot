"""Outreach Department — массовая рассылка через несколько юзерботов.

Архитектура:
  • OutreachManager — singleton, держит pool TelegramClient-инстансов.
  • Auth flow — phone → send_code → sign_in(code) → возможно 2FA → session.
  • Campaign worker — async loop, отправляет сообщения по таргетам с rate-limit
    и jitter, использует round-robin по доступным ботам.
  • Reply handler — событие NewMessage от любого юзербота в чатах рассылки
    → AI-классификатор (Haiku) → если interested → пересылка менеджеру.

Storage ключи: outreach_bots, outreach_campaigns, outreach_messages,
outreach_responses, outreach_pending_auth.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from typing import Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    PhoneCodeInvalidError, SessionPasswordNeededError,
    FloodWaitError, PhoneNumberInvalidError, UserDeactivatedBanError,
    PhoneCodeExpiredError,
)

import config
from storage import storage
import event_bus

logger = logging.getLogger(__name__)


class OutreachManager:
    """Глобальный менеджер юзерботов рассылки + кампаний."""

    def __init__(self):
        # bot_id -> TelegramClient
        self.clients: dict[int, TelegramClient] = {}
        # campaign_id -> asyncio.Task
        self.campaign_tasks: dict[int, asyncio.Task] = {}
        # phone -> {client, phone_code_hash} для активной auth-сессии
        self.auth_clients: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # === Bot lifecycle ===

    async def connect_bot(self, bot: dict) -> Optional[TelegramClient]:
        """Подключает бота по сохранённой session_string. Регистрирует
        обработчик входящих сообщений (для reply handler)."""
        if not bot.get("session_string"):
            return None
        bot_id = int(bot["id"])
        if bot_id in self.clients:
            return self.clients[bot_id]
        try:
            client = TelegramClient(
                StringSession(bot["session_string"]),
                config.API_ID, config.API_HASH,
            )
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("outreach bot %s session expired", bot_id)
                return None
            me = await client.get_me()
            await storage.update_outreach_bot(
                bot_id,
                tg_user_id=me.id, tg_username=me.username or "",
                status="active",
            )
            self.clients[bot_id] = client
            # Регистрируем reply handler
            client.add_event_handler(
                self._make_reply_handler(bot_id),
                events.NewMessage(incoming=True),
            )
            logger.info("outreach bot %s connected: @%s", bot_id, me.username)
            event_bus.emit_event(
                "outreach-bot-online",
                {"bot_id": bot_id, "username": me.username or "", "phone": bot.get("phone")},
                character="outreach", severity="success",
            )
            return client
        except Exception as e:
            logger.warning("outreach connect failed bot=%s: %s", bot_id, e)
            await storage.update_outreach_bot(bot_id, status="error")
            return None

    async def disconnect_bot(self, bot_id: int):
        client = self.clients.pop(int(bot_id), None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def connect_all(self):
        """Подключает все боты из storage. Зови при старте приложения."""
        for bot in storage.list_outreach_bots():
            await self.connect_bot(bot)

    # === Auth flow ===

    async def start_auth(self, phone: str) -> dict:
        """Шаг 1: запрашиваем SMS-код. Возвращает {phone_code_hash, sent}."""
        phone = phone.strip()
        if not phone.startswith("+"):
            phone = "+" + phone
        try:
            client = TelegramClient(
                StringSession(), config.API_ID, config.API_HASH,
            )
            await client.connect()
            try:
                sent = await client.send_code_request(phone)
            except PhoneNumberInvalidError:
                await client.disconnect()
                return {"ok": False, "error": "phone_invalid"}
            self.auth_clients[phone] = {
                "client": client,
                "phone_code_hash": sent.phone_code_hash,
                "ts": time.time(),
            }
            await storage.set_pending_auth(phone, {
                "phone_code_hash": sent.phone_code_hash,
            })
            return {"ok": True, "phone": phone, "code_sent": True}
        except FloodWaitError as e:
            return {"ok": False, "error": f"flood_wait_{e.seconds}s"}
        except Exception as e:
            logger.warning("start_auth %s failed: %s", phone, e)
            return {"ok": False, "error": str(e)[:200]}

    async def confirm_code(self, phone: str, code: str, password: str = "") -> dict:
        """Шаг 2: подтверждаем код. Если включена 2FA — нужен password."""
        phone = phone.strip()
        if not phone.startswith("+"):
            phone = "+" + phone
        ent = self.auth_clients.get(phone)
        if not ent:
            return {"ok": False, "error": "no_pending_auth"}
        client: TelegramClient = ent["client"]
        try:
            try:
                await client.sign_in(
                    phone=phone, code=code,
                    phone_code_hash=ent["phone_code_hash"],
                )
            except SessionPasswordNeededError:
                if not password:
                    return {"ok": False, "error": "2fa_required"}
                await client.sign_in(password=password)
            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                return {"ok": False, "error": "code_invalid"}
            me = await client.get_me()
            session_string = client.session.save()
            # Сохраняем бота
            bot = await storage.add_outreach_bot(
                phone=phone, session_string=session_string,
                name=me.first_name or "",
                tg_user_id=me.id, tg_username=me.username or "",
                status="active",
            )
            # Регистрируем reply handler
            client.add_event_handler(
                self._make_reply_handler(bot["id"]),
                events.NewMessage(incoming=True),
            )
            self.clients[bot["id"]] = client
            # Чистим pending
            self.auth_clients.pop(phone, None)
            await storage.clear_pending_auth(phone)
            event_bus.emit_event(
                "outreach-bot-added",
                {"bot_id": bot["id"], "username": me.username or "", "phone": phone},
                character="outreach", severity="success",
            )
            return {"ok": True, "bot": bot}
        except UserDeactivatedBanError:
            return {"ok": False, "error": "account_banned"}
        except Exception as e:
            logger.warning("confirm_code %s failed: %s", phone, e)
            return {"ok": False, "error": str(e)[:200]}

    # === Send loop ===

    def _can_send(self, bot: dict, campaign: dict) -> tuple[bool, str]:
        """Проверка может ли бот сейчас отправлять. Возвращает (ok, reason)."""
        now = time.time()
        if int(bot.get("flood_wait_until", 0) or 0) > now:
            return False, "flood_wait"
        if bot.get("status") not in ("active", None):
            return False, "inactive"
        # Active hours
        try:
            hour = datetime.now().hour
            if not (
                int(campaign.get("active_hours_from", 9))
                <= hour
                < int(campaign.get("active_hours_to", 21))
            ):
                return False, "outside_hours"
        except Exception:
            pass
        # Rate per hour
        rate = int(campaign.get("rate_per_hour", 20))
        last = float(bot.get("last_send_ts", 0) or 0)
        min_gap = 3600.0 / max(1, rate)
        if (now - last) < min_gap * 0.6:  # запас
            return False, "rate_limit"
        return True, ""

    def _pick_bot(self, campaign: dict) -> Optional[tuple[int, dict]]:
        """Выбирает доступного бота. Round-robin по созданию + проверки."""
        bots = storage.list_outreach_bots()
        random.shuffle(bots)
        for b in bots:
            if int(b["id"]) not in self.clients:
                continue
            ok, _ = self._can_send(b, campaign)
            if ok:
                return int(b["id"]), b
        return None

    async def _send_one(
        self, bot_id: int, client: TelegramClient,
        campaign: dict, target,
    ) -> dict:
        """Отправка одного сообщения. Возвращает {status, error?}."""
        try:
            entity = await client.get_entity(target)
            text = campaign.get("text") or ""
            sent_msg = await client.send_message(entity, text, link_preview=False)
            now = time.time()
            await storage.update_outreach_bot(
                bot_id,
                last_send_ts=now,
                sent_today=int(
                    (storage.get_outreach_bot(bot_id) or {}).get("sent_today", 0)
                ) + 1,
            )
            await storage.add_outreach_message(
                campaign_id=campaign["id"], bot_id=bot_id,
                target_chat_id=target,
                status="sent",
                tg_message_id=getattr(sent_msg, "id", 0),
            )
            await storage.update_outreach_campaign(
                campaign["id"], stats={"sent": 1},
            )
            event_bus.emit_event(
                "outreach-sent", {
                    "campaign_id": campaign["id"], "bot_id": bot_id,
                    "target": str(target),
                }, character="outreach",
            )
            return {"status": "sent"}
        except FloodWaitError as e:
            until = time.time() + e.seconds + 5
            await storage.update_outreach_bot(bot_id, flood_wait_until=until)
            await storage.add_outreach_message(
                campaign_id=campaign["id"], bot_id=bot_id,
                target_chat_id=target, status="flood_wait",
                error=f"FloodWait {e.seconds}s",
            )
            return {"status": "flood_wait", "wait": e.seconds}
        except Exception as e:
            await storage.add_outreach_message(
                campaign_id=campaign["id"], bot_id=bot_id,
                target_chat_id=target, status="failed",
                error=str(e)[:200],
            )
            await storage.update_outreach_campaign(
                campaign["id"], stats={"errors": 1},
            )
            return {"status": "failed", "error": str(e)}

    async def _run_campaign(self, campaign_id: int):
        """Главный цикл кампании. Идёт по targets, отправляет с задержками."""
        logger.info("outreach campaign %s started", campaign_id)
        try:
            while True:
                storage.reload_sync()
                campaign = storage.get_outreach_campaign(campaign_id)
                if not campaign:
                    break
                if campaign.get("status") != "running":
                    break

                targets = campaign.get("targets") or []
                # Фильтруем те куда уже отправили
                pending = [
                    t for t in targets
                    if not storage.was_target_sent(campaign_id, t)
                ]
                if not pending:
                    await storage.update_outreach_campaign(
                        campaign_id, status="done", finished_at=time.time(),
                    )
                    event_bus.emit_event(
                        "outreach-campaign-done",
                        {"campaign_id": campaign_id},
                        character="outreach", severity="success",
                    )
                    break

                bot_pick = self._pick_bot(campaign)
                if not bot_pick:
                    # Никто не может отправлять — ждём 30 сек
                    await asyncio.sleep(30)
                    continue
                bot_id, bot = bot_pick
                client = self.clients[bot_id]

                target = pending[0]
                await self._send_one(bot_id, client, campaign, target)

                # Jitter
                jmin = int(campaign.get("jitter_min_sec", 90))
                jmax = int(campaign.get("jitter_max_sec", 240))
                await asyncio.sleep(random.uniform(jmin, jmax))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("campaign %s crashed: %s", campaign_id, e)
        finally:
            self.campaign_tasks.pop(campaign_id, None)
            logger.info("outreach campaign %s loop ended", campaign_id)

    async def start_campaign(self, campaign_id: int) -> bool:
        if campaign_id in self.campaign_tasks:
            return False
        ok = await storage.update_outreach_campaign(
            campaign_id, status="running", started_at=time.time(),
        )
        if not ok:
            return False
        task = asyncio.create_task(self._run_campaign(campaign_id))
        self.campaign_tasks[campaign_id] = task
        return True

    async def pause_campaign(self, campaign_id: int) -> bool:
        ok = await storage.update_outreach_campaign(campaign_id, status="paused")
        if campaign_id in self.campaign_tasks:
            self.campaign_tasks[campaign_id].cancel()
        return ok

    async def stop_campaign(self, campaign_id: int) -> bool:
        ok = await storage.update_outreach_campaign(
            campaign_id, status="done", finished_at=time.time(),
        )
        if campaign_id in self.campaign_tasks:
            self.campaign_tasks[campaign_id].cancel()
        return ok

    # === Reply handler ===

    def _make_reply_handler(self, bot_id: int):
        async def handler(event):
            try:
                await self._handle_incoming(bot_id, event)
            except Exception as e:
                logger.warning("reply handler bot=%s: %s", bot_id, e)
        return handler

    async def _handle_incoming(self, bot_id: int, event):
        """Когда юзербот получает сообщение — проверяем, было ли в этом чате
        наше объявление; если да — классифицируем через AI и сохраняем."""
        chat_id = event.chat_id
        text = (event.message and event.message.text) or ""
        if not text:
            return
        # Найдём кампанию, в которой мы отправляли в этот чат
        campaign_id = None
        for m in storage.list_outreach_messages(limit=2000):
            if (str(m.get("target_chat_id")) == str(chat_id)
                    and int(m.get("bot_id", 0)) == int(bot_id)
                    and m.get("status") == "sent"):
                campaign_id = m.get("campaign_id")
                break
        if not campaign_id:
            return  # не наш чат

        # Игнорируем своё же сообщение
        try:
            client = self.clients.get(bot_id)
            me = await client.get_me() if client else None
            if me and event.sender_id == me.id:
                return
        except Exception:
            pass

        sender_username = ""
        try:
            sender = await event.get_sender()
            sender_username = getattr(sender, "username", "") or ""
        except Exception:
            pass

        # AI classifier (Haiku)
        intent = await self._classify_intent(text)
        resp = await storage.add_outreach_response(
            campaign_id=campaign_id, bot_id=bot_id,
            from_chat_id=chat_id, from_user_id=event.sender_id,
            from_username=sender_username,
            text=text[:1000], ai_intent=intent,
        )
        await storage.update_outreach_campaign(
            campaign_id, stats={"replied": 1},
        )
        event_bus.emit_event(
            "outreach-reply", {
                "campaign_id": campaign_id,
                "from": sender_username or f"id{event.sender_id}",
                "intent": intent,
                "short": text[:100],
            }, character="outreach",
            severity="success" if intent == "interested" else "info",
        )

        if intent == "interested":
            await self._transfer_to_manager(bot_id, resp, event)

    async def _classify_intent(self, text: str) -> str:
        """Haiku-классификатор: interested / spam / junk / already_client / unclear."""
        try:
            from anthropic import AsyncAnthropic
            if not config.ANTHROPIC_API_KEY:
                return "unclear"
            cli = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
            prompt = (
                "Текст сообщения от человека, ответившего на наше объявление о "
                "выкупе расчётных счетов ИП/ООО.\n"
                "Классифицируй intent:\n"
                "- interested = хочет узнать подробнее / готов продать счёт\n"
                "- spam = реклама / попытка втюхать что-то нам\n"
                "- junk = троллинг / оскорбление / off-topic\n"
                "- already_client = уже работали с нами / упомянул что клиент\n"
                "- unclear = непонятно, нейтрально\n\n"
                "Сообщение:\n"
                f"{text[:500]}\n\n"
                "Ответь ОДНИМ словом: interested / spam / junk / already_client / unclear"
            )
            resp = await cli.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            ans = (resp.content[0].text if resp.content else "").strip().lower()
            for valid in ("interested", "spam", "junk", "already_client", "unclear"):
                if valid in ans:
                    return valid
            return "unclear"
        except Exception as e:
            logger.warning("intent classify failed: %s", e)
            return "unclear"

    async def _transfer_to_manager(self, bot_id: int, resp: dict, event):
        """Пересылает интересного клиента менеджеру кампании."""
        campaign = storage.get_outreach_campaign(resp["campaign_id"])
        if not campaign:
            return
        manager = (campaign.get("manager_username") or "").lstrip("@").strip()
        if not manager:
            return
        client = self.clients.get(bot_id)
        if not client:
            return
        try:
            mgr_entity = await client.get_entity(manager)
            sender_uname = resp.get("from_username") or ""
            sender_tag = f"@{sender_uname}" if sender_uname else f"id {resp.get('from_user_id')}"
            chat_link = ""
            try:
                msg = event.message
                if msg and getattr(msg, "id", None):
                    chat_link = f"https://t.me/c/{abs(resp['from_chat_id'])}/{msg.id}"
            except Exception:
                pass
            forward_text = (
                f"🎯 Outreach (кампания «{campaign.get('name')}»)\n"
                f"Заинтересованный: {sender_tag}\n"
                f"Чат: {resp.get('from_chat_id')}\n"
                + (f"Ссылка: {chat_link}\n" if chat_link else "")
                + f"\nЕго ответ:\n«{resp.get('text', '')[:400]}»\n\n"
                f"Свяжись с ним и переведи в работу."
            )
            await client.send_message(mgr_entity, forward_text, link_preview=False)
            await storage.mark_outreach_response(
                resp["id"], handled=True,
                transferred_to=manager, transfer_ts=time.time(),
            )
            await storage.update_outreach_campaign(
                resp["campaign_id"], stats={"transferred": 1},
            )
            event_bus.emit_event(
                "outreach-transferred", {
                    "campaign_id": resp["campaign_id"],
                    "manager": manager,
                    "from": sender_uname or str(resp.get("from_user_id")),
                }, character="outreach", severity="success",
            )
        except Exception as e:
            logger.warning("transfer failed: %s", e)


# Singleton
manager = OutreachManager()

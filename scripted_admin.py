"""
scripted_admin.py — управление скриптованными текстами через юзербот
в отдельной группе-админке.

ЗАЧЕМ: Bot API (aiogram) фильтрует premium emoji entities и сжимает фото
через свой файловый пайплайн. Юзербот (Telethon с Premium-аккаунтом) видит
и получает всё 1:1, поэтому редактирование скриптов делаем через него,
а не через @PRIDE_CRM_BOT.

КАК:
1. SIMBA создаёт группу, добавляет Асика (юзербот)
2. Пишет в группе /setup — Асик запоминает chat_id + owner_id (в storage)
3. Дальше команды: /list, /show KEY, /edit KEY, /reset KEY, /help, /cancel

/edit KEY переводит в режим ожидания — следующее сообщение в этой группе
(текст, фото, premium emoji) сохраняется как новый скрипт через
storage.set_scripted_text(). Клиентам юзербот шлёт через _send_scripted()
1:1 (уже написан в userbot.py).
"""

import logging
import os
import time

from telethon import events
from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityUnderline,
    MessageEntityStrike,
    MessageEntityCode,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityBlockquote,
    MessageEntityTextUrl,
    MessageEntityCustomEmoji,
)

logger = logging.getLogger(__name__)

# in-memory: user_id -> key. Ждём следующее сообщение как контент для этого key.
# Не переживёт рестарт юзербота (5 сек лаг — юзер повторит /edit).
_pending_edit: dict = {}


HELP_TEXT = (
    "**Асик Админка · команды**\n\n"
    "/list — список всех скриптов\n"
    "/show KEY — показать текущий (перепост 1:1 с premium emoji и фото)\n"
    "/edit KEY — заменить: следующим сообщением пришли новый текст/фото/emoji\n"
    "/reset KEY — сбросить на дефолт из config.SCRIPTED_TEXTS_DEFAULTS\n"
    "/cancel — отменить ожидание /edit\n"
    "/help — эта справка\n\n"
    "Пример: `/edit welcome` → присылаешь картинку с подписью + premium emoji "
    "→ Асик сохраняет → рассылает клиентам 1:1."
)


def _telethon_entities_to_dict(entities) -> list:
    """Telethon MessageEntity[] -> JSON-совместимый list of dict.
    Формат совпадает с тем что читает userbot._entities_to_telethon()."""
    if not entities:
        return []
    out = []
    for e in entities:
        try:
            off = int(e.offset)
            ln = int(e.length)
        except Exception:
            continue
        item = {"offset": off, "length": ln}
        if isinstance(e, MessageEntityCustomEmoji):
            item["type"] = "custom_emoji"
            item["custom_emoji_id"] = str(getattr(e, "document_id", "") or "")
        elif isinstance(e, MessageEntityBold):
            item["type"] = "bold"
        elif isinstance(e, MessageEntityItalic):
            item["type"] = "italic"
        elif isinstance(e, MessageEntityUnderline):
            item["type"] = "underline"
        elif isinstance(e, MessageEntityStrike):
            item["type"] = "strikethrough"
        elif isinstance(e, MessageEntityCode):
            item["type"] = "code"
        elif isinstance(e, MessageEntityPre):
            item["type"] = "pre"
            lang = getattr(e, "language", "") or ""
            if lang:
                item["language"] = lang
        elif isinstance(e, MessageEntityBlockquote):
            item["type"] = "blockquote"
        elif isinstance(e, MessageEntitySpoiler):
            item["type"] = "spoiler"
        elif isinstance(e, MessageEntityTextUrl):
            item["type"] = "text_link"
            item["url"] = getattr(e, "url", "") or ""
        else:
            continue
        out.append(item)
    return out


async def _cmd_help(event):
    await event.reply(HELP_TEXT, parse_mode="markdown")


async def _cmd_list(event, storage):
    items = storage.list_scripted_texts()
    if not items:
        await event.reply("Скриптов нет — config.SCRIPTED_TEXTS_DEFAULTS пуст.")
        return
    lines = ["**Скрипты сообщений:**", ""]
    for it in items:
        mark = "✏️" if it.get("is_custom") else "🔹"
        title = it.get("title") or it["key"]
        lines.append(f"{mark} `{it['key']}` — {title}")
    lines.append("")
    lines.append("✏️ кастомный · 🔹 дефолт")
    lines.append("Команды: /show KEY, /edit KEY, /reset KEY")
    await event.reply("\n".join(lines), parse_mode="markdown")


async def _cmd_show(event, storage, client, key: str):
    data = storage.get_scripted_text(key)
    if not data:
        await event.reply(
            f"Скрипт `{key}` не найден. Проверь /list.",
            parse_mode="markdown",
        )
        return
    text = data.get("text") or ""
    entities_raw = data.get("entities") or []
    photo_path = data.get("photo_path") or None
    is_default = data.get("is_default", False)
    title = data.get("title") or key

    from userbot import _entities_to_telethon
    ents = _entities_to_telethon(entities_raw) if entities_raw else None

    header = (
        f"**{title}**  `{key}`\n"
        f"{'🔹 Дефолт' if is_default else '✏️ Кастомный'} · "
        f"entities={len(entities_raw)} · "
        f"photo={'да' if photo_path else 'нет'}"
    )
    await event.reply(header, parse_mode="markdown")

    if photo_path and os.path.isfile(photo_path):
        try:
            await client.send_file(
                event.chat_id, photo_path,
                caption=text, formatting_entities=ents,
            )
            return
        except Exception as e:
            logger.warning("[scripted_admin] show send_file failed key=%s: %s", key, e)

    if ents:
        try:
            await client.send_message(event.chat_id, text, formatting_entities=ents)
        except Exception:
            await client.send_message(event.chat_id, text)
    else:
        await client.send_message(event.chat_id, text)


async def _cmd_edit(event, storage, key: str):
    all_keys = {it["key"] for it in storage.list_scripted_texts()}
    if key not in all_keys:
        await event.reply(
            f"Ключ `{key}` неизвестен. Проверь /list.",
            parse_mode="markdown",
        )
        return
    _pending_edit[event.sender_id] = key
    await event.reply(
        f"Ок, жду сообщение для `{key}`.\n"
        f"Пришли следующим: **текст** (можно с картинкой в подписи, "
        f"premium emoji, форматированием).\n"
        f"Отмена — /cancel",
        parse_mode="markdown",
    )


async def _cmd_reset(event, storage, key: str):
    existed = await storage.reset_scripted_text(key)
    if existed:
        await event.reply(f"✅ `{key}` сброшен на дефолт.", parse_mode="markdown")
    else:
        await event.reply(
            f"`{key}` уже был на дефолте (кастомной версии нет).",
            parse_mode="markdown",
        )


async def _cmd_cancel(event):
    if event.sender_id in _pending_edit:
        key = _pending_edit.pop(event.sender_id)
        await event.reply(f"Отменил /edit {key}.")
    else:
        await event.reply("Нечего отменять.")


async def _handle_edit_content(event, storage, client, key: str):
    """Сохраняет содержимое присланного сообщения как новый скрипт."""
    msg = event.message
    # У Telethon caption == msg.message (единое поле). msg.text может быть None
    # если сообщение — только медиа, но msg.message в этом случае содержит caption.
    text = msg.message or ""
    entities = list(msg.entities or [])

    photo_path = None
    if msg.photo:
        try:
            import config as _cfg
            base_dir = os.path.dirname(
                os.path.abspath(
                    getattr(_cfg, "STORAGE_PATH", "") or "/app/data/state.json"
                )
            )
            media_dir = os.path.join(base_dir, "scripted_media")
            os.makedirs(media_dir, exist_ok=True)
            ts = int(time.time())
            target = os.path.join(media_dir, f"{key}_{ts}.jpg")
            saved = await client.download_media(msg, file=target)
            if saved:
                photo_path = saved if isinstance(saved, str) else target
                size = os.path.getsize(photo_path) if os.path.isfile(photo_path) else -1
                logger.info(
                    "[scripted_admin] photo saved key=%s path=%s size=%d",
                    key, photo_path, size,
                )
        except Exception as e:
            logger.warning(
                "[scripted_admin] photo download failed key=%s: %s", key, e,
            )
            photo_path = None

    if not text.strip() and not photo_path:
        await event.reply(
            "❌ Сообщение пустое (ни текста, ни фото). "
            "Пришли ещё раз или /cancel."
        )
        return

    ents_dict = _telethon_entities_to_dict(entities)

    default_title = ""
    try:
        import config as _cfg2
        defaults = getattr(_cfg2, "SCRIPTED_TEXTS_DEFAULTS", {}) or {}
        if key in defaults:
            default_title = defaults[key].get("title", key)
    except Exception:
        pass

    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        pass
    updated_by = (
        (getattr(sender, "username", "") or str(event.sender_id))
        if sender else str(event.sender_id)
    )

    saved = await storage.set_scripted_text(
        key=key,
        text=text,
        entities=ents_dict,
        updated_by=updated_by,
        title=default_title,
        photo_path=photo_path,
    )
    _pending_edit.pop(event.sender_id, None)

    ce_count = sum(1 for e in ents_dict if e.get("type") == "custom_emoji")
    logger.info(
        "[scripted_admin] saved key=%s by=%s entities=%d ce=%d text_len=%d photo=%s",
        key, updated_by, saved, ce_count, len(text), bool(photo_path),
    )

    await event.reply(
        f"✅ Сохранил `{key}`\n"
        f"• текст: {len(text)} символов\n"
        f"• entities: {saved} (custom emoji: {ce_count})\n"
        f"• фото: {'да' if photo_path else 'нет'}\n\n"
        f"Проверить: /show {key}",
        parse_mode="markdown",
    )


async def register(client, storage):
    """Подключает handler к юзерботу. Вызывается из UserbotService.start()."""

    @client.on(events.NewMessage())
    async def _handler(event):
        try:
            if not event.is_group:
                return

            text = (event.raw_text or "").strip()
            admin_cfg = storage.get_scripted_admin() or {}
            saved_chat = admin_cfg.get("chat_id")
            saved_owner = admin_cfg.get("owner_id")

            # /setup — работает только если админка ещё не настроена
            if text == "/setup":
                if saved_chat:
                    if event.chat_id == saved_chat:
                        await event.reply(
                            f"Уже настроено. chat_id={saved_chat}, "
                            f"owner_id={saved_owner}.\nПиши /help."
                        )
                    return
                try:
                    parts = await client.get_participants(event.chat_id)
                    if len(parts) > 2:
                        await event.reply(
                            f"❌ В группе {len(parts)} участников. "
                            f"Оставь только себя и меня, потом /setup."
                        )
                        return
                except Exception as e:
                    logger.warning("[scripted_admin] get_participants failed: %s", e)
                await storage.set_scripted_admin(
                    chat_id=int(event.chat_id),
                    owner_id=int(event.sender_id),
                )
                logger.info(
                    "[scripted_admin] SETUP done chat_id=%s owner_id=%s",
                    event.chat_id, event.sender_id,
                )
                await event.reply(
                    f"✅ Асик Админка настроена.\n"
                    f"chat_id: `{event.chat_id}`\n"
                    f"owner_id: `{event.sender_id}`\n\n"
                    f"Пиши /help для списка команд.",
                    parse_mode="markdown",
                )
                return

            # Всё остальное — только в настроенной группе от owner
            if not saved_chat or event.chat_id != saved_chat:
                return
            if event.sender_id != saved_owner:
                return

            # Ждём контент для /edit
            if event.sender_id in _pending_edit and not text.startswith("/"):
                key = _pending_edit[event.sender_id]
                await _handle_edit_content(event, storage, client, key)
                return

            # Команды
            if text in ("/help", "/start"):
                await _cmd_help(event)
            elif text == "/list":
                await _cmd_list(event, storage)
            elif text.startswith("/show"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /show KEY (см. /list)")
                    return
                await _cmd_show(event, storage, client, parts[1].strip())
            elif text.startswith("/edit"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /edit KEY (см. /list)")
                    return
                await _cmd_edit(event, storage, parts[1].strip())
            elif text.startswith("/reset"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /reset KEY (см. /list)")
                    return
                await _cmd_reset(event, storage, parts[1].strip())
            elif text == "/cancel":
                await _cmd_cancel(event)
        except Exception as e:
            logger.exception("[scripted_admin] handler crashed: %s", e)

    logger.info("[scripted_admin] handler registered")

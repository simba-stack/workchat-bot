"""
scripted_admin.py — управление скриптованными текстами через юзербот
в отдельной группе-админке.

Команды (в группе-админке, только от owner_id):
  /setup       — одноразовая настройка chat_id + owner_id
  /help        — справка
  /list        — все ключи скриптов
  /show KEY    — перепост текущего скрипта 1:1
  /edit KEY    — заменить: следующим сообщением пришли новый контент
  /reset KEY   — сбросить кастомный на дефолт (для ключей из config)
  /cancel      — отменить ожидание /edit
  /add KEY "Title"          — создать новый пустой ключ + сразу /edit
  /delete KEY               — удалить кастомный ключ (нельзя для дефолтных)
  /rename OLD NEW           — переименовать кастомный ключ
  /export                   — Асик пришлёт JSON-файл со всеми scripted_texts
  /import                   — reply на JSON + /import → восстановить
  /preview KEY              — dry-run с dummy-подстановкой placeholder'ов

Placeholder'ы (подставляются в _send_scripted при отправке клиенту):
  {bank} {fio} {deal_id} {price_usdt} {usdt_address}
  {client_tag} {client_username}
"""

import json
import logging
import os
import re
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
_pending_edit: dict = {}

# Validation regex для ключей
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# Dummy-подстановка для /preview
_DUMMY_VARS = {
    "bank": "Альфа",
    "fio": "Иванов Иван Иванович",
    "deal_id": "12345",
    "price_usdt": "0.85",
    "usdt_address": "TX7abc9DummyAddressForPreview1234",
    "client_tag": "@simba",
    "client_username": "simba",
}


HELP_TEXT = (
    "**Асик Админка · команды**\n\n"
    "**Просмотр:**\n"
    "/list — все ключи (📷 = есть фото)\n"
    "/show KEY — показать текущий (перепост 1:1)\n"
    "/preview KEY — dry-run с dummy-переменными\n\n"
    "**Редактирование:**\n"
    "/edit KEY — заменить: следующим сообщением пришли текст/фото/emoji\n"
    "/reset KEY — сбросить на дефолт (только для ключей из config)\n"
    "/cancel — отменить ожидание /edit\n\n"
    "**Управление ключами:**\n"
    "/add KEY \"Заголовок\" — создать новый ключ + /edit\n"
    "/delete KEY — удалить кастомный ключ полностью\n"
    "/rename OLD NEW — переименовать кастомный ключ\n\n"
    "**Бэкап:**\n"
    "/export — прислать JSON со всеми scripted_texts\n"
    "/import — reply на JSON + команда → восстановить\n\n"
    "**Placeholder\\'ы** (подставляются при отправке клиенту):\n"
    "`{bank}` `{fio}` `{deal_id}` `{price_usdt}` `{usdt_address}` "
    "`{client_tag}` `{client_username}`\n\n"
    "Пример: `/edit reply_status_perevyaz` → `✅ ЛК {bank} для {fio} перевязан. "
    "Deal #{deal_id}.`"
)


def _telethon_entities_to_dict(entities) -> list:
    """Telethon MessageEntity[] -> JSON-совместимый list of dict."""
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


# ═══════════════════════════════════════════════════════════════════════
# Утилиты
# ═══════════════════════════════════════════════════════════════════════

def _fmt_list_line(item: dict) -> str:
    mark = "✏️" if item.get("is_custom") else "🔹"
    photo = " 📷" if item.get("has_photo") else ""
    title = item.get("title") or item["key"]
    return f"{mark} `{item['key']}`{photo} — {title}"


async def _cmd_help(event):
    await event.reply(HELP_TEXT, parse_mode="markdown")


async def _cmd_list(event, storage):
    items = storage.list_scripted_texts()
    if not items:
        await event.reply("Скриптов нет — config.SCRIPTED_TEXTS_DEFAULTS пуст.")
        return
    lines = ["**Скрипты сообщений:**", ""]
    for it in items:
        lines.append(_fmt_list_line(it))
    lines.append("")
    lines.append("✏️ кастомный · 🔹 дефолт · 📷 с фото")
    lines.append("Команды: /show KEY, /edit KEY, /reset KEY, /add KEY, /delete KEY")
    await event.reply("\n".join(lines), parse_mode="markdown")


async def _cmd_show(event, storage, client, key: str):
    data = storage.get_scripted_text(key)
    if not data:
        await event.reply(f"Скрипт `{key}` не найден. Проверь /list.", parse_mode="markdown")
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
            await client.send_file(event.chat_id, photo_path,
                                    caption=text, formatting_entities=ents)
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
            f"Ключ `{key}` неизвестен. Проверь /list или создай через /add.",
            parse_mode="markdown",
        )
        return
    _pending_edit[event.sender_id] = key
    await event.reply(
        f"Ок, жду сообщение для `{key}`.\n"
        f"Пришли: **текст** (можно с картинкой в подписи, premium emoji, "
        f"форматированием, placeholder'ами вида `{{bank}}` `{{fio}}`).\n"
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


# ── /add /delete /rename ────────────────────────────────────────────

async def _cmd_add(event, storage, args: str):
    """Формат: /add KEY "Заголовок с пробелами"  или  /add KEY Title-без-кавычек"""
    # Парсим args: KEY + опционально title в кавычках или после первого пробела
    m = re.match(r'^([a-z0-9_]+)\s+"([^"]+)"\s*$', args)
    if not m:
        m = re.match(r'^([a-z0-9_]+)\s+(.+?)\s*$', args)
    if not m:
        await event.reply(
            'Формат: `/add KEY "Заголовок"` или `/add KEY Заголовок`',
            parse_mode="markdown",
        )
        return
    key, title = m.group(1), m.group(2).strip()
    if not _KEY_RE.match(key):
        await event.reply(
            f"❌ Ключ `{key}` неправильный. Формат: строчные латинские, "
            f"цифры, `_`; 2–64 символа; начинается с буквы.",
            parse_mode="markdown",
        )
        return
    if len(title) > 200:
        await event.reply("❌ Заголовок слишком длинный (макс 200).")
        return

    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        pass
    updated_by = (getattr(sender, "username", "") or str(event.sender_id)) if sender else str(event.sender_id)

    ok = await storage.add_scripted_text(key=key, title=title, updated_by=updated_by)
    if not ok:
        await event.reply(
            f"❌ Ключ `{key}` уже существует (в custom или defaults). "
            f"Используй /edit {key} для замены или /rename для переименования.",
            parse_mode="markdown",
        )
        return

    # Сразу переводим в edit-режим
    _pending_edit[event.sender_id] = key
    await event.reply(
        f"✅ Создан пустой ключ `{key}` («{title}»).\n\n"
        f"Теперь пришли **следующим сообщением** контент "
        f"(текст/фото/premium emoji/placeholder'ы). Отмена — /cancel.",
        parse_mode="markdown",
    )
    logger.info("[scripted_admin] add key=%s by=%s", key, updated_by)


async def _cmd_delete(event, storage, key: str):
    result = await storage.delete_scripted_text(key)
    defaults_check = key in (getattr(__import__("config"), "SCRIPTED_TEXTS_DEFAULTS", {}) or {})
    if defaults_check:
        await event.reply(
            f"❌ `{key}` — дефолтный ключ (из config). Используй `/reset {key}` "
            f"чтобы вернуть на дефолт вместо удаления.",
            parse_mode="markdown",
        )
        return
    if not result:
        await event.reply(
            f"Ключ `{key}` не найден в custom.", parse_mode="markdown",
        )
        return
    await event.reply(f"✅ Кастомный ключ `{key}` удалён полностью.", parse_mode="markdown")
    logger.info("[scripted_admin] deleted key=%s", key)


async def _cmd_rename(event, storage, args: str):
    parts = args.split()
    if len(parts) != 2:
        await event.reply(
            "Формат: `/rename OLD NEW` — оба ключа без пробелов",
            parse_mode="markdown",
        )
        return
    old_key, new_key = parts[0], parts[1]
    if not _KEY_RE.match(new_key):
        await event.reply(
            f"❌ Новое имя `{new_key}` неправильное. Формат: строчные латинские, "
            f"цифры, `_`; 2–64 символа; начинается с буквы.",
            parse_mode="markdown",
        )
        return
    result = await storage.rename_scripted_text(old_key, new_key)
    if result == "ok":
        await event.reply(
            f"✅ `{old_key}` → `{new_key}`.", parse_mode="markdown",
        )
        logger.info("[scripted_admin] renamed %s → %s", old_key, new_key)
    elif result == "not_found":
        await event.reply(f"Ключ `{old_key}` не найден в custom.", parse_mode="markdown")
    elif result == "default_forbidden":
        await event.reply(
            f"❌ `{old_key}` — дефолтный ключ. Дефолты переименовать нельзя.",
            parse_mode="markdown",
        )
    elif result == "target_exists":
        await event.reply(
            f"❌ Ключ `{new_key}` уже существует.", parse_mode="markdown",
        )


# ── /export /import ──────────────────────────────────────────────────

async def _cmd_export(event, storage, client):
    data = storage.export_scripted_texts()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"scripted_texts_{ts}.json"
    try:
        import io
        buf = io.BytesIO(payload.encode("utf-8"))
        buf.name = filename
        await client.send_file(
            event.chat_id, buf,
            caption=f"📦 Экспорт scripted_texts ({len(data.get('scripted_texts') or {})} ключей). "
                    f"Сохрани как бэкап. Восстановление: reply → /import",
            attributes=[],
        )
        logger.info("[scripted_admin] exported %d keys", len(data.get("scripted_texts") or {}))
    except Exception as e:
        logger.exception("[scripted_admin] export failed: %s", e)
        await event.reply(f"❌ Ошибка экспорта: {e}")


async def _cmd_import(event, storage, client):
    reply = await event.get_reply_message()
    if not reply or not reply.document:
        await event.reply(
            "Формат: сделай reply на JSON-файл (полученный через /export) + напиши /import"
        )
        return
    try:
        raw = await client.download_media(reply, file=bytes)
        if not raw:
            await event.reply("❌ Не удалось скачать файл.")
            return
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        await event.reply(f"❌ Не валидный JSON: {e}")
        return
    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        pass
    updated_by = (getattr(sender, "username", "") or str(event.sender_id)) if sender else str(event.sender_id)
    count = await storage.import_scripted_texts(data, updated_by=updated_by)
    await event.reply(
        f"✅ Импортировано ключей: **{count}**. Проверь /list.",
        parse_mode="markdown",
    )
    logger.info("[scripted_admin] imported %d keys by=%s", count, updated_by)


# ── /preview ─────────────────────────────────────────────────────────

async def _cmd_preview(event, storage, client, key: str):
    data = storage.get_scripted_text(key)
    if not data:
        await event.reply(f"Ключ `{key}` не найден.", parse_mode="markdown")
        return
    text = data.get("text") or ""
    entities_raw = data.get("entities") or []
    photo_path = data.get("photo_path") or None

    # Подставляем dummy-переменные
    substituted = text
    used_placeholders = []
    for k, v in _DUMMY_VARS.items():
        marker = "{" + k + "}"
        if marker in substituted:
            used_placeholders.append(k)
            substituted = substituted.replace(marker, str(v))

    header_lines = [
        f"**Preview `{key}`** — dry-run с dummy-переменными",
    ]
    if used_placeholders:
        header_lines.append(f"Подставлены: {', '.join('`{'+p+'}`' for p in used_placeholders)}")
    else:
        header_lines.append("Placeholder'ов нет — текст без подстановки.")
    await event.reply("\n".join(header_lines), parse_mode="markdown")

    # Отправляем как получит клиент. NB: entities могут сдвинуться если placeholder
    # изменил длину — это OK для preview, задача — показать логику.
    from userbot import _entities_to_telethon
    ents = _entities_to_telethon(entities_raw) if entities_raw else None
    if photo_path and os.path.isfile(photo_path):
        try:
            await client.send_file(event.chat_id, photo_path,
                                    caption=substituted, formatting_entities=ents)
            return
        except Exception as e:
            logger.warning("[scripted_admin] preview send_file failed: %s", e)
    if ents:
        try:
            await client.send_message(event.chat_id, substituted, formatting_entities=ents)
        except Exception:
            await client.send_message(event.chat_id, substituted)
    else:
        await client.send_message(event.chat_id, substituted)


# ── /edit content handler (сохранение) ───────────────────────────────

async def _handle_edit_content(event, storage, client, key: str):
    msg = event.message
    text = msg.message or ""
    entities = list(msg.entities or [])

    photo_path = None
    if msg.photo:
        try:
            import config as _cfg
            base_dir = os.path.dirname(
                os.path.abspath(getattr(_cfg, "STORAGE_PATH", "") or "/app/data/state.json")
            )
            media_dir = os.path.join(base_dir, "scripted_media")
            os.makedirs(media_dir, exist_ok=True)
            ts = int(time.time())
            target = os.path.join(media_dir, f"{key}_{ts}.jpg")
            saved = await client.download_media(msg, file=target)
            if saved:
                photo_path = saved if isinstance(saved, str) else target
                size = os.path.getsize(photo_path) if os.path.isfile(photo_path) else -1
                logger.info("[scripted_admin] photo saved key=%s path=%s size=%d",
                            key, photo_path, size)
        except Exception as e:
            logger.warning("[scripted_admin] photo download failed key=%s: %s", key, e)
            photo_path = None

    if not text.strip() and not photo_path:
        await event.reply("❌ Сообщение пустое. Пришли ещё раз или /cancel.")
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
    # Если ключ кастомный (не в дефолтах) — сохраняем title из уже существующей записи
    if not default_title:
        existing = storage.get_scripted_text(key) or {}
        default_title = existing.get("title") or key

    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        pass
    updated_by = (getattr(sender, "username", "") or str(event.sender_id)) if sender else str(event.sender_id)

    saved = await storage.set_scripted_text(
        key=key, text=text, entities=ents_dict,
        updated_by=updated_by, title=default_title, photo_path=photo_path,
    )
    _pending_edit.pop(event.sender_id, None)

    ce_count = sum(1 for e in ents_dict if e.get("type") == "custom_emoji")
    # Найдём placeholder'ы использованные в тексте
    found_placeholders = re.findall(r"\{([a-z_][a-z0-9_]*)\}", text)
    logger.info("[scripted_admin] saved key=%s by=%s entities=%d ce=%d text_len=%d photo=%s placeholders=%s",
                key, updated_by, saved, ce_count, len(text), bool(photo_path), found_placeholders)

    ph_line = ""
    if found_placeholders:
        ph_line = f"\n• placeholder\\'ы: {', '.join('`{'+p+'}`' for p in set(found_placeholders))}"

    await event.reply(
        f"✅ Сохранил `{key}`\n"
        f"• текст: {len(text)} символов\n"
        f"• entities: {saved} (custom emoji: {ce_count})\n"
        f"• фото: {'да' if photo_path else 'нет'}"
        f"{ph_line}\n\n"
        f"Проверить: /show {key} или /preview {key}",
        parse_mode="markdown",
    )


# ═══════════════════════════════════════════════════════════════════════
# Регистрация handler'а
# ═══════════════════════════════════════════════════════════════════════

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
                logger.info("[scripted_admin] SETUP done chat_id=%s owner_id=%s",
                            event.chat_id, event.sender_id)
                await event.reply(
                    f"✅ Асик Админка настроена.\n"
                    f"chat_id: `{event.chat_id}`\n"
                    f"owner_id: `{event.sender_id}`\n\n"
                    f"Пиши /help.",
                    parse_mode="markdown",
                )
                return

            # Всё остальное — только в настроенной группе от owner
            if not saved_chat or event.chat_id != saved_chat:
                return
            if event.sender_id != saved_owner:
                return

            # Ждём контент для /edit (или для /add → edit)
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
                    await event.reply("Формат: /show KEY")
                    return
                await _cmd_show(event, storage, client, parts[1].strip())
            elif text.startswith("/edit"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /edit KEY")
                    return
                await _cmd_edit(event, storage, parts[1].strip())
            elif text.startswith("/reset"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /reset KEY")
                    return
                await _cmd_reset(event, storage, parts[1].strip())
            elif text == "/cancel":
                await _cmd_cancel(event)
            elif text.startswith("/add"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply('Формат: /add KEY "Заголовок"')
                    return
                await _cmd_add(event, storage, parts[1].strip())
            elif text.startswith("/delete"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /delete KEY")
                    return
                await _cmd_delete(event, storage, parts[1].strip())
            elif text.startswith("/rename"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /rename OLD NEW")
                    return
                await _cmd_rename(event, storage, parts[1].strip())
            elif text == "/export":
                await _cmd_export(event, storage, client)
            elif text == "/import":
                await _cmd_import(event, storage, client)
            elif text.startswith("/preview"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.reply("Формат: /preview KEY")
                    return
                await _cmd_preview(event, storage, client, parts[1].strip())
        except Exception as e:
            logger.exception("[scripted_admin] handler crashed: %s", e)

    logger.info("[scripted_admin] handler registered")

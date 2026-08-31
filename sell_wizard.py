"""
sell_wizard.py v2 — полный клиентский визард ЦРМ v2.

Флоу:
  1) Материал: [🧾 ИП/ООО] | [💳 Дебет (stub)]  → sw:mat:IP / DEBET
  2) Банк (только ИП): [АЛЬФА] [ОЗОН] [РАЙФ]     → sw:b:ALFA / OZON / RAIF
  3) Проверка ЛК: инструкция per bank + буфер uploads (фото/видео/текст)
       кнопки: [📤 На проверку] [◀️ Назад] [❌ Отмена]
       → пересылаем пачку в verification_group + [✅ Пропустить] / [❌ Отклонить]
  4) Approved → payment_prompt + [🤝 Гарант] [💸 USDT]
       Гарант → инструкция + [✅ Создал сделку] → просит номер сделки →
       forward в guarantor_group + [💰 Пополнено]
       USDT → payment_usdt_prompt (переход к CRM LK-form)
  5) [💰 Пополнено] callback → уведомляем клиента + запрос CRM LK-form
     (пока просто dashboard_command open_lk_form; полноценно в Волне C)

State в storage.state["client_sell_flow"][str(chat_id)]:
  step: material | bank | verification_upload | verification_pending |
        payment_choice | guarantor_wait_deal | guarantor_wait_deposit |
        usdt_done | done
  material: IP | DEBET
  bank: ALFA | OZON | RAIF
  price: float
  method: GUARANTOR_BEFORE_WORK | USDT_TRC20
  uploads: [{type: photo|video|text|document, file_id, caption}]
  deal_number: str
  verification_msg_id: int (id сообщения в verification_group)
  guarantor_msg_id: int (id сообщения в guarantor_group)

Регистрируется из userbot.start() → sell_wizard.register(client, storage, self).
Message hook (uploads/deal_number) вызывается из userbot._on_new_message
через handle_managed_chat_message(userbot, storage, event) ДО AI-handler.
"""

import logging
import time
from typing import Optional, List

from telethon import events, Button

from storage import _lock as _storage_lock

logger = logging.getLogger(__name__)

CB_PREFIX = b"sw"
WIZARD_TIMEOUT_SEC = 60 * 60  # 1 час на весь визард

# Разрешённые банки для ИП (SIMBA — только 3):
# SIMBA 2026-08: расширил список банков.
# Убрали Тинькофф (не берём тинь-бизнес), добавили СБЕР, ВТБ, ББР, Газпром.
ALLOWED_BANKS_IP = ("ALFA", "OZON", "RAIF", "SBER", "VTB", "BBR", "GAZ")
BANK_TITLES = {
    "ALFA": "АЛЬФА",
    "OZON": "ОЗОН",
    "RAIF": "РАЙФ",
    "SBER": "СБЕР",
    "VTB":  "ВТБ",
    "BBR":  "ББР",
    "GAZ":  "ГАЗПРОМ",
}
# Соответствие sw:b:XXX → scripted key дефолтной инструкции проверки
VERIFICATION_SCRIPTED_KEY = {
    "ALFA": "verification_alfa",
    "OZON": "verification_ozon",
    "RAIF": "verification_raif",
}

METHOD_LABELS = {
    "GUARANTOR_BEFORE_WORK": "🤝 Гарант в Continental",
    "USDT_TRC20": "💸 USDT TRC20",
}


# ═══════════════════════ STATE HELPERS ═══════════════════════

def get_flow(storage, chat_id) -> dict:
    return storage.get_sell_flow(chat_id)


async def set_flow(storage, chat_id, **patch) -> dict:
    return await storage.set_sell_flow(chat_id, **patch)


async def clear_flow(storage, chat_id) -> None:
    await storage.clear_sell_flow(chat_id)


def _is_stale(flow: dict) -> bool:
    try:
        return (time.time() - float(flow.get("updated_ts") or 0)) > WIZARD_TIMEOUT_SEC
    except Exception:
        return False


def is_client_in_flow(storage, chat_id, step: Optional[str] = None) -> bool:
    flow = storage.get_sell_flow(chat_id)
    if not flow:
        return False
    if _is_stale(flow):
        return False
    if step:
        return flow.get("step") == step
    return True


# ═══════════════════════ SCRIPTED HELPERS ═══════════════════════

def _scripted_text(storage, key: str, default: str = "", **placeholders) -> str:
    """Тянет заскриптованный текст, применяет placeholder-подстановку."""
    data = storage.get_scripted_text(key) or {}
    text = (data.get("text") or default or "").strip()
    if not text:
        return ""
    for k, v in placeholders.items():
        text = text.replace("{" + k + "}", str(v if v is not None else ""))
    return text


# ═══════════════════════ KEYBOARDS ═══════════════════════

def _material_kb():
    return [
        [Button.inline("🧾 ИП / ООО", b"sw:mat:IP")],
        [Button.inline("💳 Дебет", b"sw:mat:DEBET")],
        [Button.inline("❌ Отмена", b"sw:x")],
    ]


def _bank_kb_ip(storage):
    """3 кнопки: АЛЬФА / ОЗОН / РАЙФ с ценами из storage.pricing или дефолт."""
    rows = []
    row = []
    for bank_key in ALLOWED_BANKS_IP:
        # Цена: сначала pricing, потом DEFAULT_LK_PRICES
        title = BANK_TITLES[bank_key]
        price = 0
        try:
            price = storage.resolve_lk_price(title, 0)
        except Exception:
            pass
        if price > 0:
            label = f"{title} · {int(price)}$"
        else:
            label = title
        row.append(Button.inline(label, f"sw:b:{bank_key}".encode("utf-8")))
    rows.append(row)
    rows.append([Button.inline("◀️ Назад", b"sw:back:material"),
                 Button.inline("❌ Отмена", b"sw:x")])
    return rows


def _debet_stub_kb():
    return [[Button.inline("◀️ Назад", b"sw:back:material")]]


def _verification_upload_kb(uploads_count: int):
    check_lbl = f"📤 На проверку ({uploads_count})" if uploads_count else "📤 На проверку"
    rows = [[Button.inline(check_lbl, b"sw:sendcheck")]]
    rows.append([Button.inline("◀️ Назад к банку", b"sw:back:bank"),
                 Button.inline("❌ Отмена", b"sw:x")])
    return rows


def _verification_review_kb(client_chat_id):
    """Кнопки в verification_group для проверяющих."""
    cid = str(client_chat_id).encode("utf-8")
    return [
        [Button.inline("✅ Пропустить на вяз", b"sw:vok:" + cid)],
        [Button.inline("❌ Отклонить", b"sw:vno:" + cid)],
    ]


def _payment_kb():
    return [
        [Button.inline("🤝 Гарант в Continental", b"sw:pay:GUAR")],
        [Button.inline("💸 USDT TRC20 (после работы)", b"sw:pay:USDT")],
        [Button.inline("❌ Отмена", b"sw:x")],
    ]


def _guarantor_created_kb():
    return [
        [Button.inline("✅ Создал сделку — ввести номер", b"sw:dealready")],
        [Button.inline("❌ Отмена", b"sw:x")],
    ]


def _guarantor_review_kb(client_chat_id):
    """Кнопка в guarantor_group для бухгалтера."""
    cid = str(client_chat_id).encode("utf-8")
    return [[Button.inline("💰 Пополнено", b"sw:gok:" + cid)]]


# ═══════════════════════ SCREEN RENDERERS ═══════════════════════

async def _render_material(userbot, storage, chat_id, edit_event=None):
    text = _scripted_text(storage, "sell_material_prompt",
                          default="Какой материал сдаёте?")
    kb = _material_kb()
    if edit_event:
        try:
            await edit_event.edit(text, parse_mode="html", buttons=kb)
            return
        except Exception:
            pass
    msg = await userbot.client.send_message(chat_id, text, parse_mode="html", buttons=kb)
    await set_flow(storage, chat_id, wizard_msg_id=int(msg.id))


async def _render_bank(userbot, storage, chat_id, edit_event=None):
    text = _scripted_text(storage, "sell_bank_prompt",
                          default="Выберите банк:")
    kb = _bank_kb_ip(storage)
    if edit_event:
        try:
            await edit_event.edit(text, parse_mode="html", buttons=kb)
            return
        except Exception:
            pass
    msg = await userbot.client.send_message(chat_id, text, parse_mode="html", buttons=kb)
    await set_flow(storage, chat_id, wizard_msg_id=int(msg.id))


async def _render_debet_stub(userbot, storage, chat_id, edit_event=None):
    text = _scripted_text(storage, "sell_debet_stub",
                          default="🚧 По дебету дорабатывается. Следите за новостями.")
    kb = _debet_stub_kb()
    if edit_event:
        try:
            await edit_event.edit(text, parse_mode="html", buttons=kb)
            return
        except Exception:
            pass
    await userbot.client.send_message(chat_id, text, parse_mode="html", buttons=kb)


async def _render_verification(userbot, storage, chat_id, edit_event=None):
    """Инструкция проверки для банка (из verification_config или дефолт scripted)."""
    flow = get_flow(storage, chat_id)
    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    price = float(flow.get("price") or 0)
    uploads = flow.get("uploads") or []

    # Приоритет: verification_config (админка ЦРМ) → verification_<bank> scripted
    cfg = storage.get_verification_config(bank_title) or {}
    text = (cfg.get("text") or "").strip()
    if not text:
        scripted_key = VERIFICATION_SCRIPTED_KEY.get(bank_key)
        if scripted_key:
            text = _scripted_text(storage, scripted_key, default="")
    if not text:
        text = f"🛡 Проверка ЛК {bank_title}\n\nПришлите пруф в чат и нажмите «📤 На проверку»."

    header = f"💰 <b>Цена:</b> <b>{int(price)}$</b>\n\n" if price > 0 else ""
    footer = ""
    if uploads:
        types_count = {}
        for u in uploads:
            t = u.get("type") or "?"
            types_count[t] = types_count.get(t, 0) + 1
        parts = []
        if types_count.get("photo"):
            parts.append(f"фото×{types_count['photo']}")
        if types_count.get("video"):
            parts.append(f"видео×{types_count['video']}")
        if types_count.get("document"):
            parts.append(f"файлов×{types_count['document']}")
        if types_count.get("text"):
            parts.append(f"текст×{types_count['text']}")
        footer = "\n\n📎 <b>В буфере:</b> " + ", ".join(parts)

    full_text = header + text + footer
    kb = _verification_upload_kb(len(uploads))
    if edit_event:
        try:
            await edit_event.edit(full_text, parse_mode="html", buttons=kb)
            return
        except Exception:
            pass
    msg = await userbot.client.send_message(chat_id, full_text, parse_mode="html", buttons=kb)
    await set_flow(storage, chat_id, wizard_msg_id=int(msg.id),
                   step="verification_upload")


async def _render_payment(userbot, storage, chat_id):
    """Показ выбора способа расчёта после verification_approved."""
    # Approval-текст + payment-меню
    approve = _scripted_text(storage, "verification_approved",
                             default="✅ Проверка пройдена!")
    payment_text_override = storage.get_payment_text("after_verification")
    if payment_text_override:
        prompt = payment_text_override
    else:
        prompt = _scripted_text(storage, "payment_prompt",
                                default="Как удобнее получить оплату?")

    full = approve + "\n\n" + prompt
    msg = await userbot.client.send_message(
        chat_id, full, parse_mode="html", buttons=_payment_kb(),
    )
    await set_flow(storage, chat_id, step="payment_choice",
                   wizard_msg_id=int(msg.id))


async def _render_guarantor_instruction(userbot, storage, chat_id, edit_event=None):
    flow = get_flow(storage, chat_id)
    price = float(flow.get("price") or 0)
    override = storage.get_payment_text("guarantor")
    if override:
        text = override.replace("{price}", str(int(price)))
    else:
        text = _scripted_text(
            storage, "payment_guarantor_instruction",
            default="Создайте сделку в @PRIDE_BUHGALTERIA и жмите «Создал сделку».",
            price=int(price),
        )
    kb = _guarantor_created_kb()
    if edit_event:
        try:
            await edit_event.edit(text, parse_mode="html", buttons=kb)
            return
        except Exception:
            pass
    msg = await userbot.client.send_message(chat_id, text, parse_mode="html", buttons=kb)
    await set_flow(storage, chat_id, step="guarantor_wait_deal_ready",
                   wizard_msg_id=int(msg.id))


# ═══════════════════════ PUBLIC ENTRYPOINT ═══════════════════════

async def start_wizard(userbot, storage, chat_id) -> bool:
    """Открывает первый экран (материал). Вызывается из userbot regex/кнопки."""
    # Мьютим AI на время визарда
    try:
        await storage.mute_chat_ai(chat_id, True)
    except Exception:
        pass
    await set_flow(storage, chat_id,
                   step="material", material="", bank="", price=0,
                   method="", uploads=[], deal_number="",
                   verification_msg_id=0, guarantor_msg_id=0)
    await _render_material(userbot, storage, chat_id)
    logger.info("[sell_wizard v2] opened chat=%s", chat_id)
    return True


# ═══════════════════════ MESSAGE HOOK (uploads / deal_number) ═══════════════════════

async def handle_managed_chat_message(userbot, storage, event) -> bool:
    """Вызывается из userbot._on_new_message ДО AI-handler'а.
    Перехватывает сообщения клиента если он в шаге verification_upload
    или guarantor_wait_deal_number.
    Возвращает True если сообщение обработано (не пускать в AI)."""
    try:
        chat_id = event.chat_id
        flow = storage.get_sell_flow(chat_id)
        if not flow or _is_stale(flow):
            return False
        step = flow.get("step") or ""
        msg = event.message
        if not msg:
            return False

        # Только сообщения от клиента чата (не от бота/оператора)
        try:
            chat_info = storage.get_chat_info(chat_id) or {}
            client_id = int(chat_info.get("client_id") or 0)
            sender_id = int(getattr(msg, "sender_id", 0) or 0)
            if client_id and sender_id and sender_id != client_id:
                return False
        except Exception:
            pass

        # === UPLOAD BUFFER ===
        if step == "verification_upload":
            upload = None
            if getattr(msg, "photo", None):
                upload = {"type": "photo",
                          "msg_id": int(msg.id),
                          "caption": (msg.text or msg.message or "")[:200]}
            elif getattr(msg, "video", None):
                upload = {"type": "video",
                          "msg_id": int(msg.id),
                          "caption": (msg.text or msg.message or "")[:200]}
            elif getattr(msg, "document", None):
                upload = {"type": "document",
                          "msg_id": int(msg.id),
                          "caption": (msg.text or msg.message or "")[:200]}
            elif (msg.text or msg.message or "").strip():
                upload = {"type": "text",
                          "msg_id": int(msg.id),
                          "caption": (msg.text or msg.message or "").strip()[:1000]}
            if upload:
                new_flow = await storage.sell_flow_append_upload(chat_id, upload)
                logger.info("[sell_wizard v2] upload+ chat=%s type=%s total=%d",
                            chat_id, upload["type"], len(new_flow.get("uploads") or []))
                # Тихая реакция на сообщение — не спамим текстом
                try:
                    await msg.react("👍")
                except Exception:
                    pass
                return True

        # === DEAL NUMBER (гарант ветка) ===
        if step == "guarantor_wait_deal_number":
            text = (msg.text or msg.message or "").strip()
            import re as _re
            m = _re.match(r"^\s*#?\s*(\d{3,10})\s*$", text)
            if not m:
                # ждём цифры — тихо игнорим текст (или можно попросить прислать цифры)
                return False
            deal_number = m.group(1)
            await set_flow(storage, chat_id, deal_number=deal_number)
            await _send_deal_to_guarantor(userbot, storage, chat_id, deal_number)
            return True

        return False
    except Exception as e:
        logger.exception("[sell_wizard v2] handle_managed_chat_message failed: %s", e)
        return False


# ═══════════════════════ VERIFICATION FORWARD ═══════════════════════

async def _send_uploads_to_verification(userbot, storage, chat_id):
    """Пересылаем пачку uploads из managed_chat в verification_group + inline-кнопки."""
    flow = get_flow(storage, chat_id)
    uploads = flow.get("uploads") or []
    if not uploads:
        return False, "нет пруфов в буфере"

    vgroup = storage.get_verification_group_id()
    if not vgroup:
        return False, "verification_group_id не задан (админ, задайте в /crm_admin → Проверки)"

    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    price = float(flow.get("price") or 0)
    chat_info = storage.get_chat_info(chat_id) or {}
    client_uname = (chat_info.get("client_username") or "").lstrip("@")
    client_id = chat_info.get("client_id") or 0
    tag = f"@{client_uname}" if client_uname else f"client_id={client_id}"

    # 1. Header-сообщение
    header = (
        f"🛡 <b>Проверка ЛК {bank_title}</b>\n\n"
        f"Клиент: {tag}\n"
        f"Chat: <code>{chat_id}</code>\n"
        f"Цена: <b>{int(price)}$</b>\n"
        f"Пруфов: <b>{len(uploads)}</b>\n\n"
        f"Проверьте вложения ниже и решите:"
    )
    try:
        header_msg = await userbot.client.send_message(
            vgroup, header, parse_mode="html",
        )
    except Exception as e:
        return False, f"send header failed: {e}"

    # 2. Пересылаем uploads
    try:
        msg_ids = [u.get("msg_id") for u in uploads if u.get("msg_id")]
        if msg_ids:
            try:
                await userbot.client.forward_messages(vgroup, msg_ids, chat_id)
            except Exception as _fe:
                logger.warning("[sell_wizard v2] forward failed: %s — trying text only", _fe)
        # Текстовые uploads (без msg_id) — просто перечислим
        text_uploads = [u for u in uploads if u.get("type") == "text" and not u.get("msg_id")]
        for tu in text_uploads:
            await userbot.client.send_message(
                vgroup, f"📝 <i>Текст от клиента:</i>\n{tu.get('caption','')}",
                parse_mode="html",
            )
    except Exception as e:
        logger.warning("[sell_wizard v2] uploads forward exception: %s", e)

    # 3. Кнопки review — под header'ом
    try:
        review_msg = await userbot.client.send_message(
            vgroup, "👇 Решение по проверке:",
            buttons=_verification_review_kb(chat_id),
        )
        await set_flow(storage, chat_id,
                       step="verification_pending",
                       verification_msg_id=int(review_msg.id))
    except Exception as e:
        return False, f"review buttons failed: {e}"

    return True, ""


# ═══════════════════════ GUARANTOR FORWARD ═══════════════════════

async def _send_deal_to_guarantor(userbot, storage, chat_id, deal_number):
    """Пересылаем номер сделки бухгалтеру в guarantor_group + кнопка [Пополнено]."""
    ggroup = storage.get_guarantor_group_id()
    if not ggroup:
        logger.warning("[sell_wizard v2] guarantor_group_id пуст — уведомляем клиента текстом")
        await userbot.client.send_message(
            chat_id,
            f"⚠️ Номер сделки #{deal_number} принят, но группа бухгалтерии не настроена.\n"
            f"Оператор скоро подключится.",
        )
        return

    flow = get_flow(storage, chat_id)
    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    price = float(flow.get("price") or 0)
    chat_info = storage.get_chat_info(chat_id) or {}
    client_uname = (chat_info.get("client_username") or "").lstrip("@")
    tag = f"@{client_uname}" if client_uname else f"client_id={chat_info.get('client_id')}"

    text = (
        f"🤝 <b>Сделка на пополнение гаранта</b>\n\n"
        f"Клиент: {tag}\n"
        f"Chat: <code>{chat_id}</code>\n"
        f"Банк: <b>{bank_title}</b>\n"
        f"Сумма: <b>{int(price)}$</b>\n"
        f"Номер сделки: <b>#{deal_number}</b>\n\n"
        f"Пополните и нажмите «💰 Пополнено» ↓"
    )
    try:
        msg = await userbot.client.send_message(
            ggroup, text, parse_mode="html",
            buttons=_guarantor_review_kb(chat_id),
        )
        await set_flow(storage, chat_id,
                       step="guarantor_wait_deposit",
                       guarantor_msg_id=int(msg.id))
    except Exception as e:
        logger.exception("[sell_wizard v2] guarantor forward failed: %s", e)
        await userbot.client.send_message(
            chat_id,
            f"⚠️ Не удалось отправить сделку #{deal_number} бухгалтеру: {e}. Оператор скоро подключится.",
        )
        return

    # Уведомляем клиента что принято
    ack = _scripted_text(storage, "payment_guarantor_deal_received",
                        default=f"✅ Номер сделки №{deal_number} принят. Ждём пополнения.",
                        deal_number=deal_number)
    try:
        await userbot.client.send_message(chat_id, ack, parse_mode="html")
    except Exception:
        pass


# ═══════════════════════ CRM LK-FORM TRIGGER ═══════════════════════

async def _trigger_crm_lk_form(userbot, storage, chat_id):
    """После успешного гаранта / выбора USDT — просим CRM открыть LK-form.
    Полноценная связка — в Волне C. Пока просто пишем dashboard_command
    и уведомляем клиента текстом что скоро откроется меню."""
    flow = get_flow(storage, chat_id)
    bank_key = (flow.get("bank") or "").upper()
    bank_title = BANK_TITLES.get(bank_key, bank_key)
    method = flow.get("method") or ""
    chat_info = storage.get_chat_info(chat_id) or {}
    client_id = chat_info.get("client_id") or 0
    client_uname = chat_info.get("client_username") or ""

    # Инжект команды в dashboard_commands (CRM-воркер её подхватит по regex).
    # Формат: __open_lk_form|chat=<id>|client=<id>|bank=<TITLE>|price=<N>|method=<M>|deal=<num>
    try:
        cmd = (
            f"__open_lk_form|chat={int(chat_id)}"
            f"|client={int(client_id or 0)}"
            f"|username={client_uname or ''}"
            f"|bank={bank_title}"
            f"|price={float(flow.get('price') or 0)}"
            f"|method={method}"
            f"|deal={flow.get('deal_number') or ''}"
        )
        await storage.enqueue_dashboard_command(cmd, source="sell_wizard")
        logger.info("[sell_wizard v2] LK-form triggered for chat=%s bank=%s",
                    chat_id, bank_title)
    except Exception as e:
        logger.warning("[sell_wizard v2] LK-form trigger failed: %s", e)

    # Финалим визард (клиент дальше работает с ЦРМ-ботом)
    await set_flow(storage, chat_id, step="done")


# ═══════════════════════ CALLBACK HANDLERS ═══════════════════════

async def _cb_cancel(userbot, storage, event):
    await clear_flow(storage, event.chat_id)
    try:
        await storage.mute_chat_ai(event.chat_id, False)
    except Exception:
        pass
    try:
        await event.edit("❌ Оформление отменено. Напишите «продать» чтобы начать заново.")
    except Exception:
        pass
    await event.answer("Отменено")


async def _cb_material(userbot, storage, event, mat: str):
    flow = get_flow(storage, event.chat_id)
    if not flow or _is_stale(flow):
        await event.answer("Сессия истекла.", alert=True)
        await clear_flow(storage, event.chat_id)
        return
    # SEC AUDIT 2026-08: replay `sw:mat:*` из старой клавы после старта
    # проверки/оплаты — сбрасывал uploads, стирал прогресс. Разрешаем
    # только на шагах material/start.
    if flow.get("step") not in ("material", "start", None, ""):
        await event.answer("Уже поздно менять материал — используйте «Отмена».", alert=True)
        return
    await set_flow(storage, event.chat_id, material=mat)
    if mat == "DEBET":
        # SIMBA 2026-08: старый stub «дорабатывается» больше не показываем.
        # Отправляем актуальный reply_debet (ручной режим, менеджер свяжется),
        # тот же ключ что при выборе «2» в welcome-меню. И завершаем flow.
        await set_flow(storage, event.chat_id, step="debet_stub")
        try:
            await userbot._send_scripted(
                event.chat_id, "reply_debet",
                default_text=(
                    "Понял, направление Дебет.\n\n"
                    "🔹 По дебету работаем в ручном режиме — цены и условия "
                    "уточняет менеджер лично."
                ),
            )
        except Exception:
            await _render_debet_stub(userbot, storage, event.chat_id, edit_event=event)
        await event.answer()
        return
    if mat == "IP":
        await set_flow(storage, event.chat_id, step="bank")
        await _render_bank(userbot, storage, event.chat_id, edit_event=event)
        await event.answer()
        return
    await event.answer("Неизвестный материал", alert=True)


async def _cb_bank(userbot, storage, event, bank_key: str):
    flow = get_flow(storage, event.chat_id)
    if not flow or _is_stale(flow):
        await event.answer("Сессия истекла.", alert=True)
        await clear_flow(storage, event.chat_id)
        return
    # SEC AUDIT 2026-08: replay `sw:b:*` после старта верификации сбрасывал
    # uploads=[] — стёр пруфы. Разрешаем только на шагах material/bank.
    if flow.get("step") not in ("material", "bank"):
        await event.answer("Уже поздно менять банк — используйте «Отмена».", alert=True)
        return
    if bank_key not in ALLOWED_BANKS_IP:
        await event.answer("Банк недоступен.", alert=True)
        return
    bank_title = BANK_TITLES[bank_key]
    price = 0
    try:
        price = storage.resolve_lk_price(bank_title, 0)
    except Exception:
        pass
    await set_flow(storage, event.chat_id, bank=bank_key, price=float(price),
                   step="verification_upload", uploads=[])
    await _render_verification(userbot, storage, event.chat_id, edit_event=event)
    await event.answer()


async def _cb_back(userbot, storage, event, target: str):
    flow = get_flow(storage, event.chat_id)
    if not flow or _is_stale(flow):
        await event.answer("Сессия истекла.", alert=True)
        await clear_flow(storage, event.chat_id)
        return
    if target == "material":
        await set_flow(storage, event.chat_id, step="material")
        await _render_material(userbot, storage, event.chat_id, edit_event=event)
    elif target == "bank":
        await set_flow(storage, event.chat_id, step="bank", uploads=[])
        await _render_bank(userbot, storage, event.chat_id, edit_event=event)
    await event.answer()


async def _cb_sendcheck(userbot, storage, event):
    """Клиент нажал «📤 На проверку» — форвардим uploads в verification_group."""
    flow = get_flow(storage, event.chat_id)
    if not flow or _is_stale(flow):
        await event.answer("Сессия истекла.", alert=True)
        return
    uploads = flow.get("uploads") or []
    if not uploads:
        await event.answer("Нет вложений! Пришлите фото/видео/текст в чат.", alert=True)
        return
    ok, err = await _send_uploads_to_verification(userbot, storage, event.chat_id)
    if not ok:
        await event.answer(f"Ошибка: {err}", alert=True)
        return
    waiting = _scripted_text(storage, "verification_awaiting",
                             default="⏳ Пруфы на проверке. Ожидайте.")
    try:
        await event.edit(waiting, parse_mode="html")
    except Exception:
        pass
    await event.answer("Отправлено на проверку")


async def _cb_verification_review(userbot, storage, event, verdict: str, client_chat_id):
    """Callback от проверяющих в verification_group."""
    try:
        cid = int(client_chat_id)
    except Exception:
        await event.answer("Не понимаю ID чата", alert=True)
        return
    flow = storage.get_sell_flow(cid)
    if not flow:
        await event.answer("Заявка уже неактивна", alert=True)
        return
    if verdict == "ok":
        await set_flow(storage, cid, step="verification_approved")
        try:
            await event.edit(f"✅ <b>Пропущено на вяз</b> (chat {cid})", parse_mode="html")
        except Exception:
            pass
        await event.answer("Клиент уведомлён — идём в оплату")
        # Уведомляем клиента + показываем payment-меню
        await _render_payment(userbot, storage, cid)
    else:
        # Rejection — попросим причину простым текстовым запросом.
        # Для MVP: без FSM причины — просто уведомляем rejection дефолтом.
        reason = "требуется дополнительная проверка, свяжитесь с оператором"
        try:
            await event.edit(
                f"❌ <b>Отклонено</b> (chat {cid})\nПричина: {reason}",
                parse_mode="html",
            )
        except Exception:
            pass
        await event.answer("Клиент уведомлён об отклонении")
        text = _scripted_text(storage, "verification_rejected",
                              default="❌ Проверка не пройдена. Причина: {reason}",
                              reason=reason)
        try:
            await userbot.client.send_message(cid, text, parse_mode="html")
        except Exception:
            pass
        await clear_flow(storage, cid)
        try:
            await storage.mute_chat_ai(cid, False)
        except Exception:
            pass


async def _cb_payment(userbot, storage, event, method_short: str):
    flow = get_flow(storage, event.chat_id)
    if not flow or _is_stale(flow):
        await event.answer("Сессия истекла.", alert=True)
        return
    if method_short == "GUAR":
        method = "GUARANTOR_BEFORE_WORK"
    elif method_short == "USDT":
        method = "USDT_TRC20"
    else:
        await event.answer("Неизвестный метод.", alert=True)
        return
    await set_flow(storage, event.chat_id, method=method)

    if method == "GUARANTOR_BEFORE_WORK":
        await _render_guarantor_instruction(userbot, storage, event.chat_id, edit_event=event)
        await event.answer()
        return

    # USDT — сразу переход к CRM LK-form
    override = storage.get_payment_text("usdt")
    text = override or _scripted_text(
        storage, "payment_usdt_prompt",
        default="💸 USDT TRC20 — переходим к заполнению ЛК."
    )
    try:
        await event.edit(text, parse_mode="html")
    except Exception:
        pass
    await event.answer("Переходим к заполнению ЛК")
    await _trigger_crm_lk_form(userbot, storage, event.chat_id)


async def _cb_dealready(userbot, storage, event):
    """Клиент нажал «✅ Создал сделку» — просим цифры."""
    flow = get_flow(storage, event.chat_id)
    if not flow or _is_stale(flow):
        await event.answer("Сессия истекла.", alert=True)
        return
    await set_flow(storage, event.chat_id, step="guarantor_wait_deal_number")
    text = _scripted_text(storage, "payment_guarantor_ask_deal",
                          default="Пришлите номер сделки цифрами:")
    try:
        await event.edit(text, parse_mode="html")
    except Exception:
        pass
    await event.answer("Жду номер сделки")


async def _cb_guarantor_deposited(userbot, storage, event, client_chat_id):
    """Callback [💰 Пополнено] от бухгалтера в guarantor_group."""
    try:
        cid = int(client_chat_id)
    except Exception:
        await event.answer("Bad chat id", alert=True)
        return
    flow = storage.get_sell_flow(cid)
    if not flow:
        await event.answer("Заявка неактивна", alert=True)
        return
    deal_number = flow.get("deal_number") or "?"
    try:
        await event.edit(f"💰 <b>Средства внесены</b> (сделка №{deal_number}, chat {cid})",
                         parse_mode="html")
    except Exception:
        pass
    await event.answer("Клиент уведомлён")
    # Уведомляем клиента + запускаем CRM LK-form
    text = _scripted_text(storage, "payment_guarantor_deposited",
                          default="💰 Сделка №{deal_number} пополнена!",
                          deal_number=deal_number)
    try:
        await userbot.client.send_message(cid, text, parse_mode="html")
    except Exception:
        pass
    await _trigger_crm_lk_form(userbot, storage, cid)


# ═══════════════════════ REGISTER ═══════════════════════

async def register(client, storage, userbot):
    @client.on(events.CallbackQuery(pattern=b"^sw:"))
    async def _cb(event):
        try:
            data = (event.data or b"").decode("utf-8", errors="ignore")
        except Exception:
            data = ""
        try:
            if data == "sw:x":
                await _cb_cancel(userbot, storage, event); return
            if data == "sw:start":
                await start_wizard(userbot, storage, event.chat_id)
                await event.answer("Начинаем оформление")
                return
            if data.startswith("sw:mat:"):
                await _cb_material(userbot, storage, event, data[len("sw:mat:"):]); return
            if data.startswith("sw:b:"):
                await _cb_bank(userbot, storage, event, data[len("sw:b:"):]); return
            if data.startswith("sw:back:"):
                await _cb_back(userbot, storage, event, data[len("sw:back:"):]); return
            if data == "sw:sendcheck":
                await _cb_sendcheck(userbot, storage, event); return
            if data.startswith("sw:vok:"):
                await _cb_verification_review(userbot, storage, event, "ok",
                                              data[len("sw:vok:"):]); return
            if data.startswith("sw:vno:"):
                await _cb_verification_review(userbot, storage, event, "no",
                                              data[len("sw:vno:"):]); return
            if data.startswith("sw:pay:"):
                await _cb_payment(userbot, storage, event, data[len("sw:pay:"):]); return
            if data == "sw:dealready":
                await _cb_dealready(userbot, storage, event); return
            if data.startswith("sw:gok:"):
                await _cb_guarantor_deposited(userbot, storage, event,
                                              data[len("sw:gok:"):]); return
            await event.answer()
        except Exception as e:
            logger.exception("[sell_wizard v2] callback error data=%r: %s", data, e)
            try:
                await event.answer("Ошибка, попробуйте ещё раз.", alert=True)
            except Exception:
                pass

    logger.info("[sell_wizard v2] CallbackQuery handler registered (^sw:)")

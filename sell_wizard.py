"""
sell_wizard.py — inline-кнопочный визард продажи ЛК.

Клиент вместо «уралсиб озон газпром за сколько заберёте» проходит
3 экрана с кнопками:
    1) банк      → sw:b:URALSIB
    2) метод     → sw:m:GUARANTOR_BEFORE_WORK / USDT_TRC20
    3) confirm   → sw:ok / sw:x

По подтверждению:
  • создаётся локальная ЛК-карточка (storage.add_lk_card)
  • fire-and-forget push в @PRIDE_AUDIT_BOT (audit_bot_client)
  • для GUARANTOR — просим номер сделки Continental (существующий flow)
  • для USDT — просто ждём «отработано» (адрес спросим потом)

На время визарда AI-ответы мьютим (mute_chat_ai=True) — иначе Claude
попытается ответить на клики и посылки клиента параллельно.

Все state хранятся в storage.state["sell_wizards"][chat_id_str]:
  {step, bank, price, method, wizard_msg_id, opened_ts, updated_ts}

Файл самодостаточный: подключается в userbot.start() через
    import sell_wizard
    await sell_wizard.register(self.client, storage, self)
"""

import logging
import time
from typing import Optional

from telethon import events, Button

from storage import _lock as _storage_lock

logger = logging.getLogger(__name__)

CB_PREFIX = b"sw"
STATE_KEY = "sell_wizards"
WIZARD_TIMEOUT_SEC = 30 * 60  # 30 минут — потом кнопки просто перестают работать

METHOD_LABELS = {
    "GUARANTOR_BEFORE_WORK": "🤝 Гарант в Continental",
    "USDT_TRC20": "💸 USDT TRC20 (без гаранта)",
}


# ───────────────────────── STATE HELPERS ─────────────────────────

def _get_state(storage, chat_id) -> Optional[dict]:
    wizards = storage.state.get(STATE_KEY) or {}
    return wizards.get(str(chat_id))


async def _set_state(storage, chat_id, patch: dict) -> dict:
    async with _storage_lock:
        wizards = storage.state.setdefault(STATE_KEY, {})
        cur = wizards.get(str(chat_id)) or {}
        cur.update(patch)
        cur["updated_ts"] = time.time()
        wizards[str(chat_id)] = cur
        await storage._save_unlocked()
        return dict(cur)


async def _clear_state(storage, chat_id) -> None:
    async with _storage_lock:
        wizards = storage.state.setdefault(STATE_KEY, {})
        wizards.pop(str(chat_id), None)
        await storage._save_unlocked()


def _is_stale(state: dict) -> bool:
    try:
        return (time.time() - float(state.get("updated_ts") or 0)) > WIZARD_TIMEOUT_SEC
    except Exception:
        return False


# ───────────────────────── KEYBOARDS ─────────────────────────

def _list_banks(storage):
    """Возвращает [(BANK_KEY, price), ...] отсортировано по цене DESC.
    Мержит storage.list_pricing() + DEFAULT_LK_PRICES."""
    combined = {}
    defaults = getattr(storage, "DEFAULT_LK_PRICES", {}) or {}
    for k, v in defaults.items():
        try:
            combined[str(k).upper()] = float(v)
        except (TypeError, ValueError):
            continue
    pricing = {}
    try:
        pricing = storage.list_pricing() or {}
    except Exception:
        pass
    for k, v in pricing.items():
        try:
            combined[str(k).upper()] = float(v)
        except (TypeError, ValueError):
            continue
    # Отфильтруем цены <=0
    items = [(k, v) for k, v in combined.items() if v > 0]
    items.sort(key=lambda kv: -kv[1])
    return items


def _bank_kb(storage):
    banks = _list_banks(storage)
    rows = []
    row = []
    for bank, price in banks:
        label = f"{bank.title()} · {int(price)}$"
        row.append(Button.inline(label, f"sw:b:{bank}".encode("utf-8")))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows.append([Button.inline("Прайс пуст — напишите оператору", b"sw:x")])
    rows.append([Button.inline("❌ Отмена", b"sw:x")])
    return rows


def _method_kb():
    return [
        [Button.inline("🤝 Гарант в Continental", b"sw:m:GUARANTOR_BEFORE_WORK")],
        [Button.inline("💸 USDT TRC20 (без гаранта)", b"sw:m:USDT_TRC20")],
        [Button.inline("◀️ Назад к банкам", b"sw:back:bank"),
         Button.inline("❌ Отмена", b"sw:x")],
    ]


def _confirm_kb():
    return [
        [Button.inline("✅ Оформить заявку", b"sw:ok")],
        [Button.inline("◀️ Изменить метод", b"sw:back:method"),
         Button.inline("❌ Отмена", b"sw:x")],
    ]


# ───────────────────────── PUBLIC API ─────────────────────────

async def start_wizard(userbot, storage, chat_id) -> bool:
    """Открывает шаг 1 (выбор банка) в managed_chat клиента.
    Возвращает True если визард запущен."""
    banks = _list_banks(storage)
    if not banks:
        logger.warning("[sell_wizard] no banks in pricing — refuse to open (chat=%s)", chat_id)
        return False
    # AI-mute на время визарда — не даём Claude отвечать на посторонние
    # сообщения клиента параллельно с кликами.
    try:
        await storage.mute_chat_ai(chat_id, True)
    except Exception as e:
        logger.warning("[sell_wizard] mute_chat_ai failed: %s", e)
    await _set_state(storage, chat_id, {
        "step": "bank", "opened_ts": time.time(),
        "bank": "", "price": 0, "method": "",
    })
    text = "🛒 <b>Оформление продажи ЛК</b>\n\nВыберите банк:"
    try:
        msg = await userbot.client.send_message(
            chat_id, text, parse_mode="html", buttons=_bank_kb(storage),
        )
        await _set_state(storage, chat_id, {"wizard_msg_id": int(msg.id)})
        logger.info("[sell_wizard] opened chat=%s", chat_id)
        return True
    except Exception as e:
        logger.exception("[sell_wizard] failed to open in chat=%s: %s", chat_id, e)
        try:
            await storage.mute_chat_ai(chat_id, False)
        except Exception:
            pass
        return False


# ───────────────────────── CALLBACK HANDLERS ─────────────────────────

async def _cb_bank(userbot, storage, event, bank_key: str):
    state = _get_state(storage, event.chat_id)
    if not state or _is_stale(state):
        await _clear_state(storage, event.chat_id)
        await event.answer("Сессия истекла. Напишите «продать» чтобы начать заново.", alert=True)
        return
    banks = dict(_list_banks(storage))
    price = float(banks.get(bank_key, 0))
    if price <= 0:
        await event.answer("По этому банку нет цены. Свяжитесь с оператором.", alert=True)
        return
    await _set_state(storage, event.chat_id, {
        "step": "method", "bank": bank_key, "price": price,
    })
    text = (
        f"🛒 <b>{bank_key.title()}</b> · <b>{int(price)}$</b>\n\n"
        f"Как проведём оплату?"
    )
    try:
        await event.edit(text, parse_mode="html", buttons=_method_kb())
    except Exception:
        pass
    await event.answer()


async def _cb_method(userbot, storage, event, method_key: str):
    state = _get_state(storage, event.chat_id) or {}
    if _is_stale(state):
        await _clear_state(storage, event.chat_id)
        await event.answer("Сессия истекла.", alert=True)
        return
    bank = state.get("bank")
    price = float(state.get("price") or 0)
    if not bank:
        await event.answer("Сначала выберите банк.", alert=True)
        return
    await _set_state(storage, event.chat_id, {"step": "confirm", "method": method_key})
    method_label = METHOD_LABELS.get(method_key, method_key)
    text = (
        f"📋 <b>Ваша заявка</b>\n\n"
        f"• Банк: <b>{bank.title()}</b>\n"
        f"• Цена: <b>{int(price)}$</b>\n"
        f"• Оплата: {method_label}\n\n"
        f"Подтверждаете?"
    )
    try:
        await event.edit(text, parse_mode="html", buttons=_confirm_kb())
    except Exception:
        pass
    await event.answer()


async def _cb_back(userbot, storage, event, target: str):
    state = _get_state(storage, event.chat_id) or {}
    if _is_stale(state):
        await _clear_state(storage, event.chat_id)
        await event.answer("Сессия истекла.", alert=True)
        return
    if target == "bank":
        await _set_state(storage, event.chat_id, {"step": "bank"})
        try:
            await event.edit(
                "🛒 <b>Оформление продажи ЛК</b>\n\nВыберите банк:",
                parse_mode="html", buttons=_bank_kb(storage),
            )
        except Exception:
            pass
    elif target == "method":
        bank = state.get("bank") or ""
        price = float(state.get("price") or 0)
        await _set_state(storage, event.chat_id, {"step": "method"})
        try:
            await event.edit(
                f"🛒 <b>{bank.title()}</b> · <b>{int(price)}$</b>\n\nКак проведём оплату?",
                parse_mode="html", buttons=_method_kb(),
            )
        except Exception:
            pass
    await event.answer()


async def _cb_cancel(userbot, storage, event):
    await _clear_state(storage, event.chat_id)
    try:
        await storage.mute_chat_ai(event.chat_id, False)
    except Exception:
        pass
    try:
        await event.edit("❌ Оформление отменено. Напишите «продать» чтобы начать заново.")
    except Exception:
        pass
    await event.answer("Отменено")


async def _cb_confirm(userbot, storage, event):
    state = _get_state(storage, event.chat_id) or {}
    if _is_stale(state):
        await _clear_state(storage, event.chat_id)
        await event.answer("Сессия истекла.", alert=True)
        return
    bank = state.get("bank") or ""
    method = state.get("method") or ""
    price = float(state.get("price") or 0)
    if not (bank and method):
        await event.answer("Не хватает данных, начните заново.", alert=True)
        return

    chat_info = storage.get_chat_info(event.chat_id) or {}
    client_id = int(chat_info.get("client_id") or 0) or None
    client_uname = (chat_info.get("client_username") or "").lstrip("@")

    # 1) Локальная карточка (Группа 1 «ЛК»)
    card_id = None
    try:
        card_id = await storage.add_lk_card(
            supplier=client_uname or "",
            bank=bank, fio="",
            price_usdt=price,
            payment_method=method,
            client_id=client_id or 0,
            client_username=client_uname or "",
            work_chat_id=int(event.chat_id),
            created_by="sell_wizard",
        )
        logger.info("[sell_wizard] local card=%s chat=%s bank=%s method=%s price=%s",
                    card_id, event.chat_id, bank, method, price)
    except Exception as e:
        logger.exception("[sell_wizard] add_lk_card failed: %s", e)

    # 2) Push в @PRIDE_AUDIT_BOT (fire-and-forget)
    try:
        import audit_bot_client
        _ab_method_map = {
            "USDT_TRC20": "trc",
            "GUARANTOR_BEFORE_WORK": "guarantor_before",
        }
        _ab_method = _ab_method_map.get(method, "")
        ab_card = await audit_bot_client.create_lk_card(
            work_chat_id=int(event.chat_id),
            supplier=f"@{client_uname}" if client_uname else "",
            material=bank,
            responsible="Wizard",
            bank=bank, fio="",
            client_id=client_id,
            client_username=client_uname or "",
            source="sell_wizard",
            autopost=True,
        )
        if ab_card and _ab_method:
            try:
                await audit_bot_client.set_payment(
                    card_id=int(ab_card.get("id", 0)),
                    method=_ab_method, usdt_address="",
                )
            except Exception as _pe:
                logger.warning("[sell_wizard] audit-bot set_payment failed: %s", _pe)
    except Exception as e:
        logger.warning("[sell_wizard] audit-bot push failed: %s", e)

    # 3) Сохраняем метод в chat_info + очищаем визард + снимаем AI-mute
    try:
        # set_chat_payment_info — если есть в storage
        if hasattr(storage, "set_chat_payment_info"):
            if method == "GUARANTOR_BEFORE_WORK":
                await storage.set_chat_payment_info(
                    event.chat_id,
                    stage="pending_deal_number",
                    payment_method="GUARANTOR_BEFORE_WORK",
                )
            else:
                await storage.set_chat_payment_info(
                    event.chat_id,
                    payment_method="USDT_TRC20",
                )
    except Exception as e:
        logger.warning("[sell_wizard] set_chat_payment_info failed: %s", e)

    await _clear_state(storage, event.chat_id)
    try:
        await storage.mute_chat_ai(event.chat_id, False)
    except Exception:
        pass

    method_label = METHOD_LABELS.get(method, method)
    text = (
        f"✅ <b>Заявка принята!</b>\n\n"
        f"• Карточка: <b>#{card_id or '—'}</b>\n"
        f"• Банк: <b>{bank.title()}</b>\n"
        f"• Цена: <b>{int(price)}$</b>\n"
        f"• Оплата: {method_label}\n\n"
    )
    if method == "GUARANTOR_BEFORE_WORK":
        text += (
            "Следующий шаг: создайте сделку в @PRIDE_BUHGALTERIA "
            "и пришлите её номер сюда."
        )
    else:
        text += "Как только отработаем — пришлём вам сюда сообщение."
    try:
        await event.edit(text, parse_mode="html")
    except Exception:
        pass
    await event.answer("Заявка оформлена")


# ───────────────────────── REGISTER ─────────────────────────

async def register(client, storage, userbot):
    """Регистрирует Telethon CallbackQuery handler на pattern ^sw:.
    Вызывается один раз из userbot.start()."""

    @client.on(events.CallbackQuery(pattern=b"^sw:"))
    async def _cb(event):
        try:
            data = (event.data or b"").decode("utf-8", errors="ignore")
        except Exception:
            data = ""
        try:
            if data == "sw:x":
                await _cb_cancel(userbot, storage, event); return
            if data == "sw:ok":
                await _cb_confirm(userbot, storage, event); return
            if data.startswith("sw:b:"):
                await _cb_bank(userbot, storage, event, data[len("sw:b:"):]); return
            if data.startswith("sw:m:"):
                await _cb_method(userbot, storage, event, data[len("sw:m:"):]); return
            if data.startswith("sw:back:"):
                await _cb_back(userbot, storage, event, data[len("sw:back:"):]); return
            # unknown → просто ack
            await event.answer()
        except Exception as e:
            logger.exception("[sell_wizard] callback error data=%r: %s", data, e)
            try:
                await event.answer("Ошибка, попробуйте ещё раз.", alert=True)
            except Exception:
                pass

    logger.info("[sell_wizard] CallbackQuery handler registered (^sw:)")

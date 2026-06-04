"""Откупы — детектор сообщений «дай Xк СБП» в чате клиентов.

Использует userbot для:
- Чтения сообщений из payments_chat_id
- Парсинга «дай 100К СБП», «100к нал», «карта 50000» и пр.
- Расчёта USDT по текущему курсу
- Реплая клиенту с подтверждением
- Форварда подтверждённой заявки в outkup_team_chat_id

API экспортирует:
- parse_outkup_request(text) → dict | None
- handle_outkup_message(event, userbot) → bool (обработано / нет)
- handle_outkup_confirm(event, userbot) → bool (если клиент пишет "да"/"нет")
"""
import logging
import re
import time

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# ПАРСЕР СУММЫ И МЕТОДА
# ════════════════════════════════════════════════════════════════
# Поддерживаем: "100К", "100к", "100 тыс", "100000", "100к₽", "100,000", "100 000"
# Методы: СБП, нал, карта, по номеру карты, фулл реки, реквизиты
_AMOUNT_RX = re.compile(
    r"""
    (?P<num>\d{1,3}(?:[\s,.]\d{3})+|\d+(?:[.,]\d+)?)   # 100000 / 100,000 / 100.5 / 100
    \s*
    (?P<mult>к|k|тыс|тысяч[а-я]*|м|млн|миллион[а-я]*)?  # к=*1000, м=*1000000
    """,
    re.IGNORECASE | re.VERBOSE,
)

_METHOD_PATTERNS = [
    ("sbp",   r"\bсбп\b|\bsbp\b|\bбыстр\w*\s+платеж\w*"),
    ("card",  r"\b(?:по\s+)?(?:номер\w*\s+)?карт\w*|\bкартой\b|\bна\s+карту\b"),
    ("full",  r"\bфулл?\s+реки\b|\bфулл?\s+реквизит\w*|\bреквизит\w*|\bр[\.\/]?с\b|\bрасч[её]тн\w*\s+сч[её]т\w*"),
]

# Триггер-маркеры запроса. Если в сообщении есть один из них И есть сумма И метод →
# скорее всего заявка.
_TRIGGER_WORDS = (
    "дай", "хочу", "куплю", "обмен", "обменя", "купить", "обменять",
    "нужно", "надо", "нужен", "нужны",
)


def _parse_amount(text: str):
    """Возвращает float-сумму в рублях из текста, или None."""
    if not text:
        return None
    m = _AMOUNT_RX.search(text)
    if not m:
        return None
    num_raw = m.group("num").replace(" ", "").replace(",", ".")
    mult_raw = (m.group("mult") or "").lower()
    # Если в num несколько точек (100.000 = 100000) — это разделитель тысяч
    if num_raw.count(".") > 1:
        num_raw = num_raw.replace(".", "")
    try:
        amount = float(num_raw)
    except ValueError:
        return None
    if mult_raw in ("к", "k", "тыс") or mult_raw.startswith("тыс"):
        amount *= 1000
    elif mult_raw in ("м", "млн") or mult_raw.startswith("миллион"):
        amount *= 1_000_000
    return amount


def _parse_method(text: str) -> str:
    """Возвращает 'sbp' | 'card' | 'full' или '' если не распознано."""
    t = (text or "").lower()
    for method, pat in _METHOD_PATTERNS:
        if re.search(pat, t, re.IGNORECASE):
            return method
    return ""


def parse_outkup_request(text: str) -> dict:
    """Парсит сообщение клиента. Возвращает {amount_rub, method} или {} если не запрос."""
    if not text:
        return {}
    t = text.lower().strip()
    # Должен быть либо триггер-глагол, либо метод (т.к. иногда «100к сбп» без глагола)
    has_trigger = any(w in t for w in _TRIGGER_WORDS)
    method = _parse_method(t)
    amount = _parse_amount(t)
    if not amount or not method:
        return {}
    # Если нет триггера, но есть и сумма и метод — считаем что заявка
    return {
        "amount_rub": amount,
        "method": method,
        "has_explicit_trigger": has_trigger,
    }


# ════════════════════════════════════════════════════════════════
# HANDLER (вызывается из userbot.py)
# ════════════════════════════════════════════════════════════════
METHOD_RU = {
    "sbp":  "СБП",
    "card": "Карта",
    "full": "Реквизиты ИП/ООО (фулл)",
}


async def handle_outkup_message(event, userbot, storage) -> bool:
    """Обрабатывает сообщение в payments_chat_id.

    Возвращает True если сообщение было распознано как заявка и обработано.
    Userbot должен вызвать это в обработчике входящих, ПЕРЕД остальной логикой.
    """
    settings = storage.get_outkup_settings()
    if not settings.get("enabled"):
        return False
    payments_chat = settings.get("payments_chat_id") or 0
    if not payments_chat:
        return False
    # Нормализуем chat_id (-100 префикс)
    from storage import _norm_chat_id
    if _norm_chat_id(event.chat_id) != _norm_chat_id(payments_chat):
        return False
    # Игнорируем сообщения от самого userbot/админа
    if not event.message or not (event.message.text or "").strip():
        return False
    text = event.message.text.strip()
    parsed = parse_outkup_request(text)
    if not parsed:
        return False
    # Проверка sender (не worker / не сам userbot)
    sender_id = event.sender_id
    if userbot._me and sender_id == userbot._me.id:
        return False
    # Проверка границ суммы
    if parsed["amount_rub"] < settings["min_amount_rub"]:
        return False  # слишком мало — игнор (anti-spam)
    if parsed["amount_rub"] > settings["max_amount_rub"]:
        return False
    # Получаем username клиента
    try:
        sender = await event.get_sender()
        client_username = (getattr(sender, "username", "") or "").lower()
    except Exception:
        client_username = ""
    # Создаём заявку (pending_confirm)
    order = await storage.create_outkup_order(
        client_chat_id=event.chat_id,
        client_user_id=sender_id,
        client_username=client_username,
        client_msg_id=event.message.id if event.message else 0,
        amount_rub=parsed["amount_rub"],
        method=parsed["method"],
    )
    if not order:
        return False
    # Реплай клиенту с расчётом v2 (per-client rate + payout с учётом комиссии)
    method_ru = METHOD_RU.get(order["method"], order["method"].upper())
    rate = order["rate"]
    usdt = order["calculated_usdt"]
    amount = order["amount_rub"]
    pct = float(order.get("pct_fee") or 0)
    payout = float(order.get("payout_client_usdt") or usdt)
    req_num = int(order.get("req_num") or 0)
    text_reply = (
        f"💱 <b>Заявка #{req_num:04d}</b>\n\n"
        f"💸 К приёму: <b>{amount:,.0f} ₽</b> ({method_ru})\n"
        f"📊 Курс: <b>{rate:.2f} ₽/USDT</b>\n"
        f"💰 USDT-эквивалент: {usdt:.2f}\n"
        f"⚙️ Наша комиссия: <b>{pct:.1f}%</b>\n"
        f"💵 К выплате: <b>{payout:.2f} USDT TRC20</b>\n\n"
        f"<b>Подтверждаете?</b> Напишите «<b>подтверждаю</b>» reply на это сообщение."
    ).replace(",", " ")
    try:
        target = await userbot._resolve_chat_target(event.chat_id)
        sent = await userbot.client.send_message(
            target, text_reply, parse_mode="html",
            reply_to=event.message.id if event.message else None,
        )
        if sent:
            await storage.update_outkup_order(
                order["id"], bot_reply_msg_id=sent.id,
            )
    except Exception as e:
        logger.warning("outkup reply failed: %s", e)
    logger.info(
        "outkup: новая заявка %s от %s — %.0f₽ %s → %.2f USDT",
        order["id"], client_username or sender_id, amount, method_ru, usdt,
    )
    return True


async def handle_outkup_confirm(event, userbot, storage) -> bool:
    """Если клиент пишет 'да' или 'нет' в ответ на заявку — обрабатываем."""
    settings = storage.get_outkup_settings()
    if not settings.get("enabled"):
        return False
    payments_chat = settings.get("payments_chat_id") or 0
    if not payments_chat:
        return False
    from storage import _norm_chat_id
    if _norm_chat_id(event.chat_id) != _norm_chat_id(payments_chat):
        return False
    if not event.message:
        return False
    text = (event.message.text or "").strip().lower()
    if text not in ("да", "+", "yes", "ок", "ок!", "ок.", "подтверждаю", "верно",
                    "нет", "no", "-", "отмена", "не надо", "отменить"):
        return False
    sender_id = event.sender_id
    # Ищем последнюю pending_confirm заявку этого юзера в этом чате
    orders = storage.list_outkup_orders()
    matching = [
        o for o in orders.values()
        if o.get("status") == "pending_confirm"
        and o.get("client_user_id") == sender_id
        and _norm_chat_id(o.get("client_chat_id")) == _norm_chat_id(event.chat_id)
        and (time.time() - (o.get("created_at") or 0)) < 30 * 60  # 30 мин TTL
    ]
    if not matching:
        return False
    matching.sort(key=lambda x: -(x.get("created_at") or 0))
    order = matching[0]
    is_yes = text in ("да", "+", "yes", "ок", "ок!", "ок.", "подтверждаю", "верно")
    if is_yes:
        # Подтверждаем заявку
        await storage.confirm_outkup_order(order["id"])
        # Форвардим в чат Откупщиков
        team_chat = settings.get("outkup_team_chat_id") or 0
        if team_chat:
            method_ru = METHOD_RU.get(order["method"], order["method"].upper())
            client_handle = f"@{order['client_username']}" if order.get("client_username") else f"id={order.get('client_user_id')}"
            forward_text = (
                f"📩 <b>НОВАЯ ЗАЯВКА ОТКУПА #{order['id']}</b>\n\n"
                f"👤 Клиент: {client_handle}\n"
                f"💸 Сумма: <b>{order['amount_rub']:,.0f} ₽</b>\n".replace(",", " ") +
                f"💱 К выдаче: <b>{order['calculated_usdt']:.2f} USDT TRC20</b>\n"
                f"💳 Метод: <b>{method_ru}</b>\n"
                f"📊 Курс: {order['rate']:.2f} ₽/USDT\n\n"
                f"<i>Выдайте реквизиты клиенту вручную и нажмите «✋ Взять» в JARVIS.</i>"
            )
            try:
                team_target = await userbot._resolve_chat_target(team_chat)
                await userbot.client.send_message(team_target, forward_text, parse_mode="html")
            except Exception as e:
                logger.warning("outkup forward to team failed: %s", e)
        # Отвечаем клиенту
        try:
            target = await userbot._resolve_chat_target(event.chat_id)
            await userbot.client.send_message(
                target,
                f"✅ Заявка <b>#{order['id']}</b> принята.\n\n"
                f"Сейчас наш Откупщик пришлёт вам реквизиты для оплаты "
                f"<b>{order['amount_rub']:,.0f} ₽</b> через "
                f"<b>{METHOD_RU.get(order['method'], order['method'])}</b>.\n\n"
                f"<i>Ожидайте ~1-5 минут.</i>".replace(",", " "),
                parse_mode="html",
                reply_to=event.message.id,
            )
        except Exception as e:
            logger.warning("outkup confirm-reply failed: %s", e)
        logger.info("outkup: заявка %s подтверждена клиентом", order["id"])
    else:
        # Отмена
        await storage.cancel_outkup_order(order["id"], reason="client_declined")
        try:
            target = await userbot._resolve_chat_target(event.chat_id)
            await userbot.client.send_message(
                target,
                f"❌ Заявка <b>#{order['id']}</b> отменена.",
                parse_mode="html",
                reply_to=event.message.id,
            )
        except Exception as e:
            logger.warning("outkup cancel-reply failed: %s", e)
        logger.info("outkup: заявка %s отменена клиентом", order["id"])
    return True

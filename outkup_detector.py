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
    # full: «реквизит», «р/с», «расчётный счёт», а также короткое «рек»/«реки»/«реков»
    # которое часто используют партнёры в просьбе «дай рек на N».
    ("full",  r"\bфулл?\s+реки\b|\bфулл?\s+реквизит\w*|\bреквизит\w*|\bр[\.\/]?с\b|\bрасч[её]тн\w*\s+сч[её]т\w*|\bрек(?:и|ов|у|а|ам)?\b"),
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
        logger.info("[outkup] disabled — skip chat=%s", event.chat_id)
        return False
    # v2: проверяем что чат зарегистрирован как outkup work-chat (per-партнёр).
    # Старый legacy: payments_chat_id (один общий чат) — поддерживаем как fallback.
    is_outkup = False
    try:
        is_outkup = storage.is_outkup_chat(event.chat_id)
    except Exception:
        pass
    if not is_outkup:
        from storage import _norm_chat_id
        legacy_chat = settings.get("payments_chat_id") or 0
        norm_event = _norm_chat_id(event.chat_id)
        norm_legacy = _norm_chat_id(legacy_chat) if legacy_chat else 0
        if not legacy_chat or norm_event != norm_legacy:
            logger.info(
                "[outkup] chat NOT registered: event=%s (norm=%s) is_outkup=%s legacy=%s (norm=%s) — skip",
                event.chat_id, norm_event, is_outkup, legacy_chat, norm_legacy,
            )
            return False
        logger.info("[outkup] matched legacy chat: %s", legacy_chat)
    else:
        logger.info("[outkup] matched outkup_chats: %s", event.chat_id)
    # Игнорируем сообщения от самого userbot/админа
    if not event.message or not (event.message.text or "").strip():
        return False
    text = event.message.text.strip()
    parsed = parse_outkup_request(text)
    if not parsed:
        logger.info(
            "[outkup] parse_failed: chat=%s text=%r — нужны сумма + метод (СБП/карта/рек/реквизит)",
            event.chat_id, text[:80],
        )
        return False
    logger.info(
        "[outkup] parsed OK: chat=%s amount=%s method=%s",
        event.chat_id, parsed.get("amount_rub"), parsed.get("method"),
    )
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
        f"💸 К приёму: <b>{amount:,.0f} ₽</b>\n"
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
    """Если клиент пишет 'да' / 'подтверждаю' / 'нет' в ответ на заявку."""
    # Импортим _norm_chat_id безусловно — используется и в legacy-проверке,
    # и в сравнении chat_id заявок ниже. Без этого при is_outkup=True
    # был UnboundLocalError и handler молча падал.
    from storage import _norm_chat_id
    settings = storage.get_outkup_settings()
    if not settings.get("enabled"):
        return False
    # v2: проверяем outkup_chats, legacy fallback на payments_chat_id
    is_outkup = False
    try:
        is_outkup = storage.is_outkup_chat(event.chat_id)
    except Exception:
        pass
    if not is_outkup:
        legacy_chat = settings.get("payments_chat_id") or 0
        if not legacy_chat or _norm_chat_id(event.chat_id) != _norm_chat_id(legacy_chat):
            return False
    if not event.message:
        return False
    text = (event.message.text or "").strip().lower()
    if text not in ("да", "+", "yes", "ок", "ок!", "ок.", "подтверждаю", "верно",
                    "нет", "no", "-", "отмена", "не надо", "отменить"):
        return False
    try:
        sender_id = int(event.sender_id) if event.sender_id else 0
    except Exception:
        sender_id = 0
    # Ищем последнюю pending_confirm заявку этого юзера в этом чате.
    # Раньше client_user_id сравнивался с sender_id напрямую: Telethon мог вернуть
    # Peer или строку — int!=Peer и матчинг падал. Теперь обе стороны приводим к int.
    orders = storage.list_outkup_orders()
    pending_in_chat = [
        o for o in orders.values()
        if o.get("status") == "pending_confirm"
        and _norm_chat_id(o.get("client_chat_id")) == _norm_chat_id(event.chat_id)
        and (time.time() - (o.get("created_at") or 0)) < 30 * 60  # 30 мин TTL
    ]
    matching = [
        o for o in pending_in_chat
        if int(o.get("client_user_id") or 0) == sender_id
    ]
    if not matching and pending_in_chat:
        # Fallback: если в чате одна pending заявка и sender — owner/admin,
        # подтверждаем без жёсткой привязки к user_id (полезно когда заявку
        # создаёт ассистент по команде владельца, а подтверждает он сам).
        logger.info(
            "[outkup_confirm] no exact user-match (sender=%s) — fallback to "
            "single pending in chat=%s", sender_id, event.chat_id,
        )
        matching = pending_in_chat
    if not matching:
        logger.info(
            "[outkup_confirm] NO MATCH: text=%r sender=%s chat=%s total_pending=%d",
            text, sender_id, event.chat_id,
            sum(1 for o in orders.values() if o.get("status") == "pending_confirm"),
        )
        return False
    logger.info(
        "[outkup_confirm] MATCH: text=%r sender=%s chat=%s matched=%d",
        text, sender_id, event.chat_id, len(matching),
    )
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


async def handle_outkup_stats(event, userbot, storage) -> bool:
    """Клиент-партнёр в outkup-чате пишет «стата» / «статистика» / «баланс»
    → ассистент выдаёт сводку по его откупам."""
    if not event or not event.message:
        return False
    try:
        if not storage.is_outkup_chat(event.chat_id):
            return False
    except Exception:
        return False
    text = (event.message.text or event.message.message or "").strip().lower()
    if text not in (
        "стата", "статистика", "баланс", "/стата", "/статистика", "/баланс",
        "/stats", "stats", "сколько откупил", "сколько откупили", "моя стата",
    ):
        return False
    stats = storage.get_outkup_client_stats(event.chat_id)
    import datetime as _dt
    last = ""
    if stats.get("last_done_at"):
        try:
            last = _dt.datetime.fromtimestamp(stats["last_done_at"]).strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass
    msg = (
        f"\U0001f4ca <b>Ваша статистика по откупам</b>\n\n"
        f"✅ Завершено заявок: <b>{stats['completed']}</b>\n"
        f"⏳ В работе: <b>{stats['in_progress']}</b>\n"
        f"❌ Отменено: <b>{stats['cancelled']}</b>\n\n"
        f"\U0001f4b8 Всего принято: <b>{stats['total_rub']:,.0f} ₽</b>\n"
        f"\U0001f4b5 Выплачено USDT: <b>{stats['total_usdt']:.2f}</b>"
        + (f"\n\n\U0001f552 Последняя выплата: {last}" if last else "")
    ).replace(",", " ")
    try:
        target = await userbot._resolve_chat_target(event.chat_id)
        await userbot.client.send_message(
            target, msg, parse_mode="html",
            reply_to=event.message.id if event.message else None,
        )
    except Exception as e:
        logger.warning("outkup stats reply failed: %s", e)
    logger.info("outkup: stats sent to chat=%s", event.chat_id)
    return True

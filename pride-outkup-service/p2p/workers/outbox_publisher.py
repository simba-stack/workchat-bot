"""Outbox Publisher Worker (ТЗ Том 19).

Каждые 1-2 сек:
- claim_pending() из p2p_outbox (FOR UPDATE SKIP LOCKED)
- для каждого события: WS broadcast + bot.notify_user
- mark_published / mark_failed (exponential backoff)
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

from core.db import AsyncSessionLocal
from p2p import outbox
from p2p.enums import EventType
from p2p.models import P2POutbox

logger = logging.getLogger("p2p.worker.outbox")


# WebSocket subscribers (filled in api/websocket.py later)
_ws_publishers: list = []


def register_ws_publisher(fn) -> None:
    """Регистрация WS publisher из api/main.py.
    fn: async def(event_type: str, payload: dict, aggregate_type: str|None, aggregate_id: str|None) -> None
    """
    _ws_publishers.append(fn)


# Bot notify helper
# Tg ID нужен (а не User.id) — резолвим из БД при первом обращении и кешируем
_TGID_CACHE: dict[int, int] = {}


async def _resolve_tg_id(user_id: int) -> int | None:
    """Резолв User.id → User.tg_id с in-memory кешем."""
    if user_id in _TGID_CACHE:
        return _TGID_CACHE[user_id]
    try:
        from sqlalchemy import select
        from core.db import AsyncSessionLocal
        from core.models import User
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(User.tg_id).where(User.id == user_id))
            tg = r.scalar_one_or_none()
            if tg:
                _TGID_CACHE[user_id] = int(tg)
            return tg
    except Exception as e:
        logger.warning("[outbox] resolve tg_id user=%s failed: %s", user_id, e)
        return None


_PM_LABELS = {
    "SBP": "СБП", "SBER": "Сбербанк", "TINKOFF": "Тинькофф", "ALPHA": "Альфа-Банк",
    "VTB": "ВТБ", "RAIF": "Райффайзен", "GAZPROM": "Газпромбанк", "OZON": "OZON Банк",
    "CASH": "Наличные", "OTHER": "Другое",
}


def _fmt_requisites(snap: dict) -> str:
    """Человекочитаемые реквизиты из payment_snapshot — для карточки покупателю."""
    if not snap:
        return ""
    ptype = str(snap.get("type") or "").upper()
    label = _PM_LABELS.get(ptype, ptype or "—")
    bank = str(snap.get("bank_name") or "").strip()
    cop = str(snap.get("card_number_masked") or snap.get("phone") or "").strip()
    holder = str(snap.get("account_holder") or "").strip()
    parts = [label]
    if bank and bank.upper() != ptype:
        parts.append(bank)
    out = " · ".join(parts)
    if cop:
        out += f"\nРеквизит: {cop}"
    if holder:
        out += f"\nПолучатель: {holder}"
    return out


def _trade_card(event_type: str, p: dict, role: str) -> tuple[str, str | None, list[tuple[str, str]] | None]:
    """Возвращает (title, body, deeplinks).

    role: 'buyer' | 'seller' | 'owner' | 'opener' — кому шлём.
    """
    trade_id = p.get("trade_id", "")
    short = str(trade_id)[:8]
    crypto = p.get("crypto") or p.get("crypto_currency") or "USDT"
    fiat = p.get("fiat") or p.get("fiat_currency") or "RUB"

    if event_type == EventType.TRADE_CREATED.value:
        amt = p.get("amount_crypto") or p.get("crypto_amount")
        amt_fiat = p.get("amount_fiat") or p.get("fiat_amount")
        price = p.get("price")
        snap = p.get("payment_snapshot") or {}
        num = p.get("trade_number") or short
        title = f"🆕 Новая сделка #{num}"
        rate_line = f"\nКурс: {price} {fiat} за 1 {crypto}" if price else ""
        if role == "buyer":
            body = (
                f"Покупаю {amt} {crypto}{rate_line}\n"
                f"К оплате: {amt_fiat} {fiat}\n"
                f"Статус: ожидает оплаты"
            )
            req = _fmt_requisites(snap)
            if req:
                body += (
                    f"\n\n💳 Оплатите по реквизитам продавца:\n{req}"
                    f"\n\n⚠️ Переведите точную сумму, затем нажмите «Я оплатил»."
                )
            else:
                body += "\n\n⏳ Ожидаем реквизиты продавца."
            return title, body, [
                ("💳 Открыть сделку", f"trade_{trade_id}"),
                ("✅ Я оплатил", f"trade_{trade_id}"),
                ("❌ Отменить", f"trade_{trade_id}"),
            ]
        else:  # seller
            body = (
                f"Продаю {amt} {crypto}{rate_line}\n"
                f"Получу: {amt_fiat} {fiat}\n"
                f"Статус: эскроу заблокирован, ждём оплату покупателя"
            )
            return title, body, [
                ("💬 Открыть сделку", f"trade_{trade_id}"),
            ]

    if event_type == EventType.TRADE_PAYMENT_MARKED.value:
        title = f"💸 Покупатель отметил оплату #{short}"
        if role == "seller":
            return title, "Проверь получение средств и подтверди или открой диспут.", [
                ("✅ Подтвердить", f"trade_{trade_id}"),
                ("⚠️ Открыть диспут", f"dispute_{trade_id}"),
            ]
        else:
            return title, "Ждём подтверждения от продавца.", [
                ("💬 Открыть сделку", f"trade_{trade_id}"),
            ]

    if event_type == EventType.TRADE_COMPLETED.value:
        amt = p.get("amount") or p.get("crypto_amount")
        return f"✅ Сделка #{short} завершена", f"Сумма: {amt} {crypto}\n\nОставь отзыв партнёру!", [
            ("⭐ Оставить отзыв", f"trade_{trade_id}"),
        ]

    if event_type == EventType.TRADE_CANCELLED.value:
        reason = (p.get("reason") or "").strip()[:120]
        return f"❌ Сделка #{short} отменена", reason or "Эскроу возвращён.", [
            ("📄 Подробнее", f"trade_{trade_id}"),
        ]

    if event_type == EventType.TRADE_DEADLINE_EXTENDED.value:
        return f"⏰ Таймер сделки #{short} продлён", "У тебя больше времени на оплату.", [
            ("💬 Открыть сделку", f"trade_{trade_id}"),
        ]

    if event_type == EventType.DISPUTE_OPENED.value:
        return f"⚠️ Открыт диспут #{short}", "Арбитр рассмотрит ситуацию в течение 24 часов.", [
            ("⚖️ Открыть диспут", f"dispute_{trade_id}"),
        ]

    if event_type == EventType.DISPUTE_RESOLVED.value:
        resolution = p.get("resolution") or "решён"
        return f"⚖️ Диспут #{short} решён", f"Решение арбитра: {resolution}", [
            ("📄 Подробнее", f"dispute_{trade_id}"),
        ]

    if event_type == EventType.CHAT_MESSAGE_SENT.value:
        return f"💬 Новое сообщение #{short}", None, [
            ("✉️ Открыть чат", f"trade_{trade_id}"),
        ]

    if event_type == EventType.MERCHANT_RATING_CHANGED.value:
        rating = p.get("rating", "")
        return f"⭐ Тебе оставили отзыв ({rating}/5)", None, [
            ("📊 Профиль", f"profile"),
        ]

    if event_type == EventType.SYSTEM_ALERT.value:
        return "🚨 Системное уведомление", str(p.get("description", "Подробнее в админке"))[:200], [
            ("🛡 Admin", "admin"),
        ]

    return "", None, None


async def _bot_notify(event_type: str, payload: dict) -> None:
    """Шлёт кастомизированные карточки участникам события."""
    try:
        from bot.main import notify_p2p_event  # type: ignore
    except Exception:
        return  # bot не загружен — пропустим

    # Маппинг: (user_id, role)
    sends: list[tuple[int, str]] = []
    if payload.get("buyer_id"):
        sends.append((int(payload["buyer_id"]), "buyer"))
    if payload.get("seller_id"):
        sends.append((int(payload["seller_id"]), "seller"))
    # Если нет buyer/seller — owner для ad-only events, opener для dispute
    if not sends:
        if payload.get("owner_id"):
            sends.append((int(payload["owner_id"]), "owner"))
        if payload.get("opener_id") or payload.get("opened_by_id"):
            uid = payload.get("opener_id") or payload.get("opened_by_id")
            sends.append((int(uid), "opener"))

    for uid, role in sends:
        tg_id = await _resolve_tg_id(uid)
        if not tg_id:
            continue
        title, body, links = _trade_card(event_type, payload, role)
        if not title:
            continue
        try:
            await notify_p2p_event(tg_id, title=title, body=body, deeplinks=links)
        except Exception as e:
            logger.debug("[outbox] bot notify failed user=%s tg=%s: %s", uid, tg_id, e)


def _format_notification(event_type: str, p: dict) -> str | None:
    """Только для in-app notifications (короткий текст без кнопок)."""
    short = str(p.get("trade_id", ""))[:8]
    crypto = p.get("crypto") or p.get("crypto_currency") or "USDT"
    if event_type == EventType.TRADE_CREATED.value:
        amt = p.get("amount_crypto") or p.get("crypto_amount")
        return f"🆕 Новая сделка #{short} — {amt} {crypto}"
    if event_type == EventType.TRADE_PAYMENT_MARKED.value:
        return f"💸 Покупатель отметил оплату #{short}"
    if event_type == EventType.TRADE_COMPLETED.value:
        return f"✅ Сделка #{short} завершена"
    if event_type == EventType.TRADE_CANCELLED.value:
        return f"❌ Сделка #{short} отменена"
    if event_type == EventType.DISPUTE_OPENED.value:
        return f"⚠️ Открыт диспут #{short}"
    if event_type == EventType.DISPUTE_RESOLVED.value:
        return f"⚖️ Диспут #{short} решён ({p.get('resolution','')})"
    if event_type == EventType.CHAT_MESSAGE_SENT.value:
        return f"💬 Новое сообщение #{short}"
    if event_type == EventType.MERCHANT_RATING_CHANGED.value:
        return f"⭐ Тебе оставили отзыв ({p.get('rating','')}/5)"
    if event_type == EventType.TRADE_DEADLINE_EXTENDED.value:
        return f"⏰ Таймер сделки #{short} продлён"
    return None


async def _process_one(event: P2POutbox) -> tuple[bool, str | None]:
    """Опубликовать одно событие. Возвращает (success, error_text)."""
    try:
        # 1) WS broadcast
        for fn in _ws_publishers:
            try:
                await fn(event.event_type, event.payload or {},
                        event.aggregate_type, event.aggregate_id)
            except Exception as e:
                logger.warning("[outbox] WS publisher failed: %s", e)
        # 2) Bot notify
        try:
            await _bot_notify(event.event_type, event.payload or {})
        except Exception as e:
            logger.warning("[outbox] bot notify failed: %s", e)
        # 3) Persistent notifications в p2p_notifications
        try:
            await _persist_notifications(event)
        except Exception as e:
            logger.warning("[outbox] persist notifications failed: %s", e)
        return True, None
    except Exception as e:
        return False, str(e)


async def _persist_notifications(event) -> None:
    """Создать P2PNotification записи для участников события."""
    from p2p.api.notifications import create_notification
    from core.db import AsyncSessionLocal
    text = _format_notification(event.event_type, event.payload or {})
    if not text:
        return
    targets: set[int] = set()
    p = event.payload or {}
    for k in ("buyer_id", "seller_id", "owner_id", "opened_by_id"):
        if k in p and p[k]:
            try:
                targets.add(int(p[k]))
            except Exception:
                pass
    if not targets:
        return
    async with AsyncSessionLocal() as db:
        for uid in targets:
            try:
                await create_notification(
                    db, user_id=uid, type_=event.event_type,
                    title=text, body=None, payload=p,
                    correlation_id=event.correlation_id,
                )
            except Exception:
                pass
        await db.commit()


async def run() -> None:
    logger.info("[outbox-worker] started")
    backoff = 1.0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                events = await outbox.claim_pending(db, limit=50)
                if not events:
                    await db.commit()
                    await asyncio.sleep(2.0)
                    continue
                logger.info("[outbox-worker] claimed %d events", len(events))
                for event in events:
                    ok, err = await _process_one(event)
                    if ok:
                        await outbox.mark_published(db, event)
                    else:
                        await outbox.mark_failed(db, event, err or "unknown")
                await db.commit()
                backoff = 1.0
        except Exception as e:
            logger.exception("[outbox-worker] iteration failed: %s", e)
            await asyncio.sleep(min(backoff, 30))
            backoff *= 2

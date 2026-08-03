"""
audit_bot_client.py — HTTP-клиент к @PRIDE_AUDIT_BOT (stroy-crm-bot).

Асик использует этот модуль чтобы:
  1. создать LK-карточку сразу после успешной перевязки (POST /lk-cards)
  2. заранее задать метод оплаты когда клиент ответил (PATCH /payment)
  3. читать список карточек клиента для regex-перехвата "почему блок?"

Все методы async. Ошибки логируются, не бросаются (fire-and-forget).
Кэш list_lk_cards — 60 сек TTL для снижения нагрузки.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)

AUDIT_BOT_URL = (getattr(config, "AUDIT_BOT_URL", "") or "").rstrip("/")
API_TOKEN = getattr(config, "AUDIT_BOT_API_TOKEN", "") or ""

TIMEOUT = httpx.Timeout(5.0, connect=3.0)
DEFAULT_HEADERS = {"content-type": "application/json"}


def _auth_headers() -> dict:
    h = dict(DEFAULT_HEADERS)
    if API_TOKEN:
        h["authorization"] = f"Bearer {API_TOKEN}"
    return h


def _is_configured() -> bool:
    if not AUDIT_BOT_URL:
        logger.debug("[audit_bot] AUDIT_BOT_URL не задан — интеграция отключена")
        return False
    return True


# ── Cache для list_lk_cards ──────────────────────────────────────────
_list_cache: dict = {}  # key=(work_chat_id, status) → (ts, data)
_CACHE_TTL = 60
_cache_lock = asyncio.Lock()


async def create_lk_card(
    work_chat_id: int,
    supplier: str = "",
    material: str = "",
    responsible: str = "",
    date_supply: str = "",
    bank: str = "",
    fio: str = "",
    client_id: Optional[int] = None,
    client_username: str = "",
    source: str = "jarvis-auto",
    autopost: bool = True,
    # AUDIT #2 CRIT-2 (авг 2026): payment_method как структурное поле.
    # trc | deal | guarantor_before | guarantor_after | cash | card | usdt | ...
    payment_method: str = "",
    payment_label: str = "",  # человекочитаемая метка для отображения
) -> Optional[dict]:
    """POST /api/v1/lk-cards. Возвращает card dict или None при ошибке.
    autopost=True — audit-bot сразу постит карточку в Группу 1 (В РАБОТЕ)."""
    if not _is_configured():
        return None
    payload = {
        "work_chat_id": int(work_chat_id),
        "supplier": supplier or "",
        "material": material or "",
        "responsible": responsible or "",
        "date_supply": date_supply or "",
        "bank": bank or "",
        "fio": fio or "",
        "client_id": int(client_id) if client_id else None,
        "client_username": (client_username or "").lstrip("@"),
        "source": source,
        "autopost": bool(autopost),
    }
    # CRIT-2: способ оплаты как структурное поле — иначе downstream-гейт
    # выплаты не сработает (см. stroy-crm-bot/src/index.js:460).
    if payment_method:
        payload["payment_method"] = str(payment_method)
    if payment_label:
        payload["payment_label"] = str(payment_label)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(
                f"{AUDIT_BOT_URL}/api/v1/lk-cards",
                headers=_auth_headers(),
                json=payload,
            )
        if r.status_code >= 400:
            logger.warning("[audit_bot] create_lk_card failed: %s %s", r.status_code, r.text[:200])
            return None
        data = r.json() or {}
        card = data.get("card")
        # SIMBA fix: server-side dedup — если карточка уже есть, API вернёт 200
        # с duplicate=True и существующую карточку. НЕ создаём вторую.
        if data.get("duplicate"):
            logger.info(
                "[audit_bot] DUPLICATE — карточка уже была id=%s fio=%s bank=%s status=%s "
                "(дубль НЕ создан, возвращаем существующую)",
                (card or {}).get("id"), (card or {}).get("fio"),
                (card or {}).get("bank"), (card or {}).get("status"),
            )
        elif card:
            logger.info("[audit_bot] card created id=%s work_chat=%s bank=%s",
                        card.get("id"), work_chat_id, bank)
        if card:
            # Инвалидируем кэш этого work_chat
            async with _cache_lock:
                for k in list(_list_cache.keys()):
                    if k[0] == str(work_chat_id):
                        _list_cache.pop(k, None)
        return card
    except Exception as e:
        logger.warning("[audit_bot] create_lk_card exception: %s", e)
        return None


async def find_duplicate_lk_card(
    fio: str,
    bank: str,
    client_id: Optional[int] = None,
    work_chat_id: Optional[int] = None,
) -> Optional[dict]:
    """GET /api/v1/lk-cards/search/dup — ищет активную карточку по fio+bank.
    Возвращает card dict если найдена, иначе None.
    Асик должен вызывать ПЕРЕД create_lk_card, чтобы избежать дубля."""
    if not _is_configured():
        return None
    if not fio or not bank:
        return None
    params = {"fio": fio, "bank": bank}
    if client_id:
        params["client_id"] = str(int(client_id))
    if work_chat_id:
        params["work_chat_id"] = str(int(work_chat_id))
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(
                f"{AUDIT_BOT_URL}/api/v1/lk-cards/search/dup",
                headers=_auth_headers(),
                params=params,
            )
        if r.status_code >= 400:
            logger.warning("[audit_bot] find_duplicate failed: %s %s", r.status_code, r.text[:200])
            return None
        data = r.json() or {}
        if data.get("found") and data.get("card"):
            return data["card"]
        return None
    except Exception as e:
        logger.warning("[audit_bot] find_duplicate exception: %s", e)
        return None


async def set_payment(
    card_id: int,
    method: str,
    usdt_address: str = "",
    deal_number: str = "",
    accountant_comment: str = "",
) -> Optional[dict]:
    """PATCH /api/v1/lk-cards/:id/payment. method: trc|deal|guarantor_before|guarantor_after."""
    if not _is_configured():
        return None
    payload = {"method": method}
    if usdt_address:
        payload["usdt_address"] = usdt_address
    if deal_number:
        payload["deal_number"] = deal_number
    if accountant_comment:
        payload["accountant_comment"] = accountant_comment
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.patch(
                f"{AUDIT_BOT_URL}/api/v1/lk-cards/{int(card_id)}/payment",
                headers=_auth_headers(),
                json=payload,
            )
        if r.status_code >= 400:
            logger.warning("[audit_bot] set_payment card=%s failed: %s %s",
                           card_id, r.status_code, r.text[:200])
            return None
        data = r.json() or {}
        card = data.get("card")
        if card:
            logger.info("[audit_bot] payment set card=%s method=%s addr=%s",
                        card_id, method, (usdt_address or "")[:20])
        return card
    except Exception as e:
        logger.warning("[audit_bot] set_payment exception: %s", e)
        return None


async def list_lk_cards(
    work_chat_id: Optional[int] = None,
    status: str = "",
    use_cache: bool = True,
) -> list:
    """GET /api/v1/lk-cards. С 60сек кэшем."""
    if not _is_configured():
        return []
    key = (str(work_chat_id) if work_chat_id else "", status or "")
    if use_cache:
        async with _cache_lock:
            hit = _list_cache.get(key)
            if hit and time.time() - hit[0] < _CACHE_TTL:
                return hit[1]
    params = {}
    if work_chat_id:
        params["work_chat_id"] = str(work_chat_id)
    if status:
        params["status"] = status
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(
                f"{AUDIT_BOT_URL}/api/v1/lk-cards",
                headers=_auth_headers(),
                params=params,
            )
        if r.status_code >= 400:
            logger.warning("[audit_bot] list_lk_cards failed: %s", r.status_code)
            return []
        data = r.json() or {}
        items = data.get("items") or []
        async with _cache_lock:
            _list_cache[key] = (time.time(), items)
        return items
    except Exception as e:
        logger.warning("[audit_bot] list_lk_cards exception: %s", e)
        return []


async def get_lk_card(card_id: int) -> Optional[dict]:
    """GET /api/v1/lk-cards/:id."""
    if not _is_configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(
                f"{AUDIT_BOT_URL}/api/v1/lk-cards/{int(card_id)}",
                headers=_auth_headers(),
            )
        if r.status_code >= 400:
            return None
        data = r.json() or {}
        return data.get("card")
    except Exception as e:
        logger.warning("[audit_bot] get_lk_card exception: %s", e)
        return None


async def get_latest_lk_card_for_chat(work_chat_id: int) -> Optional[dict]:
    """Хелпер: последняя карточка (по id desc) для work_chat_id.
    Используется чтобы связать ответ клиента 'USDT адрес' с созданной карточкой."""
    cards = await list_lk_cards(work_chat_id=work_chat_id, use_cache=False)
    if not cards:
        return None
    # Сортируем по id desc
    cards_sorted = sorted(cards, key=lambda c: int(c.get("id", 0) or 0), reverse=True)
    return cards_sorted[0]

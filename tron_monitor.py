"""TronGrid TRC20 USDT мониторинг для auto-credit пополнений в @marketplace_PRIDE_BOT.

Каждые TRON_POLL_INTERVAL секунд:
1. Запрашиваем у TronGrid входящие TRC20-трансферы на корп-кошелёк (с last_processed_ts)
2. Для каждой транзакции — ищем pending top-up request с точно совпадающей суммой
3. Если match — зачисляем баланс юзеру + помечаем request как credited + шлём ему сообщение

USDT TRC20 contract: TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t (decimals: 6).

API: https://api.trongrid.io/v1/accounts/{address}/transactions/trc20
Free tier: ~10 req/sec, 100k/day с API-ключом, без ключа меньше но работает.

ENV переменные (опционально):
- TRONGRID_API_KEY — API key для увеличения rate limits
- TRON_POLL_INTERVAL — секунды между опросами (по умолчанию 60)
"""
import os
import asyncio
import logging
import time
from typing import Optional

import httpx

from storage import storage

logger = logging.getLogger(__name__)

# USDT TRC20 контракт на mainnet
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6  # 1 USDT = 10^6 raw units

TRONGRID_BASE = "https://api.trongrid.io"
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()
POLL_INTERVAL = int(os.getenv("TRON_POLL_INTERVAL", "60"))


async def _fetch_recent_trc20_transfers(
    client: httpx.AsyncClient, address: str, min_timestamp_ms: int = 0, limit: int = 50,
) -> list:
    """Возвращает список входящих TRC20 USDT транзакций на address с min_timestamp.

    TronGrid сортирует по timestamp DESC. Используем `only_to=true` чтобы получить только
    входящие.
    """
    url = f"{TRONGRID_BASE}/v1/accounts/{address}/transactions/trc20"
    params = {
        "only_to": "true",
        "contract_address": USDT_TRC20_CONTRACT,
        "limit": min(limit, 200),
        "order_by": "block_timestamp,desc",
    }
    if min_timestamp_ms > 0:
        params["min_timestamp"] = int(min_timestamp_ms)
    headers = {}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    try:
        r = await client.get(url, params=params, headers=headers, timeout=20.0)
        if r.status_code != 200:
            logger.warning("TronGrid %s: %s", r.status_code, r.text[:200])
            return []
        j = r.json()
        return j.get("data") or []
    except Exception as e:
        logger.warning("TronGrid fetch error: %s", e)
        return []


def _parse_amount_to_usdt(tx: dict) -> Optional[float]:
    """Извлекает сумму USDT из TRC20 транзакции TronGrid v1."""
    try:
        raw = int(tx.get("value") or 0)
        return raw / (10 ** USDT_DECIMALS)
    except Exception:
        return None


async def _notify_user(bot, tg_user_id: int, message: str) -> None:
    """Шлёт сообщение юзеру через outsource bot. Не падает если бот не доступен."""
    if not bot or not tg_user_id:
        return
    try:
        await bot.send_message(int(tg_user_id), message, parse_mode="HTML")
    except Exception as e:
        logger.warning("Notify user %s failed: %s", tg_user_id, e)


async def _process_one_transfer(tx: dict, bot=None) -> bool:
    """Обрабатывает одну транзакцию: ищет matching pending request, кредитует если нашёлся.

    Возвращает True если транзакция была зачислена.
    """
    amount = _parse_amount_to_usdt(tx)
    if amount is None or amount <= 0:
        return False
    txid = tx.get("transaction_id") or ""
    block_ts = int(tx.get("block_timestamp") or 0)
    # Ищем pending request с этой суммой
    req = storage.find_pending_topup_by_amount(amount)
    if not req:
        return False
    # Проверим что транзакция новее чем создан запрос
    created_ms = int((req.get("created_at") or 0) * 1000)
    if block_ts > 0 and block_ts < created_ms - 60_000:  # tolerance 60 sec
        # Эта транза была ДО создания запроса — не может быть оплатой
        return False
    # Кредитуем
    updated = await storage.credit_outsource_topup(
        request_id=req["id"], txid=txid, credited_block_ts=block_ts,
    )
    if not updated:
        return False
    # Шлём юзеру уведомление через бот
    mgr = storage.get_outsource_manager(req.get("username") or "")
    if mgr and mgr.get("tg_user_id") and bot:
        base = float(req.get("base_amount") or 0)
        new_bal = float(mgr.get("wallet_balance_usdt") or 0)
        await _notify_user(
            bot, mgr["tg_user_id"],
            f"✅ <b>Зачислено {base:.2f} USDT</b>\n\n"
            f"💼 Новый баланс: <b>{new_bal:.2f} USDT</b>\n"
            f"🔗 TXID: <code>{txid}</code>\n\n"
            f"Можете брать ЛК из каталога.",
        )
    logger.info("TopUp credited: %s = %s USDT (txid=%s)", req["id"], amount, txid)
    return True


async def check_pending_topups(bot=None) -> int:
    """Один проход мониторинга. Возвращает кол-во зачисленных транзакций."""
    address = storage.get_outsource_corp_wallet()
    if not address:
        return 0
    # Сначала помечаем все истёкшие как expired
    try:
        await storage.expire_old_outsource_topups()
    except Exception as e:
        logger.warning("expire_old error: %s", e)
    last_ts = storage.get_outsource_tron_last_ts()
    # Если last_ts ещё не выставлен — берём последние 6 часов (24h на всякий)
    if last_ts == 0:
        last_ts = int((time.time() - 6 * 3600) * 1000)
    async with httpx.AsyncClient() as client:
        transfers = await _fetch_recent_trc20_transfers(
            client, address, min_timestamp_ms=last_ts,
        )
    if not transfers:
        return 0
    # Сортируем по block_timestamp ASC (старые первыми) для надёжной обработки
    transfers.sort(key=lambda x: int(x.get("block_timestamp") or 0))
    credited = 0
    max_ts = last_ts
    for tx in transfers:
        try:
            if await _process_one_transfer(tx, bot=bot):
                credited += 1
            block_ts = int(tx.get("block_timestamp") or 0)
            if block_ts > max_ts:
                max_ts = block_ts
        except Exception as e:
            logger.exception("Process transfer error: %s", e)
    # Обновляем cursor (минус 1 минута safety overlap чтобы не пропустить blockchain reorg)
    if max_ts > last_ts:
        await storage.set_outsource_tron_last_ts(max_ts - 60_000)
    return credited


async def run_tron_monitor(bot=None):
    """Бесконечный цикл мониторинга. Запускается из bot.py параллельно с polling.

    bot — экземпляр aiogram Bot @marketplace_PRIDE_BOT для отправки уведомлений.
    """
    logger.info("Tron monitor starting (poll every %d sec)", POLL_INTERVAL)
    while True:
        try:
            credited = await check_pending_topups(bot=bot)
            if credited:
                logger.info("Tron monitor: credited %d top-ups", credited)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Tron monitor loop error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

"""FeatureFlag service — управление функциями PRIDE P2P через админ-панель.

Каждая значимая функция в системе зарегистрирована тут с ключом вида:
  p2p.offer_create / p2p.deal_open / p2p.dispute / wallet.withdraw / etc.

Админ через JARVIS "Аудит функций":
  - видит весь каталог
  - может выключить функцию → её эндпоинт начинает отвечать 503
  - меняет per-feature настройки (config: JSON)
  - запускает Тест (returns {ok, note})

Все ключи и дефолты прописаны в `REGISTRY` ниже — single source of truth.
Bootstrap при старте сервиса (lifespan) синкает REGISTRY в БД (создаёт
недостающие, обновляет label/category, НЕ трогает enabled/config).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import AsyncSessionLocal
from core.models import FeatureFlag

logger = logging.getLogger(__name__)


# ─── Каталог функций (single source of truth) ───────────────────────────
# Каждая запись: (key, label, category, description, default_enabled, default_config)
REGISTRY: list[tuple[str, str, str, str, bool, dict | None]] = [
    # ── P2P market
    ("p2p.offer_list",      "P2P: список объявлений",         "p2p", "GET /offers/list — стакан P2P", True, None),
    ("p2p.offer_create",    "P2P: создать объявление",        "p2p", "POST /offers/create — мейкер создаёт оффер", True, {"min_amount_default": 1000, "max_methods": 5}),
    ("p2p.offer_edit",      "P2P: редактировать объявление",  "p2p", "PATCH /offers/{id}", True, None),
    ("p2p.offer_pause",     "P2P: пауза/возобновление",       "p2p", "PATCH /offers/{id}/pause|resume", True, None),
    ("p2p.offer_float",     "P2P: float-pricing",             "p2p", "Цена = index × margin%", True, {"margin_min": 85, "margin_max": 115}),
    ("p2p.price_band",      "P2P: price band protection",     "p2p", "Запрет цен вне коридора ±N% от индекса", True, {"band_pct": 15}),
    ("p2p.deal_create",     "P2P: открыть сделку",            "p2p", "POST /deals/create — тейкер открывает сделку", True, {"max_active_per_user": 3}),
    ("p2p.deal_mark_paid",  "P2P: пометить оплачено",         "p2p", "Buyer жмёт «Я оплатил»", True, None),
    ("p2p.deal_release",    "P2P: release escrow",            "p2p", "Seller подтверждает получение фиата", True, None),
    ("p2p.deal_cancel",     "P2P: отмена сделки",             "p2p", "Отмена до оплаты", True, None),
    ("p2p.deal_chat",       "P2P: чат в сделке",              "p2p", "DealMessage между участниками", True, None),
    ("p2p.dispute_open",    "P2P: открыть спор",              "p2p", "POST /deals/{id}/dispute", True, None),
    ("p2p.dispute_admin",   "P2P: модерация споров",          "p2p", "Admin resolve / partial split", True, None),
    ("p2p.maker_tier",      "P2P: tier мейкеров",             "p2p", "Bronze/Silver/Gold по 30д статистике", True, None),
    ("p2p.anti_fraud",      "P2P: anti-fraud limits",         "p2p", "Cooldown после 3 cancels за 24ч", True, {"cancel_limit_24h": 3, "cooldown_hours": 24}),
    ("p2p.auto_expire",     "P2P: авто-истечение сделок",     "p2p", "Cancel awaiting_payment по таймауту", True, None),

    # ── Wallet
    ("wallet.balance",      "Кошелёк: баланс",                "wallet", "GET /wallet/balances", True, None),
    ("wallet.deposit",      "Кошелёк: депозит",               "wallet", "Persistent HD-адреса", True, None),
    ("wallet.withdraw",     "Кошелёк: вывод",                 "wallet", "Withdraw TRC20 USDT/USDC/TRX", True, {"daily_limit_usd": 10000}),
    ("wallet.transfer",     "Кошелёк: P2P-переводы",          "wallet", "Внутр-перевод @username", True, None),
    ("wallet.swap",         "Кошелёк: свап монет",            "wallet", "Coin-to-coin через FixedFloat", True, None),
    ("wallet.sweep",        "Кошелёк: sweep на hot-wallet",   "wallet", "Авто-сбор USDT с user-адресов", True, None),

    # ── Cheques
    ("cheque.create",       "Чеки: создать",                  "cheques", "Виртуальный чек как у Crypto Bot", True, None),
    ("cheque.redeem",       "Чеки: активировать",             "cheques", "Активация по коду", True, None),
    ("cheque.cancel",       "Чеки: отменить",                 "cheques", "Возврат на баланс создателя", True, None),

    # ── KYC
    ("kyc.submit",          "KYC: подача",                    "kyc", "Загрузка KUC-видео / паспорта", True, None),
    ("kyc.admin",           "KYC: модерация",                 "kyc", "Approve / Reject админом", True, None),

    # ── Bot
    ("bot.start",           "Бот: /start",                    "bot", "Регистрация / приветствие", True, None),
    ("bot.commands_menu",   "Бот: меню команд",               "bot", "setMyCommands", True, None),
    ("bot.notifications",   "Бот: TG уведомления",            "bot", "Сделки / депозиты / выводы", True, None),

    # ── Mini-App
    ("miniapp.main",        "Mini-App: главная",              "miniapp", "Баланс + 4 кнопки", True, None),
    ("miniapp.coin_view",   "Mini-App: монета",               "miniapp", "Детали монеты + история", True, None),
    ("miniapp.exchange",    "Mini-App: обмен",                "miniapp", "Свап через FixedFloat/CoinGecko", True, None),
    ("miniapp.cheques",     "Mini-App: чеки",                 "miniapp", "Создание/история чеков", True, None),

    # ── Admin
    ("admin.dashboard",     "Админ: дашборд",                 "admin", "/admin/dashboard liabilities", True, None),
    ("admin.audit",         "Админ: аудит функций",           "admin", "Этот раздел", True, None),
]


# ─── Self-test registry (key → async () -> (ok, note)) ─────────────────
_SELF_TESTS: dict[str, Callable[[], Awaitable[tuple[bool, str]]]] = {}


def register_self_test(key: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> None:
    """Зарегистрировать функцию-самопроверку для feature key."""
    _SELF_TESTS[key] = fn


# ─── Bootstrap ──────────────────────────────────────────────────────────
async def bootstrap_registry() -> None:
    """Синкает REGISTRY с БД при старте.

    - Создаёт недостающие записи с default enabled/config
    - Обновляет label/category/description у существующих
    - НЕ перетирает enabled / config (это пользовательский ввод)
    """
    async with AsyncSessionLocal() as db:
        existing = {
            f.key: f
            for f in (await db.execute(select(FeatureFlag))).scalars().all()
        }
        created, updated = 0, 0
        for key, label, category, desc, default_enabled, default_config in REGISTRY:
            f = existing.get(key)
            if f is None:
                db.add(FeatureFlag(
                    key=key, label=label, category=category,
                    description=desc, enabled=default_enabled,
                    config=default_config,
                ))
                created += 1
            else:
                changed = False
                if f.label != label:
                    f.label = label; changed = True
                if f.category != category:
                    f.category = category; changed = True
                if f.description != desc:
                    f.description = desc; changed = True
                if changed:
                    updated += 1
        await db.commit()
        if created or updated:
            logger.info("[feature_flags] bootstrap: %d created, %d updated", created, updated)


# ─── Runtime access ─────────────────────────────────────────────────────
_CACHE: dict[str, FeatureFlag] = {}
_CACHE_TS: float = 0
CACHE_TTL_SEC = 30


async def _load_cache(db: AsyncSession) -> None:
    global _CACHE, _CACHE_TS
    rows = (await db.execute(select(FeatureFlag))).scalars().all()
    _CACHE = {f.key: f for f in rows}
    _CACHE_TS = time.time()


async def is_enabled(db: AsyncSession, key: str) -> bool:
    """Включена ли функция?"""
    if time.time() - _CACHE_TS > CACHE_TTL_SEC:
        await _load_cache(db)
    f = _CACHE.get(key)
    return bool(f.enabled) if f else True  # неизвестный ключ — считаем включённым (back-compat)


async def get_config(db: AsyncSession, key: str) -> dict:
    if time.time() - _CACHE_TS > CACHE_TTL_SEC:
        await _load_cache(db)
    f = _CACHE.get(key)
    return dict(f.config or {}) if f else {}


async def invalidate_cache() -> None:
    global _CACHE_TS
    _CACHE_TS = 0


# ─── FastAPI dependency ─────────────────────────────────────────────────
def feature_required(key: str):
    """Декоратор-зависимость: блокирует эндпоинт если функция выключена.

    Использование:
        @router.post("/foo", dependencies=[Depends(feature_required("p2p.offer_create"))])
    """
    from fastapi import Depends
    from core.db import get_db

    async def _dep(db: AsyncSession = Depends(get_db)):
        if not await is_enabled(db, key):
            raise HTTPException(503, f"Функция '{key}' временно отключена администратором")

    return _dep


# ─── Self-test runner ───────────────────────────────────────────────────
async def run_self_test(key: str) -> tuple[bool, str]:
    """Запустить самопроверку для feature. Возвращает (ok, note)."""
    fn = _SELF_TESTS.get(key)
    if fn is None:
        # Базовая проверка — есть ли запись в БД
        async with AsyncSessionLocal() as db:
            f = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))).scalar_one_or_none()
            if not f:
                return False, "не зарегистрирован в REGISTRY"
            return True, "OK (нет custom self-test, проверена регистрация)"
    try:
        ok, note = await fn()
        return bool(ok), str(note)[:512]
    except Exception as e:
        logger.exception("[feature_flags] self-test %s failed: %s", key, e)
        return False, f"exception: {e}"


async def save_check_result(key: str, ok: bool, note: str) -> None:
    """Сохранить результат последней проверки."""
    async with AsyncSessionLocal() as db:
        f = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))).scalar_one_or_none()
        if not f:
            return
        f.last_check_at = datetime.now(timezone.utc)
        f.last_check_status = "ok" if ok else "fail"
        f.last_check_note = note[:512]
        await db.commit()


# ─── Built-in self-tests for core features ──────────────────────────────
async def _t_p2p_offer_list() -> tuple[bool, str]:
    from core.models import Offer
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        cnt = (await db.execute(select(func.count(Offer.id)))).scalar() or 0
        return True, f"offers in DB: {cnt}"


async def _t_price_index() -> tuple[bool, str]:
    from core.models import PriceIndex
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        cnt = (await db.execute(select(func.count(PriceIndex.id)))).scalar() or 0
        if cnt == 0:
            return False, "ни одного индекса — rates_service не запущен или CoinGecko упал"
        return True, f"индексов в БД: {cnt}"


async def _t_anti_fraud() -> tuple[bool, str]:
    from core.models import User
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        locked = (await db.execute(
            select(func.count(User.id)).where(User.cancel_cooldown_until.is_not(None))
        )).scalar() or 0
        return True, f"под cooldown: {locked} юзеров"


async def _t_maker_tier() -> tuple[bool, str]:
    from core.models import User
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count(User.id)))).scalar() or 0
        ranked = (await db.execute(
            select(func.count(User.id)).where(User.maker_tier != "none")
        )).scalar() or 0
        return True, f"мейкеров с рангом: {ranked}/{total}"


async def _t_wallet_balance() -> tuple[bool, str]:
    from core.models import UserCoinBalance
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        cnt = (await db.execute(select(func.count(UserCoinBalance.id)))).scalar() or 0
        return True, f"balance records: {cnt}"


async def _t_kyc_admin() -> tuple[bool, str]:
    from core.models import User
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        pending = (await db.execute(
            select(func.count(User.id)).where(User.kyc_status == "pending_review")
        )).scalar() or 0
        return True, f"в очереди KYC: {pending}"


# Регистрируем встроенные self-tests
register_self_test("p2p.offer_list", _t_p2p_offer_list)
register_self_test("p2p.price_band", _t_price_index)
register_self_test("p2p.anti_fraud", _t_anti_fraud)
register_self_test("p2p.maker_tier", _t_maker_tier)
register_self_test("wallet.balance", _t_wallet_balance)
register_self_test("kyc.admin", _t_kyc_admin)

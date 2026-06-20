"""pytest fixtures for P2P E2E tests.

Требования:
- Postgres database с правами CREATE EXTENSION (для UUID) и create_all
- ENV: DATABASE_URL_TEST (или fallback на postgresql+asyncpg://postgres:postgres@localhost:5432/p2p_test)
- ENV bot_token=test устанавливается автоматически (для core.config)

Изоляция: каждый тест → TRUNCATE p2p_* + users CASCADE после выполнения.
"""
from __future__ import annotations
import asyncio
import os
import uuid
from decimal import Decimal
from typing import AsyncGenerator, Callable

import pytest
import pytest_asyncio

# --- Подсунуть env ДО импорта core.config ---
os.environ.setdefault("bot_token", "test-token-1234567890")
os.environ.setdefault("BOT_TOKEN", "test-token-1234567890")
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/p2p_test",
    ),
)
os.environ.setdefault("database_url", os.environ["DATABASE_URL"])


from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)


def _test_db_url() -> str:
    raw = os.environ.get("DATABASE_URL_TEST") or os.environ["DATABASE_URL"]
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql://", 1)
    if raw.startswith("postgresql://") and "+asyncpg" not in raw:
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop (необходим для async fixtures session-scope)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def _engine():
    """Один engine на всю сессию + create_all для всех моделей."""
    # Импортируем модели чтобы они зарегистрировались в Base.metadata
    from core.db import Base  # noqa: F401
    from core import models as _core_models  # noqa: F401
    from p2p import models as _p2p_models  # noqa: F401

    eng = create_async_engine(_test_db_url(), echo=False, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="session")
async def _session_factory(_engine):
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


P2P_TABLES = [
    "p2p_ledger_entries", "p2p_ledger_accounts", "p2p_wallets",
    "p2p_payment_methods", "p2p_trades", "p2p_advertisements",
    "p2p_disputes", "p2p_messages", "p2p_attachments", "p2p_reviews",
    "p2p_notifications", "p2p_audit_log", "p2p_workflows",
    "p2p_outbox", "p2p_inbox", "p2p_policies", "p2p_idempotency_keys",
]


@pytest_asyncio.fixture
async def db_session(_session_factory) -> AsyncGenerator[AsyncSession, None]:
    """Per-test session с автоочисткой p2p_* таблиц + users (тестовые юзеры)."""
    async with _session_factory() as session:
        try:
            yield session
        finally:
            try:
                await session.rollback()
            except Exception:
                pass
            await session.close()
        # cleanup в отдельной session
        async with _session_factory() as cleanup:
            for t in P2P_TABLES:
                try:
                    await cleanup.execute(text(f'TRUNCATE TABLE "{t}" CASCADE'))
                except Exception:
                    pass
            # Удаляем только тестовых юзеров (tg_id >= 9_000_000_000)
            try:
                await cleanup.execute(
                    text("DELETE FROM users WHERE tg_id >= 9000000000")
                )
            except Exception:
                pass
            await cleanup.commit()


# ───────── Factories ─────────


@pytest_asyncio.fixture
async def user_factory(db_session):
    """Создать тестового User. Возвращает callable(username) -> User."""
    from core.models import User

    counter = {"n": 0}
    created: list[User] = []

    async def _make(username: str | None = None, tg_id: int | None = None) -> User:
        counter["n"] += 1
        tg = tg_id or (9_000_000_000 + counter["n"] + int.from_bytes(uuid.uuid4().bytes[:3], "big") % 100000)
        u = User(
            tg_id=tg,
            username=username or f"test_user_{counter['n']}",
            full_name=f"Test User {counter['n']}",
            kyc_status="verified",
        )
        db_session.add(u)
        await db_session.flush()
        created.append(u)
        return u

    yield _make


@pytest_asyncio.fixture
async def ad_factory(db_session):
    """Создать тестовый Advertisement через workflow create_advertisement.

    Возвращает callable(owner_id, type='SELL', amount='1000', price='80') -> ad_id.
    """
    from p2p.orchestrator import run_workflow
    from p2p.workflows import create_advertisement

    async def _make(
        owner_id: int,
        type: str = "SELL",
        amount: str | Decimal = "1000",
        price: str | Decimal = "80",
        min_order_fiat: str = "100",
        max_order_fiat: str | None = None,
        crypto: str = "USDT",
        fiat: str = "RUB",
    ) -> str:
        amount = Decimal(str(amount))
        price = Decimal(str(price))
        max_default = (amount * price).quantize(Decimal("0.01"))
        result = await run_workflow(
            db_session,
            workflow_type="create_advertisement",
            user_id=owner_id,
            input_payload={
                "type": type.lower(),
                "crypto": crypto,
                "fiat": fiat,
                "amount_total": str(amount),
                "min_order_fiat": str(min_order_fiat),
                "max_order_fiat": str(max_order_fiat or max_default),
                "pricing_mode": "FIXED",
                "price_fixed": str(price),
                "payment_methods": [],
                "time_limit_minutes": 15,
            },
            handler=create_advertisement.handle,
        )
        return result["advertisement_id"]

    return _make


@pytest_asyncio.fixture
async def seed_wallet(db_session):
    """Заранее зачисляет USDT (или другую валюту) юзеру в USER_AVAILABLE.

    Записывается через ledger.post_transaction:
      DEBIT  DEPOSIT_PENDING (системный приход)
      CREDIT USER_AVAILABLE  (зачисление юзеру)
    Это сохраняет double-entry инвариант.
    """
    from p2p import ledger, wallet
    from p2p.enums import LedgerAccountType
    from p2p.ledger import LedgerLeg

    async def _seed(user_id: int, currency: str = "USDT", amount: str | Decimal = "10000") -> None:
        amount = Decimal(str(amount))
        await ledger.post_transaction(
            db_session,
            [
                LedgerLeg(LedgerAccountType.DEPOSIT_PENDING.value, None,
                          debit=amount, note=f"seed_wallet for user {user_id}"),
                LedgerLeg(LedgerAccountType.USER_AVAILABLE.value, user_id,
                          credit=amount, note="seed deposit"),
            ],
            currency=currency,
            reference_type="seed", reference_id=f"user:{user_id}",
        )
        await wallet.update_wallet_from_ledger(db_session, user_id, currency)

    return _seed

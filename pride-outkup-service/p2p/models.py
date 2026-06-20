"""SQLAlchemy модели P2P v2 (ТЗ Том 18 + 23).

Все таблицы с префиксом `p2p_` — изолированы от старых deals/offers/escrow_locks.
Никаких связей со старыми таблицами.

Принципы (из ТЗ):
- UUID v4 PK (autoincrement запрещён)
- created_at, updated_at, version (Optimistic Lock), deleted_at (Soft Delete)
- Все даты UTC
- Decimal(30,8) для крипты, Decimal(20,2) для фиата
- Soft Delete (физическое удаление запрещено)
- Foreign Key обязательны, Cascade Delete запрещён
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import uuid

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, CheckConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════════════
# LEDGER (ТЗ Том 21 — Double-Entry Ledger Engine)
# ═══════════════════════════════════════════════════════════════════════

class P2PLedgerAccount(Base):
    """Бухгалтерский счёт. Один на (owner_id, account_type, currency)."""
    __tablename__ = "p2p_ledger_accounts"
    __table_args__ = (
        UniqueConstraint("owner_id", "account_type", "currency", name="uq_p2p_ledger_acc"),
        Index("ix_p2p_ledger_acc_owner", "owner_id"),
        Index("ix_p2p_ledger_acc_type", "account_type"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)  # LedgerAccountType
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True,
    )
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False)


class P2PLedgerEntry(Base):
    """Одна проводка (debit XOR credit > 0). Группируются по transaction_id.

    Инвариант: Σ Debit = Σ Credit в рамках одного transaction_id.
    Записи IMMUTABLE — никаких UPDATE/DELETE после создания.
    """
    __tablename__ = "p2p_ledger_entries"
    __table_args__ = (
        Index("ix_p2p_ledger_tx", "transaction_id"),
        Index("ix_p2p_ledger_acc_created", "account_id", "created_at"),
        Index("ix_p2p_ledger_ref", "reference_type", "reference_id"),
        Index("ix_p2p_ledger_correlation", "correlation_id"),
        CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="ck_p2p_ledger_debit_xor_credit",
        ),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    transaction_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), nullable=False)
    account_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("p2p_ledger_accounts.id", ondelete="RESTRICT"), nullable=False,
    )
    debit: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)
    credit: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)

    # Что это за операция (для трассировки)
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# WALLET PROJECTION (ТЗ Том 20 §4)
# ═══════════════════════════════════════════════════════════════════════

class P2PWallet(Base):
    """Projection поверх Ledger. НЕ источник истины.

    Обновляется только после Ledger.commit() (через триггер в WalletProjectionEngine).
    Используется для быстрого чтения баланса.
    """
    __tablename__ = "p2p_wallets"
    __table_args__ = (
        UniqueConstraint("user_id", "currency", name="uq_p2p_wallet_user_currency"),
        Index("ix_p2p_wallet_user", "user_id"),
        CheckConstraint(
            "available >= 0 AND advertisement_hold >= 0 AND trade_escrow >= 0 "
            "AND frozen >= 0 AND pending >= 0",
            name="ck_p2p_wallet_non_negative",
        ),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")

    available: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)
    advertisement_hold: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)
    trade_escrow: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)
    frozen: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)
    pending: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Optimistic lock
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# PAYMENT METHODS (ТЗ Том 3 §3.8)
# ═══════════════════════════════════════════════════════════════════════

class P2PPaymentMethod(Base):
    """Способ оплаты юзера. Хранится отдельно от объявления."""
    __tablename__ = "p2p_payment_methods"
    __table_args__ = (
        Index("ix_p2p_pm_user", "user_id"),
        Index("ix_p2p_pm_status", "status"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # PaymentMethodType
    bank_name: Mapped[str] = mapped_column(String(64), nullable=False)
    account_holder: Mapped[str] = mapped_column(String(128), nullable=False)
    card_number_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)
    card_number_full: Mapped[str | None] = mapped_column(String(64), nullable=True)  # храним маскированно в UI
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    iban: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")  # PaymentMethodStatus

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════════════════════════════════
# ADVERTISEMENTS (ТЗ Том 3)
# ═══════════════════════════════════════════════════════════════════════

class P2PAdvertisement(Base):
    """Объявление SELL или BUY."""
    __tablename__ = "p2p_advertisements"
    __table_args__ = (
        Index("ix_p2p_ad_owner", "owner_id"),
        Index("ix_p2p_ad_status_type", "status", "type"),
        Index("ix_p2p_ad_currency_pair", "crypto_currency", "fiat_currency"),
        Index("ix_p2p_ad_created", "created_at"),
        CheckConstraint(
            "min_amount_fiat <= max_amount_fiat",
            name="ck_p2p_ad_min_le_max",
        ),
        CheckConstraint(
            "available_amount <= total_amount",
            name="ck_p2p_ad_available_le_total",
        ),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    type: Mapped[str] = mapped_column(String(8), nullable=False)  # AdvertisementType
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")  # AdvertisementStatus

    crypto_currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    fiat_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")

    pricing_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="FIXED")  # PricingMode
    price: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)  # Текущая цена (для отображения)
    price_source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # binance/bybit/...
    price_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)

    total_amount: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)  # Общий объём в крипте
    available_amount: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)  # Остаток
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)  # Зарезервировано под активные сделки
    min_amount_fiat: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    max_amount_fiat: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    payment_method_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # [UUID, ...]

    description: Mapped[str | None] = mapped_column(Text, nullable=True)  # До 500 chars
    merchant_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # Авто-сообщение в чат
    pay_window_min: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    # Дополнительные настройки
    auto_pause_empty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_resume_balance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    require_verified_taker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_taker_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_taker_rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    max_concurrent_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Статистика (обновляется триггерами Trade Engine)
    views_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trades_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_volume: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)

    paused_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════════════════════════════════
# TRADES (ТЗ Том 4)
# ═══════════════════════════════════════════════════════════════════════

class P2PTrade(Base):
    """Конкретная сделка между двумя пользователями.

    После создания все поля (цена, сумма, участники, способ оплаты) IMMUTABLE.
    """
    __tablename__ = "p2p_trades"
    __table_args__ = (
        Index("ix_p2p_trade_ad", "advertisement_id"),
        Index("ix_p2p_trade_buyer", "buyer_id"),
        Index("ix_p2p_trade_seller", "seller_id"),
        Index("ix_p2p_trade_status", "status"),
        Index("ix_p2p_trade_created", "created_at"),
        UniqueConstraint("trade_number", name="uq_p2p_trade_number"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    trade_number: Mapped[str] = mapped_column(String(32), nullable=False)  # T-2026-00001
    advertisement_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("p2p_advertisements.id", ondelete="RESTRICT"), nullable=False,
    )
    buyer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    seller_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")  # TradeStatus
    crypto_currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    fiat_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")

    # ЗАФИКСИРОВАННЫЕ ЗНАЧЕНИЯ — не меняются после создания
    price: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    crypto_amount: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    fiat_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    fee_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    fee_crypto: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, default=0)

    payment_method_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("p2p_payment_methods.id", ondelete="RESTRICT"), nullable=True,
    )
    # Снимок реквизитов на момент создания (на случай удаления PM)
    payment_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Workflow / correlation
    workflow_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), nullable=False)
    correlation_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), nullable=False)

    # Таймеры
    pay_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirm_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Времена переходов
    escrow_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_marked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# DISPUTES (ТЗ Том 10)
# ═══════════════════════════════════════════════════════════════════════

class P2PDispute(Base):
    """Спор по сделке. Один Trade → максимум один Dispute."""
    __tablename__ = "p2p_disputes"
    __table_args__ = (
        UniqueConstraint("trade_id", name="uq_p2p_dispute_trade"),
        Index("ix_p2p_dispute_status", "status"),
        Index("ix_p2p_dispute_arbitrator", "arbitrator_id"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    trade_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("p2p_trades.id", ondelete="RESTRICT"), nullable=False,
    )
    opened_by_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    arbitrator_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPENED")  # DisputeStatus
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)  # DisputeResolution
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    sla_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# CHAT (ТЗ Том 9)
# ═══════════════════════════════════════════════════════════════════════

class P2PMessage(Base):
    """Сообщение в чате сделки. IMMUTABLE."""
    __tablename__ = "p2p_messages"
    __table_args__ = (
        Index("ix_p2p_msg_trade_seq", "trade_id", "sequence_number"),
        Index("ix_p2p_msg_sender", "sender_id"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    trade_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("p2p_trades.id", ondelete="RESTRICT"), nullable=False,
    )
    sender_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    message_type: Mapped[str] = mapped_column(String(16), nullable=False, default="TEXT")  # MessageType
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("p2p_attachments.id", ondelete="RESTRICT"), nullable=True,
    )
    reply_to_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="SENT")  # MessageStatus
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)


class P2PAttachment(Base):
    """Файл в чате. SHA256 — для проверки целостности."""
    __tablename__ = "p2p_attachments"
    __table_args__ = (
        Index("ix_p2p_att_sha", "sha256"),
        Index("ix_p2p_att_uploader", "uploaded_by_id"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    preview_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    uploaded_by_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    virus_scan_status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# REVIEWS (ТЗ Том 18 §12)
# ═══════════════════════════════════════════════════════════════════════

class P2PReview(Base):
    """Отзыв после COMPLETED сделки. После публикации IMMUTABLE."""
    __tablename__ = "p2p_reviews"
    __table_args__ = (
        UniqueConstraint("trade_id", "author_id", name="uq_p2p_review_trade_author"),
        Index("ix_p2p_review_target", "target_user_id"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    trade_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("p2p_trades.id", ondelete="RESTRICT"), nullable=False,
    )
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    target_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..5
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# NOTIFICATIONS (ТЗ Том 18 §13)
# ═══════════════════════════════════════════════════════════════════════

class P2PNotification(Base):
    """Уведомление пользователю."""
    __tablename__ = "p2p_notifications"
    __table_args__ = (
        Index("ix_p2p_notif_user_unread", "user_id", "is_read"),
        Index("ix_p2p_notif_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    correlation_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════════════════════════════════
# AUDIT LOG (ТЗ Том 18 §14) — IMMUTABLE
# ═══════════════════════════════════════════════════════════════════════

class P2PAuditLog(Base):
    """Аудит-лог. Пишется внутри той же транзакции что и business data.

    IMMUTABLE: UPDATE/DELETE запрещены на уровне приложения.
    """
    __tablename__ = "p2p_audit_log"
    __table_args__ = (
        Index("ix_p2p_audit_actor", "actor_id"),
        Index("ix_p2p_audit_entity", "entity_type", "entity_id"),
        Index("ix_p2p_audit_correlation", "correlation_id"),
        Index("ix_p2p_audit_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # mini_app/bot/admin/api
    correlation_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# WORKFLOWS (ТЗ Том 16)
# ═══════════════════════════════════════════════════════════════════════

class P2PWorkflowExecution(Base):
    """Запись о выполнении workflow в Transaction Orchestrator."""
    __tablename__ = "p2p_workflows"
    __table_args__ = (
        Index("ix_p2p_wf_status", "status"),
        Index("ix_p2p_wf_correlation", "correlation_id"),
        Index("ix_p2p_wf_idempotency", "idempotency_key"),
        UniqueConstraint("idempotency_key", name="uq_p2p_wf_idempotency"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    correlation_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="CREATED")  # WorkflowStatus
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════════════════════════════════
# OUTBOX / INBOX (ТЗ Том 19)
# ═══════════════════════════════════════════════════════════════════════

class P2POutbox(Base):
    """События для гарантированной доставки. Пишется в той же транзакции."""
    __tablename__ = "p2p_outbox"
    __table_args__ = (
        Index("ix_p2p_outbox_status_created", "status", "created_at"),
        Index("ix_p2p_outbox_correlation", "correlation_id"),
        UniqueConstraint("event_id", name="uq_p2p_outbox_event"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    aggregate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")  # OutboxStatus
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class P2PInbox(Base):
    """Inbox для гарантии exactly-once на стороне получателя."""
    __tablename__ = "p2p_inbox"
    __table_args__ = (
        UniqueConstraint("consumer", "event_id", name="uq_p2p_inbox_consumer_event"),
        Index("ix_p2p_inbox_processed", "processed_at"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    consumer: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PROCESSED")
    correlation_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)

    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# POLICIES (ТЗ Том 14)
# ═══════════════════════════════════════════════════════════════════════

class P2PPolicy(Base):
    """Бизнес-правила. Меняются без перезапуска через PolicyEngine.reload()."""
    __tablename__ = "p2p_policies"
    __table_args__ = (
        UniqueConstraint("policy_key", "version", name="uq_p2p_policy_key_version"),
        Index("ix_p2p_policy_key_active", "policy_key", "is_active"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    policy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# IDEMPOTENCY (для защиты от replay)
# ═══════════════════════════════════════════════════════════════════════

class P2PIdempotencyKey(Base):
    """Запись об уже обработанном запросе с idempotency-key."""
    __tablename__ = "p2p_idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "endpoint", "key", name="uq_p2p_idemp_full"),
        Index("ix_p2p_idemp_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ═══════════════════════════════════════════════════════════════════════
# FAVORITES (Том 24 — UI пины: ad/merchant)
# ═══════════════════════════════════════════════════════════════════════

class P2PFavorite(Base):
    """Избранные объявления или мерчанты."""
    __tablename__ = "p2p_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "advertisement_id", name="uq_p2p_fav_user_ad"),
        UniqueConstraint("user_id", "target_user_id", name="uq_p2p_fav_user_merchant"),
        Index("ix_p2p_fav_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    advertisement_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False),
        ForeignKey("p2p_advertisements.id", ondelete="CASCADE"),
        nullable=True,
    )
    target_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False,
    )

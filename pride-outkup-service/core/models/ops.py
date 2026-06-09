"""OperationLog — все движения USDT по балансу пользователя.

Типы:
- earn         — заработал (откупщик получил $ за выданную заявку)
- spend        — потратил (клиент списал с баланса при sell_usdt)
- fee          — комиссия PRIDE (взимается со сделок)
- deposit      — зачислил USDT на баланс (incoming TRC20)
- withdraw     — вывел USDT с баланса на TRC20
- escrow_lock  — заморожено в escrow
- escrow_release  — освобождено из escrow
- adjustment   — административная корректировка
- referral     — реферальное начисление

TronOutboundLog — лог исходящих транзакций TRC20.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OperationLog(Base):
    __tablename__ = "operations_log"
    __table_args__ = (
        Index("idx_oplog_user_time", "user_id", "created_at"),
        Index("idx_oplog_type", "type"),
        Index("idx_oplog_ref", "ref_table", "ref_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Положительный = на баланс, отрицательный = с баланса.
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)

    # Баланс ДО и ПОСЛЕ — для аудита (snapshot).
    balance_before: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    balance_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))

    ref_table: Mapped[Optional[str]] = mapped_column(String(32))
    ref_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    txid: Mapped[Optional[str]] = mapped_column(String(128))
    note: Mapped[Optional[str]] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )


class TronOutboundLog(Base):
    __tablename__ = "tron_outbound_log"
    __table_args__ = (
        Index("idx_tron_user", "user_id"),
        Index("idx_tron_status", "status"),
        Index("idx_tron_txid", "txid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"),
    )
    to_address: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(256))
    txid: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False,
    )
    # pending | sent | confirmed | failed
    error_msg: Mapped[Optional[str]] = mapped_column(String(1024))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

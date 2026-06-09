"""Order — V1 заявка клиент ↔ PRIDE.

Объединяет 3 типа:
- buy_usdt        — клиент платит RUB, получает USDT
- sell_usdt       — клиент даёт USDT, получает RUB
- business_outkup — крупный откуп бизнес-счёта с разделением на части
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("idx_orders_user", "user_id"),
        Index("idx_orders_status", "status"),
        Index("idx_orders_assigned", "assigned_to_id"),
        Index("idx_orders_number", "order_number", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # buy_usdt | sell_usdt | business_outkup

    amount_rub: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount_rub_remaining: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rate_rub_per_usdt: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    pct_fee: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)

    destination: Mapped[str] = mapped_column(String(16), nullable=False)
    # balance | trc20
    destination_addr: Mapped[Optional[str]] = mapped_column(String(64))

    bank_in: Mapped[Optional[str]] = mapped_column(String(32))
    bank_out: Mapped[Optional[str]] = mapped_column(String(32))
    payment_method: Mapped[Optional[str]] = mapped_column(String(32))
    payout_target: Mapped[Optional[str]] = mapped_column(String(64))
    # для sell_usdt: куда выплачиваем RUB (карта/тел)

    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )
    # pending | accepted | partial | awaiting_receipts | done | cancelled | disputed

    assigned_to_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"),
    )

    cancelled_reason: Mapped[Optional[str]] = mapped_column(String(256))

    # Метаданные / источник (для аналитики)
    source: Mapped[str] = mapped_column(String(32), default="miniapp", nullable=False)
    # miniapp | bot_lkpm | bot_group | imported

    extra: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    user = relationship("User", back_populates="orders", foreign_keys=[user_id])
    payments = relationship(
        "OrderPayment", back_populates="order", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Order {self.order_number} {self.kind} {self.amount_rub}₽ {self.status}>"


class OrderPayment(Base):
    """Выданный реквизит под Order.

    Один Order может иметь несколько payments (для business_outkup
    реквизиты выдают порциями: 500к → 4×125к).
    """
    __tablename__ = "order_payments"
    __table_args__ = (
        Index("idx_payments_order", "order_id"),
        Index("idx_payments_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
    )
    payment_number: Mapped[str] = mapped_column(String(32), nullable=False)
    # 'pay13' — sequential по сервису

    bank: Mapped[str] = mapped_column(String(32), nullable=False)
    phone_or_card: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_name: Mapped[Optional[str]] = mapped_column(String(128))
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    manager_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"),
    )

    duration_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    warning_sent: Mapped[bool] = mapped_column(
        Numeric(1, 0), default=0, nullable=False,
    )

    receipt_url: Mapped[Optional[str]] = mapped_column(String(512))
    receipt_uploaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(
        String(32), default="waiting_receipt", nullable=False,
    )
    # waiting_receipt | receipt_uploaded | confirmed | rejected | expired
    rejected_reason: Mapped[Optional[str]] = mapped_column(String(256))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    order = relationship("Order", back_populates="payments")

    def __repr__(self) -> str:
        return f"<OrderPayment {self.payment_number} {self.amount_rub}₽ {self.status}>"

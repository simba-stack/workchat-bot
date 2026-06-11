"""Deal — V2 сделка между двумя клиентами (на основе Offer).

Side взят из Offer.side (точка зрения автора). В Deal:
- buyer_id = тот кто получает USDT (платит RUB)
- seller_id = тот кто получает RUB (отдаёт USDT, USDT блокируется в escrow)
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (
        Index("idx_deals_buyer", "buyer_id"),
        Index("idx_deals_seller", "seller_id"),
        Index("idx_deals_status", "status"),
        Index("idx_deals_number", "deal_number", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    offer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("offers.id", ondelete="RESTRICT"), nullable=False,
    )
    buyer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    seller_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )

    amount_rub: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rate_rub_per_usdt: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)

    payment_method: Mapped[str] = mapped_column(String(32), nullable=False)
    bank: Mapped[Optional[str]] = mapped_column(String(32))
    phone_or_card: Mapped[Optional[str]] = mapped_column(String(64))
    receiver_name: Mapped[Optional[str]] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(
        String(32), default="created", nullable=False,
    )
    # created | awaiting_payment | paid | released | disputed | cancelled

    receipt_url: Mapped[Optional[str]] = mapped_column(String(512))
    txid: Mapped[Optional[str]] = mapped_column(String(128))

    # Маржа PRIDE (комиссия с сделки)
    fee_usdt: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), default=Decimal("0"), nullable=False,
    )
    fee_pct: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), default=Decimal("0.5"), nullable=False,
    )

    cancelled_reason: Mapped[Optional[str]] = mapped_column(String(256))
    extra: Mapped[Optional[dict]] = mapped_column(JSONB)

    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    pay_deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # альтернативное название (industrial-spec); пишем оба для совместимости
    coin: Mapped[str] = mapped_column(String(16), default="USDT", nullable=False)
    fiat: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )

    offer = relationship("Offer", back_populates="deals")
    buyer = relationship("User", foreign_keys=[buyer_id], back_populates="deals_as_buyer")
    seller = relationship("User", foreign_keys=[seller_id], back_populates="deals_as_seller")

    def __repr__(self) -> str:
        return f"<Deal {self.deal_number} {self.amount_rub} {self.status}>"

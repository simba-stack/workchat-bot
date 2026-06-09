"""Offer — V2 объявление на P2P доске.

Один из клиентов выставляет курс/лимит/банки → другие клиенты берут
оффер и создают Deal.

Особый случай: оффер PRIDE Official (user_id=пользователь-системный, is_pride=True)
— это и есть мост V1↔V2. Этот оффер всегда сверху списка с лучшим курсом.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        Index("idx_offers_active", "side", "status"),
        Index("idx_offers_user", "user_id"),
        Index("idx_offers_rate", "rate_rub_per_usdt"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    # buy  = автор покупает USDT за RUB (выставляет: «я куплю USDT по X ₽»)
    # sell = автор продаёт USDT за RUB (выставляет: «продам USDT по X ₽»)

    rate_rub_per_usdt: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    min_amount_rub: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    max_amount_rub: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    payment_methods: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, nullable=False,
    )
    # ['тинькофф', 'сбер', 'альфа', 'озон']

    conditions: Mapped[Optional[str]] = mapped_column(String(1024))
    # «работаю с 10:00-22:00 МСК, чек обязателен»

    auto_reply: Mapped[Optional[str]] = mapped_column(String(1024))
    # Автотекст когда сделка создана

    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False,
    )
    # active | paused | archived

    # PRIDE Official flag
    is_pride_official: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Stats
    filled_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_volume_usdt: Mapped[Decimal] = mapped_column(
        Numeric(16, 4), default=Decimal("0"), nullable=False,
    )

    extra: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )

    user = relationship("User", back_populates="offers")
    deals = relationship("Deal", back_populates="offer")

    def __repr__(self) -> str:
        return f"<Offer #{self.id} {self.side} {self.rate_rub_per_usdt}₽ {self.status}>"

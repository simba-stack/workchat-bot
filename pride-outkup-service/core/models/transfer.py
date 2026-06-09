"""Transfer — P2P-перевод между двумя клиентами PRIDE P2P (Crypto Pay style)."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        Index("idx_transfer_from", "from_user_id"),
        Index("idx_transfer_to", "to_user_id"),
        Index("idx_transfer_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    to_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    coin_code: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="completed", nullable=False)
    # completed | reversed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Transfer {self.amount} {self.coin_code} {self.from_user_id}→{self.to_user_id}>"


class Swap(Base):
    __tablename__ = "swaps"
    __table_args__ = (
        Index("idx_swap_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    from_coin: Mapped[str] = mapped_column(String(16), nullable=False)
    to_coin: Mapped[str] = mapped_column(String(16), nullable=False)
    from_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    to_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fee_pct: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=Decimal("1.0"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )

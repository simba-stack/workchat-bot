"""EscrowLock — заморозка USDT под P2P сделку.

Когда buyer создаёт Deal:
- USDT продавца (seller) блокируется (списывается с его balance_usdt) → создаётся EscrowLock(status='locked').
- После release → USDT переводится на TRC20 buyer'а (или начисляется на его balance) → status='released'.
- При отмене → USDT возвращается продавцу → status='refunded'.
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


class EscrowLock(Base):
    __tablename__ = "escrow_locks"
    __table_args__ = (
        Index("idx_escrow_user", "user_id"),
        Index("idx_escrow_deal", "deal_id"),
        Index("idx_escrow_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)

    deal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("deals.id", ondelete="SET NULL"),
    )

    status: Mapped[str] = mapped_column(
        String(16), default="locked", nullable=False,
    )
    # locked | released | refunded

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<EscrowLock user={self.user_id} {self.amount_usdt}USDT {self.status}>"

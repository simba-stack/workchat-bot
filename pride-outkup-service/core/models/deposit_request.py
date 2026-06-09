"""DepositRequest — заявка пользователя на депозит USDT TRC20.

Crypto-Bot-стиль: пользователь жмёт «Пополнить → 50 USDT», бэк генерирует
exact_amount=50.0042 (base + микро-отклонение, уникальное среди active заявок),
TTL 15 минут. tron_monitor пуллит входящие транзакции hot-wallet и матчит
по точной сумме → зачисление с idempotency по txid.
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


class DepositRequest(Base):
    __tablename__ = "deposit_requests"
    __table_args__ = (
        Index("idx_dr_user", "user_id"),
        Index("idx_dr_status", "status"),
        Index("idx_dr_exact", "exact_amount", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )

    # Базовая сумма (что юзер хотел) и точная сумма для матча (с микро-отклонением)
    base_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    exact_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    # Hot-wallet address, на который ждём
    to_address: Mapped[str] = mapped_column(String(64), nullable=False)
    network: Mapped[str] = mapped_column(String(16), default="TRC20", nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False,
    )
    # pending | matched | expired | cancelled

    # Когда замэтчилось — записываем
    matched_tx_id: Mapped[Optional[str]] = mapped_column(String(128))
    matched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    matched_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<DepositRequest user={self.user_id} {self.exact_amount}USDT {self.status}>"

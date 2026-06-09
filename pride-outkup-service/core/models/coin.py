"""Coin — справочник криптовалют. UserCoinBalance — мульти-валютный кошелёк юзера."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Coin(Base):
    """Справочник монет. Seed в миграции 0003."""
    __tablename__ = "coins"
    __table_args__ = (
        Index("idx_coins_code", "code", unique=True),
        Index("idx_coins_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    # USDT, TON, TRX, BTC, ETH, SOL, DOGE, LTC, BNB, USDC, XAUT, RUB
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # Tether
    coingecko_id: Mapped[Optional[str]] = mapped_column(String(64))  # tether, toncoin, ...
    networks: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, nullable=False,
    )  # ['TRC20','ERC20','BEP20','TON','SPL']
    decimals: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    icon_color: Mapped[Optional[str]] = mapped_column(String(8))  # hex
    icon_url: Mapped[Optional[str]] = mapped_column(String(256))

    min_deposit: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"), nullable=False)
    min_withdraw: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"), nullable=False)
    withdraw_fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )


class UserCoinBalance(Base):
    """Per-coin баланс пользователя."""
    __tablename__ = "user_coin_balances"
    __table_args__ = (
        UniqueConstraint("user_id", "coin_code", name="uq_user_coin"),
        Index("idx_ucb_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    coin_code: Mapped[str] = mapped_column(String(16), nullable=False)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0"), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Balance user={self.user_id} {self.balance} {self.coin_code}>"

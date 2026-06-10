"""SystemSecret + UserDepositAddress — для HD-wallet архитектуры.

- SystemSecret хранит master_derivation_key (генерится при первом запуске)
- UserDepositAddress — public адреса юзеров (privkey деривируется on-demand из master)
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SystemSecret(Base):
    """K/V для внутренних секретов сервиса (master derivation key и т.п.).
    Не путать с kv_settings (там публичные настройки).
    """
    __tablename__ = "system_secrets"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_encrypted: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )


class UserDepositAddress(Base):
    """Уникальный адрес пополнения для юзера по конкретной (coin,network) паре."""
    __tablename__ = "user_deposit_addresses"
    __table_args__ = (
        UniqueConstraint("user_id", "coin_code", "network", name="uq_uda_user_coin_net"),
        Index("idx_uda_user", "user_id"),
        Index("idx_uda_address", "address"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    coin_code: Mapped[str] = mapped_column(String(16), nullable=False)
    network: Mapped[str] = mapped_column(String(16), nullable=False)
    address: Mapped[str] = mapped_column(String(80), nullable=False)
    derivation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # На случай sweep: можем кешировать сюда последний known balance / last seen txid
    last_balance: Mapped[Optional[str]] = mapped_column(String(32))
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )

    def __repr__(self) -> str:
        return f"<UDA user={self.user_id} {self.coin_code}/{self.network} {self.address}>"

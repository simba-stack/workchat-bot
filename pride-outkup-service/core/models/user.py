"""User — клиент PRIDE P2P."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("idx_users_kyc", "kyc_status"),
        Index("idx_users_partner", "is_partner"),
        Index("idx_users_tg", "tg_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    full_name: Mapped[Optional[str]] = mapped_column(String(256))
    phone: Mapped[Optional[str]] = mapped_column(String(32))

    # KYC
    # Levels: 0=unverified, 1=phone+passport, 2=KUC video, 3=full doc + address
    kyc_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    kyc_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )  # pending|pending_review|verified|rejected|banned
    kyc_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    kyc_video_url: Mapped[Optional[str]] = mapped_column(String(512))
    kyc_decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    kyc_decided_by: Mapped[Optional[str]] = mapped_column(String(64))

    # Payments
    trc20_address: Mapped[Optional[str]] = mapped_column(String(64))
    balance_usdt: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), default=Decimal("0"), nullable=False,
    )

    # Trust & roles
    is_partner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trust_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invited_by_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"),
    )

    # Settings
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    anti_phishing_code: Mapped[Optional[str]] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(8), default="ru", nullable=False)

    # Stats (denormalized, updated by services)
    total_deals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_deals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_deals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    disputed_deals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_release_time_sec: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_volume_usdt: Mapped[Decimal] = mapped_column(
        Numeric(16, 4), default=Decimal("0"), nullable=False,
    )

    # Online presence
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )

    # Relationships
    orders = relationship("Order", back_populates="user", foreign_keys="Order.user_id")
    offers = relationship("Offer", back_populates="user")
    deals_as_buyer = relationship("Deal", back_populates="buyer", foreign_keys="Deal.buyer_id")
    deals_as_seller = relationship("Deal", back_populates="seller", foreign_keys="Deal.seller_id")

    @property
    def completion_rate_pct(self) -> float:
        if self.total_deals == 0:
            return 0.0
        return round(self.completed_deals / self.total_deals * 100, 2)

    def __repr__(self) -> str:
        return f"<User id={self.id} tg={self.tg_id} @{self.username}>"

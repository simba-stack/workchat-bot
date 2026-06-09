"""Dispute — спор между buyer/seller или клиент/PRIDE.

Может относиться к Deal (V2 P2P) или к Order (V1).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Dispute(Base):
    __tablename__ = "disputes"
    __table_args__ = (
        Index("idx_disputes_deal", "deal_id"),
        Index("idx_disputes_order", "order_id"),
        Index("idx_disputes_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("deals.id", ondelete="SET NULL"),
    )
    order_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL"),
    )
    opened_by_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(1024), nullable=False)
    evidence_urls: Mapped[list[str]] = mapped_column(
        ARRAY(String(512)), default=list, nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), default="open", nullable=False,
    )
    # open | investigating | resolved
    resolution: Mapped[Optional[str]] = mapped_column(String(32))
    # buyer | seller | split | cancelled
    resolved_by_admin: Mapped[Optional[str]] = mapped_column(String(64))
    resolution_note: Mapped[Optional[str]] = mapped_column(String(2048))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )

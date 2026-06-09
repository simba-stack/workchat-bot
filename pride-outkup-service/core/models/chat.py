"""ChatMessage — анонимный чат внутри сделки/заявки.

Контрагенты переписываются через бота, без раскрытия личных TG.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("idx_chat_deal", "deal_id"),
        Index("idx_chat_order", "order_id"),
        Index("idx_chat_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("deals.id", ondelete="CASCADE"),
    )
    order_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"),
    )
    sender_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    text: Mapped[Optional[str]] = mapped_column(String(4000))
    attachment_url: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )

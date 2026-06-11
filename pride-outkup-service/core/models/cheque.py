"""Cheque — виртуальный чек: создаёшь, шлёшь ссылку, получатель принимает."""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, BigInteger, Numeric, DateTime, ForeignKey, Index,
)
from core.db import Base


class Cheque(Base):
    __tablename__ = "cheques"
    id = Column(Integer, primary_key=True)
    creator_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    coin_code = Column(String(16), nullable=False)
    amount = Column(Numeric(28, 8), nullable=False)
    code = Column(String(32), nullable=False, unique=True, index=True)  # уникальный код для ссылки
    comment = Column(String(500))
    status = Column(String(16), default="active")  # active / redeemed / cancelled / expired
    redeemed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    redeemed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    cancelled_at = Column(DateTime(timezone=True))

    __table_args__ = (Index("ix_cheques_status_creator", "status", "creator_user_id"),)

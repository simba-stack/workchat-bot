"""UserPaymentMethod — сохранённые реквизиты пользователя для P2P-сделок.

Продавец сохраняет один раз свои реквизиты (Тинькофф 2200..., имя получателя),
а при создании Deal с типом 'tinkoff' эти реквизиты подставляются автоматически.
Покупатель видит готовые реквизиты после создания сделки.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


# Допустимые типы — совпадают с Offer.payment_methods
PAYMENT_TYPES = {"sbp", "tinkoff", "sber", "alpha", "ozon", "raif", "vtb", "gazprom", "cash"}


class UserPaymentMethod(Base):
    __tablename__ = "user_payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # 'sbp' | 'tinkoff' | 'sber' | 'alpha' | 'ozon' | ...
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Отображаемое имя банка (если 'sbp' — например «Тинькофф (СБП)»)
    bank_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Номер карты, телефона СБП, кошелёк
    card_or_phone: Mapped[str] = mapped_column(String(64), nullable=False)
    # ФИО получателя (должно совпадать с KYC юзера)
    receiver_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Активные подставляются автоматом
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Доп. поле (например СБП-id)
    extra: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    user = relationship("User", backref="payment_methods", lazy="selectin")

    def __repr__(self) -> str:
        return f"<UserPaymentMethod #{self.id} user={self.user_id} {self.type}>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "bank_name": self.bank_name,
            "card_or_phone": self.card_or_phone,
            "receiver_name": self.receiver_name,
            "is_active": self.is_active,
            "extra": self.extra,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

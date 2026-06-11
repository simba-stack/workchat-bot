"""PriceIndex — кеш текущего рыночного курса coin/fiat.

Обновляется фоновой задачей `price_index_refresher` каждые 60с из CoinGecko.
Используется для:
1. Float-офферов: `rate = index * float_margin_pct / 100`
2. Price band: проверка что цена fixed-оффера не отклоняется >±N% от индекса
3. UI: показ "рыночный курс ~X" в Mini-App
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PriceIndex(Base):
    __tablename__ = "price_indices"
    __table_args__ = (
        UniqueConstraint("coin", "fiat", name="ux_price_indices_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coin: Mapped[str] = mapped_column(String(16), nullable=False)
    fiat: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="coingecko", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )

    def __repr__(self) -> str:
        return f"<PriceIndex {self.coin}/{self.fiat}={self.price}>"

"""FeatureFlag — таблица для админ-панели «Аудит функций».

Каждая значимая функция в боте / Mini-App / API регистрируется здесь.
Админ может:
- Включить/выключить функцию через JARVIS (`enabled`)
- Хранить per-feature настройки в `config` (JSON)
- Видеть статус последней самопроверки (`last_check_*`)
- Запустить ручную самопроверку из UI (кнопка «Тест»)

Декоратор `@feature_required("p2p.offer_create")` на эндпоинте даёт 503 если выключено.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 'p2p.offer_create', 'wallet.withdraw', 'cheque.create', ...
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    # Человеко-читаемое название для UI
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    # p2p | wallet | cheques | swap | kyc | admin | bot | miniapp
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[Optional[dict]] = mapped_column(JSONB)
    # per-feature настройки (лимиты, ставки, шаблоны)
    description: Mapped[Optional[str]] = mapped_column(Text)

    last_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_check_status: Mapped[Optional[str]] = mapped_column(String(16))
    # 'ok' | 'fail' | 'unknown'
    last_check_note: Mapped[Optional[str]] = mapped_column(String(512))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))

    def __repr__(self) -> str:
        return f"<FeatureFlag {self.key} enabled={self.enabled}>"

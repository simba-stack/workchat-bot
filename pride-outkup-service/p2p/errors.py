"""Единый реестр кодов ошибок P2P (ТЗ Том 20).

Использование:
    raise P2PError.insufficient_balance(
        needed=Decimal("100"), currency="USDT", available=Decimal("50")
    )

Или напрямую:
    raise P2PError(P2PError.E_INVALID_INPUT, detail="amount must be > 0")

Все P2PError автоматически конвертируются в JSON через global exception
handler в api/main.py:
    {
      "code": "E_INSUFFICIENT_BALANCE",
      "message": "Недостаточно средств: 100 USDT, доступно 50",
      "status": 400,
      "meta": {"needed": "100", "currency": "USDT", "available": "50"}
    }
"""
from __future__ import annotations
from typing import Any

from fastapi import HTTPException


# ═══════════════════════════════════════════════════════════════════════
# Registry: code → (http_status, message_template)
# ═══════════════════════════════════════════════════════════════════════
ERROR_REGISTRY: dict[str, tuple[int, str]] = {
    "E_AUTH_REQUIRED":         (401, "Требуется авторизация"),
    "E_FORBIDDEN":             (403, "Нет доступа"),
    "E_NOT_FOUND":             (404, "Ресурс не найден"),
    "E_INVALID_INPUT":         (422, "Некорректные данные: {detail}"),
    "E_INSUFFICIENT_BALANCE":  (400, "Недостаточно средств: {needed} {currency}, доступно {available}"),
    "E_TRADE_NOT_ACTIVE":      (409, "Сделка не активна (статус: {status})"),
    "E_AD_NOT_ACTIVE":         (409, "Объявление не активно (статус: {status})"),
    "E_ORDER_RANGE":           (400, "Сумма {amount} {fiat} вне диапазона [{min}; {max}]"),
    "E_OWN_AD_TRADE":          (403, "Нельзя торговать со своим объявлением"),
    "E_NOT_PARTICIPANT":       (403, "Вы не участник этой сделки"),
    "E_ALREADY_REVIEWED":      (409, "Отзыв уже оставлен"),
    "E_DISPUTE_EXISTS":        (409, "Диспут уже открыт"),
    "E_STATE_TRANSITION":      (422, "Запрещённый переход {from_} → {to}"),
    "E_RATE_LIMITED":          (429, "Превышен лимит: {limit} за {window}s"),
    "E_IDEMPOTENCY_CONFLICT":  (409, "Идемпотентный конфликт"),
    "E_PAYMENT_TIMEOUT":       (410, "Истёк срок оплаты"),
    "E_LEDGER_IMBALANCE":      (500, "Ошибка целостности Ledger"),
    "E_NEGATIVE_BALANCE":      (500, "Отрицательный баланс"),
    "E_INTERNAL":              (500, "Внутренняя ошибка"),
    "E_WORKFLOW_FAILED":       (500, "Workflow упал: {workflow}"),
}


class P2PError(HTTPException):
    """Унифицированная ошибка P2P-домена.

    Наследуется от HTTPException — FastAPI поймёт её "из коробки",
    плюс global handler в api/main.py отдаст структурированный JSON.
    """

    # === Константы кодов (для удобства IDE автокомплита) ===
    E_AUTH_REQUIRED = "E_AUTH_REQUIRED"
    E_FORBIDDEN = "E_FORBIDDEN"
    E_NOT_FOUND = "E_NOT_FOUND"
    E_INVALID_INPUT = "E_INVALID_INPUT"
    E_INSUFFICIENT_BALANCE = "E_INSUFFICIENT_BALANCE"
    E_TRADE_NOT_ACTIVE = "E_TRADE_NOT_ACTIVE"
    E_AD_NOT_ACTIVE = "E_AD_NOT_ACTIVE"
    E_ORDER_RANGE = "E_ORDER_RANGE"
    E_OWN_AD_TRADE = "E_OWN_AD_TRADE"
    E_NOT_PARTICIPANT = "E_NOT_PARTICIPANT"
    E_ALREADY_REVIEWED = "E_ALREADY_REVIEWED"
    E_DISPUTE_EXISTS = "E_DISPUTE_EXISTS"
    E_STATE_TRANSITION = "E_STATE_TRANSITION"
    E_RATE_LIMITED = "E_RATE_LIMITED"
    E_IDEMPOTENCY_CONFLICT = "E_IDEMPOTENCY_CONFLICT"
    E_PAYMENT_TIMEOUT = "E_PAYMENT_TIMEOUT"
    E_LEDGER_IMBALANCE = "E_LEDGER_IMBALANCE"
    E_NEGATIVE_BALANCE = "E_NEGATIVE_BALANCE"
    E_INTERNAL = "E_INTERNAL"
    E_WORKFLOW_FAILED = "E_WORKFLOW_FAILED"

    def __init__(self, code: str, **kwargs: Any) -> None:
        entry = ERROR_REGISTRY.get(code)
        if entry is None:
            entry = (500, "Неизвестная ошибка: {detail}")
            kwargs.setdefault("detail", code)
        http_status, template = entry

        # Подставляем шаблон. Если каких-то ключей не хватает — графefully.
        try:
            message = template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            message = template

        self.code = code
        self.http_status = http_status
        self.message_template = template
        self.message = message
        self.meta: dict[str, Any] = {k: _coerce(v) for k, v in kwargs.items()}

        # HTTPException ожидает detail; кладём туда наш JSON-ready dict —
        # global handler заберёт его. Также fallback-сообщение строкой.
        super().__init__(status_code=http_status, detail=self.to_dict())

    # ───────────────────────────────────────────────────────────────────
    # Сериализация
    # ───────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "status": self.http_status,
            "meta": self.meta,
        }

    # ───────────────────────────────────────────────────────────────────
    # Удобные класс-методы (raise_for_*)
    # ───────────────────────────────────────────────────────────────────
    @classmethod
    def auth_required(cls) -> "P2PError":
        return cls(cls.E_AUTH_REQUIRED)

    @classmethod
    def forbidden(cls) -> "P2PError":
        return cls(cls.E_FORBIDDEN)

    @classmethod
    def not_found(cls, what: str | None = None) -> "P2PError":
        err = cls(cls.E_NOT_FOUND)
        if what:
            err.meta["what"] = what
        return err

    @classmethod
    def invalid_input(cls, detail: str) -> "P2PError":
        return cls(cls.E_INVALID_INPUT, detail=detail)

    @classmethod
    def insufficient_balance(cls, *, needed: Any, currency: str, available: Any) -> "P2PError":
        return cls(
            cls.E_INSUFFICIENT_BALANCE,
            needed=needed, currency=currency, available=available,
        )

    @classmethod
    def trade_not_active(cls, status: str) -> "P2PError":
        return cls(cls.E_TRADE_NOT_ACTIVE, status=status)

    @classmethod
    def ad_not_active(cls, status: str) -> "P2PError":
        return cls(cls.E_AD_NOT_ACTIVE, status=status)

    @classmethod
    def order_range(cls, *, amount: Any, fiat: str, min: Any, max: Any) -> "P2PError":
        return cls(cls.E_ORDER_RANGE, amount=amount, fiat=fiat, min=min, max=max)

    @classmethod
    def own_ad_trade(cls) -> "P2PError":
        return cls(cls.E_OWN_AD_TRADE)

    @classmethod
    def not_participant(cls) -> "P2PError":
        return cls(cls.E_NOT_PARTICIPANT)

    @classmethod
    def already_reviewed(cls) -> "P2PError":
        return cls(cls.E_ALREADY_REVIEWED)

    @classmethod
    def dispute_exists(cls) -> "P2PError":
        return cls(cls.E_DISPUTE_EXISTS)

    @classmethod
    def state_transition(cls, *, from_: str, to: str) -> "P2PError":
        return cls(cls.E_STATE_TRANSITION, from_=from_, to=to)

    @classmethod
    def rate_limited(cls, *, limit: int, window: int) -> "P2PError":
        return cls(cls.E_RATE_LIMITED, limit=limit, window=window)

    @classmethod
    def idempotency_conflict(cls) -> "P2PError":
        return cls(cls.E_IDEMPOTENCY_CONFLICT)

    @classmethod
    def payment_timeout(cls) -> "P2PError":
        return cls(cls.E_PAYMENT_TIMEOUT)

    @classmethod
    def ledger_imbalance(cls, detail: str | None = None) -> "P2PError":
        err = cls(cls.E_LEDGER_IMBALANCE)
        if detail:
            err.meta["detail"] = detail
        return err

    @classmethod
    def negative_balance(cls, detail: str | None = None) -> "P2PError":
        err = cls(cls.E_NEGATIVE_BALANCE)
        if detail:
            err.meta["detail"] = detail
        return err

    @classmethod
    def internal(cls, detail: str | None = None) -> "P2PError":
        err = cls(cls.E_INTERNAL)
        if detail:
            err.meta["detail"] = detail
        return err

    @classmethod
    def workflow_failed(cls, workflow: str, detail: str | None = None) -> "P2PError":
        err = cls(cls.E_WORKFLOW_FAILED, workflow=workflow)
        if detail:
            err.meta["detail"] = detail
        return err


def _coerce(v: Any) -> Any:
    """Привести meta-значения к JSON-сериализуемым типам."""
    try:
        from decimal import Decimal
        from datetime import datetime as _dt
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, _dt):
            return v.isoformat()
    except Exception:
        pass
    return v

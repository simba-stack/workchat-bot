"""State Engine — State Transition Matrix (ТЗ Том 12).

Никто не имеет права менять status объекта напрямую — только через is_allowed() check.
"""
from __future__ import annotations
import logging
from fastapi import HTTPException

from p2p.enums import (
    AdvertisementStatus, TradeStatus, DisputeStatus,
)

logger = logging.getLogger("p2p.state")


# === Advertisement transitions ===
_ADV_ALLOWED: dict[str, set[str]] = {
    AdvertisementStatus.DRAFT.value: {AdvertisementStatus.ACTIVE.value, AdvertisementStatus.DELETED.value},
    AdvertisementStatus.ACTIVE.value: {AdvertisementStatus.PAUSED.value, AdvertisementStatus.ARCHIVED.value},
    AdvertisementStatus.PAUSED.value: {AdvertisementStatus.ACTIVE.value, AdvertisementStatus.ARCHIVED.value},
    AdvertisementStatus.ARCHIVED.value: {AdvertisementStatus.DELETED.value},
    AdvertisementStatus.DELETED.value: set(),
}


# === Trade transitions ===
_TRADE_ALLOWED: dict[str, set[str]] = {
    TradeStatus.CREATED.value: {TradeStatus.ESCROW_LOCKED.value, TradeStatus.CANCELLED.value},
    TradeStatus.ESCROW_LOCKED.value: {TradeStatus.WAITING_FOR_PAYMENT.value, TradeStatus.CANCELLED.value},
    TradeStatus.WAITING_FOR_PAYMENT.value: {
        TradeStatus.PAYMENT_MARKED.value,
        TradeStatus.CANCELLED.value,
    },
    TradeStatus.PAYMENT_MARKED.value: {
        TradeStatus.PAYMENT_CONFIRMATION.value,
        TradeStatus.DISPUTE_OPENED.value,
        TradeStatus.COMPLETED.value,  # seller сразу подтверждает
    },
    TradeStatus.PAYMENT_CONFIRMATION.value: {
        TradeStatus.COMPLETED.value,
        TradeStatus.DISPUTE_OPENED.value,
    },
    TradeStatus.DISPUTE_OPENED.value: {TradeStatus.ARBITRATION.value},
    TradeStatus.ARBITRATION.value: {TradeStatus.RESOLVED.value},
    # RESOLVED → COMPLETED/CANCELLED — стандартный путь после resolve.
    # RESOLVED → DISPUTE_OPENED разрешён только для reopen_dispute (24h окно,
    # см. p2p/workflows/dispute_extras.py). Это административный путь.
    TradeStatus.RESOLVED.value: {
        TradeStatus.COMPLETED.value,
        TradeStatus.CANCELLED.value,
        TradeStatus.DISPUTE_OPENED.value,
    },
    TradeStatus.COMPLETED.value: set(),
    TradeStatus.CANCELLED.value: set(),
}


# === Dispute transitions ===
_DISPUTE_ALLOWED: dict[str, set[str]] = {
    DisputeStatus.OPENED.value: {DisputeStatus.ARBITRATION.value},
    DisputeStatus.ARBITRATION.value: {DisputeStatus.RESOLVED.value},
    # RESOLVED → CLOSED — стандартный путь.
    # RESOLVED → OPENED — путь reopen_dispute (24h окно, см. dispute_extras.py).
    DisputeStatus.RESOLVED.value: {DisputeStatus.CLOSED.value, DisputeStatus.OPENED.value},
    DisputeStatus.CLOSED.value: set(),
}


def _check(matrix: dict[str, set[str]], current: str, target: str, entity: str) -> None:
    """Проверка одного перехода. Raise если запрещён."""
    allowed = matrix.get(current, set())
    if target not in allowed:
        msg = f"{entity}: запрещённый переход {current} → {target} (разрешено: {sorted(allowed) or 'none'})"
        logger.warning("[state] %s", msg)
        raise HTTPException(422, msg)


def assert_advertisement_transition(current: str, target: str) -> None:
    _check(_ADV_ALLOWED, current, target, "Advertisement")


def assert_trade_transition(current: str, target: str) -> None:
    _check(_TRADE_ALLOWED, current, target, "Trade")


def assert_dispute_transition(current: str, target: str) -> None:
    _check(_DISPUTE_ALLOWED, current, target, "Dispute")


def can_advertisement_transition(current: str, target: str) -> bool:
    return target in _ADV_ALLOWED.get(current, set())


def can_trade_transition(current: str, target: str) -> bool:
    return target in _TRADE_ALLOWED.get(current, set())


def can_dispute_transition(current: str, target: str) -> bool:
    return target in _DISPUTE_ALLOWED.get(current, set())


# Финальные статусы (нельзя выйти)
TRADE_TERMINAL = {TradeStatus.COMPLETED.value, TradeStatus.CANCELLED.value}
ADVERTISEMENT_TERMINAL = {AdvertisementStatus.DELETED.value}
DISPUTE_TERMINAL = {DisputeStatus.CLOSED.value}


def is_trade_terminal(status: str) -> bool:
    return status in TRADE_TERMINAL


def is_advertisement_terminal(status: str) -> bool:
    return status in ADVERTISEMENT_TERMINAL

"""Тесты State Engine — transition matrix."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from p2p import state
from p2p.enums import AdvertisementStatus, TradeStatus, DisputeStatus


def test_advertisement_transitions():
    # DRAFT → ACTIVE OK
    state.assert_advertisement_transition(
        AdvertisementStatus.DRAFT.value, AdvertisementStatus.ACTIVE.value,
    )
    # ACTIVE → DRAFT raises
    with pytest.raises(HTTPException):
        state.assert_advertisement_transition(
            AdvertisementStatus.ACTIVE.value, AdvertisementStatus.DRAFT.value,
        )
    # DELETED → ACTIVE raises
    with pytest.raises(HTTPException):
        state.assert_advertisement_transition(
            AdvertisementStatus.DELETED.value, AdvertisementStatus.ACTIVE.value,
        )


def test_trade_transitions():
    # CREATED → ESCROW_LOCKED OK
    state.assert_trade_transition(
        TradeStatus.CREATED.value, TradeStatus.ESCROW_LOCKED.value,
    )
    state.assert_trade_transition(
        TradeStatus.ESCROW_LOCKED.value, TradeStatus.WAITING_FOR_PAYMENT.value,
    )
    state.assert_trade_transition(
        TradeStatus.WAITING_FOR_PAYMENT.value, TradeStatus.PAYMENT_MARKED.value,
    )
    state.assert_trade_transition(
        TradeStatus.PAYMENT_MARKED.value, TradeStatus.COMPLETED.value,
    )

    # Перепрыгивания: CREATED → COMPLETED — нельзя
    with pytest.raises(HTTPException):
        state.assert_trade_transition(
            TradeStatus.CREATED.value, TradeStatus.COMPLETED.value,
        )
    # WAITING_FOR_PAYMENT → COMPLETED — нельзя (нужно сначала PAYMENT_MARKED)
    with pytest.raises(HTTPException):
        state.assert_trade_transition(
            TradeStatus.WAITING_FOR_PAYMENT.value, TradeStatus.COMPLETED.value,
        )


def test_terminal_status_cannot_transition():
    # COMPLETED → anything → raises
    for target in (TradeStatus.CANCELLED.value, TradeStatus.PAYMENT_MARKED.value,
                   TradeStatus.WAITING_FOR_PAYMENT.value):
        with pytest.raises(HTTPException):
            state.assert_trade_transition(TradeStatus.COMPLETED.value, target)

    # CANCELLED тоже terminal
    with pytest.raises(HTTPException):
        state.assert_trade_transition(
            TradeStatus.CANCELLED.value, TradeStatus.WAITING_FOR_PAYMENT.value,
        )

    # Advertisement DELETED terminal
    with pytest.raises(HTTPException):
        state.assert_advertisement_transition(
            AdvertisementStatus.DELETED.value, AdvertisementStatus.ACTIVE.value,
        )

    # Dispute CLOSED terminal
    with pytest.raises(HTTPException):
        state.assert_dispute_transition(
            DisputeStatus.CLOSED.value, DisputeStatus.OPENED.value,
        )

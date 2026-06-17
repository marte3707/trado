from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.worker.live_close_notifications import (
    _macro_risk_transition,
    _notify_stop_update_failures,
    _should_notify_macro_risk_state,
)


class _Evt:
    def __init__(self, event_type: str, position_id: int, data: dict) -> None:
        self.event_type = event_type
        self.position_id = position_id
        self.data = data


class _Market:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class _Pos:
    def __init__(self, symbol: str, side: str) -> None:
        self.market = _Market(symbol)
        self.side = side


class _Query:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *a, **k) -> _Query:
        return self

    def order_by(self, *a, **k) -> _Query:
        return self

    def all(self) -> list:
        return self._rows


class _DB:
    def __init__(self, events: list, positions: dict) -> None:
        self._events = events
        self._positions = positions

    def query(self, _model) -> _Query:
        return _Query(self._events)

    def get(self, _model, pid):
        return self._positions.get(pid)


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_stop_update_failure(self, **kwargs) -> bool:
        self.sent.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_notify_alerts_on_safety_critical_first_failure() -> None:
    db = _DB(
        [_Evt("live_stop_update_submit_failed", 7, {"reason": "breakeven", "safety_critical": True, "attempts": 1})],
        {7: _Pos("ETH", "long")},
    )
    notifier = _Notifier()
    await _notify_stop_update_failures(db, notifier, datetime.now(UTC))
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["symbol"] == "ETH"
    assert notifier.sent[0]["stuck"] is False


@pytest.mark.asyncio
async def test_notify_skips_routine_trailing_failure() -> None:
    db = _DB(
        [_Evt("live_stop_update_submit_failed", 7, {"reason": "ema_trailing", "safety_critical": False, "attempts": 1})],
        {7: _Pos("ETH", "long")},
    )
    notifier = _Notifier()
    await _notify_stop_update_failures(db, notifier, datetime.now(UTC))
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_notify_dedups_stuck_and_submit_failed_same_position() -> None:
    db = _DB(
        [
            _Evt("live_stop_update_submit_failed", 7, {"reason": "atr_trailing", "safety_critical": True, "attempts": 3}),
            _Evt("live_stop_update_stuck", 7, {"reason": "atr_trailing", "safety_critical": True, "attempts": 3}),
        ],
        {7: _Pos("ETH", "short")},
    )
    notifier = _Notifier()
    await _notify_stop_update_failures(db, notifier, datetime.now(UTC))
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["stuck"] is True
    assert notifier.sent[0]["attempts"] == 3


def test_macro_risk_transition_classifies_recovery() -> None:
    transition = _macro_risk_transition(
        {"active": True, "level": "high"},
        {"active": False, "level": "off"},
    )

    assert transition == "recovery"


def test_macro_risk_notification_suppresses_downgrade_by_default() -> None:
    notify, transition = _should_notify_macro_risk_state(
        {"active": True, "level": "extreme"},
        {
            "active": True,
            "level": "high",
            "last_checked_at": "2026-06-12T10:30:00+00:00",
            "last_telegram_risk_notification_at": "2026-06-12T10:00:00+00:00",
            "last_telegram_risk_notification_level": "extreme",
        },
        {
            "telegram_notify_cooldown_minutes": 180,
            "telegram_notify_on_downgrade": False,
        },
    )

    assert notify is False
    assert transition == "downgrade"


def test_macro_risk_notification_suppresses_repeated_active_alerts_inside_cooldown() -> None:
    notify, transition = _should_notify_macro_risk_state(
        {"active": False, "level": "off"},
        {
            "active": True,
            "level": "watch",
            "last_checked_at": "2026-06-12T10:30:00+00:00",
            "last_telegram_risk_notification_at": "2026-06-12T10:00:00+00:00",
            "last_telegram_risk_notification_level": "watch",
        },
        {
            "telegram_notify_cooldown_minutes": 180,
            "telegram_notify_on_downgrade": False,
        },
    )

    assert notify is False
    assert transition == "activation"


def test_macro_risk_notification_allows_escalation_inside_cooldown() -> None:
    notify, transition = _should_notify_macro_risk_state(
        {"active": True, "level": "watch"},
        {
            "active": True,
            "level": "extreme",
            "last_checked_at": "2026-06-12T10:30:00+00:00",
            "last_telegram_risk_notification_at": "2026-06-12T10:00:00+00:00",
            "last_telegram_risk_notification_level": "watch",
        },
        {
            "telegram_notify_cooldown_minutes": 180,
            "telegram_notify_on_downgrade": False,
        },
    )

    assert notify is True
    assert transition == "escalation"

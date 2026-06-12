from __future__ import annotations

from app.worker.live_close_notifications import (
    _macro_risk_transition,
    _should_notify_macro_risk_state,
)


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

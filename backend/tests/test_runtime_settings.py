from app.core.config import Settings
from app.core.runtime_settings import (
    effective_directional_reversal_breaker_settings,
    effective_enabled_timeframes,
    effective_monitoring_only_timeframes,
    effective_new_live_entries_enabled,
    effective_portfolio_guard_settings,
    effective_telegram_notifications_enabled,
)
from app.db.models import AppSetting


class FakeDb:
    def __init__(self, rows: dict[str, AppSetting] | None = None) -> None:
        self.rows = rows or {}

    def get(self, model: object, key: str) -> AppSetting | None:
        if model is AppSetting:
            return self.rows.get(key)
        raise AssertionError(f"unexpected get model: {model}")

    def add(self, row: AppSetting) -> None:
        self.rows[row.key] = row


def test_effective_enabled_timeframes_uses_db_values_when_present() -> None:
    db = FakeDb(
        {
            "timeframes": AppSetting(
                key="timeframes",
                value={"enabled": [" 15m ", "1h", "15m", "", 123]},
            )
        }
    )

    result = effective_enabled_timeframes(db, Settings(enabled_timeframes=["4h"]))

    assert result == ["15m", "1h"]


def test_effective_enabled_timeframes_falls_back_to_settings_defaults() -> None:
    db = FakeDb({"timeframes": AppSetting(key="timeframes", value={"enabled": []})})

    result = effective_enabled_timeframes(db, Settings(enabled_timeframes=["15m", "1h"]))

    assert result == ["15m", "1h"]


def test_effective_monitoring_only_timeframes_dedups_and_excludes_enabled() -> None:
    db = FakeDb(
        {
            "timeframes": AppSetting(
                key="timeframes",
                value={
                    "enabled": ["15m", "1h"],
                    "monitoring_only": ["5m", "15m", "5m", " 4h ", None],
                },
            )
        }
    )

    result = effective_monitoring_only_timeframes(db)

    assert result == ["5m", "4h"]


def test_effective_telegram_notifications_honors_explicit_false() -> None:
    db = FakeDb(
        {"alerts": AppSetting(key="alerts", value={"telegram_enabled": False})}
    )

    assert effective_telegram_notifications_enabled(db) is False


def test_effective_telegram_notifications_preserves_legacy_default() -> None:
    assert effective_telegram_notifications_enabled(FakeDb()) is True


def test_new_live_entries_guard_defaults_open_and_honors_pause() -> None:
    settings = Settings()
    assert effective_new_live_entries_enabled(FakeDb(), settings) is True
    db = FakeDb(
        {
            "live_execution": AppSetting(
                key="live_execution",
                value={"new_live_entries_enabled": False},
            )
        }
    )
    assert effective_new_live_entries_enabled(db, settings) is False


def test_directional_risk_guard_defaults_are_live_only_for_new_caps() -> None:
    db = FakeDb({"risk": AppSetting(key="risk", value={})})

    portfolio = effective_portfolio_guard_settings(db)
    breaker = effective_directional_reversal_breaker_settings(db)

    assert portfolio["max_new_positions_per_cycle_by_side"] == {"live": 3}
    assert portfolio["max_directional_markets"] == {"live": 10}
    assert portfolio["max_low_tf_directional_open_positions"] == {
        "apply_timeframes": ["5m", "15m"],
        "live": 8,
    }
    assert portfolio["max_directional_risk_r"]["live"] == 8
    assert breaker["modes"] == {"paper": False, "live": True}
    assert breaker["window_minutes"] == 120
    assert breaker["stop_loss_count"] == 4
    assert breaker["cooldown_minutes"] == 180

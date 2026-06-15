from app.core.config import Settings
from app.core.runtime_settings import (
    effective_directional_reversal_breaker_settings,
    DEFAULT_MACRO_RISK_MONITOR_SETTINGS,
    effective_portfolio_guard_settings,
    effective_enabled_timeframes,
    effective_macro_risk_monitor_settings,
    effective_monitoring_only_timeframes,
)
from app.db.init_db import _normalize_macro_risk_monitor_policies
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


def test_effective_macro_risk_monitor_defaults_keep_watch_and_high_soft() -> None:
    db = FakeDb({"risk": AppSetting(key="risk", value={})})

    result = effective_macro_risk_monitor_settings(db)

    assert result["min_level_hold_minutes"] == {
        "watch": 30,
        "high": 60,
        "extreme": 90,
        "default": 30,
    }
    assert result["policies"]["watch"]["require_mtf_confluence"] is False
    assert result["policies"]["high"]["require_mtf_confluence"] is False
    assert result["policies"]["extreme"]["require_mtf_confluence"] is True


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


def test_normalize_macro_risk_monitor_policies_backfills_only_watch_and_high() -> None:
    value = {
        **DEFAULT_MACRO_RISK_MONITOR_SETTINGS,
        "policies": {
            "watch": {
                "size_multiplier": 1.0,
                "require_mtf_confluence": True,
            },
            "high": {
                "size_multiplier": 0.5,
                "require_mtf_confluence": True,
            },
            "extreme": {
                "size_multiplier": 0.25,
                "require_mtf_confluence": True,
            },
        },
        "custom_key": {"keep": True},
    }

    normalized, mutated = _normalize_macro_risk_monitor_policies(value)

    assert mutated is True
    assert normalized["policies"]["watch"]["require_mtf_confluence"] is False
    assert normalized["policies"]["high"]["require_mtf_confluence"] is False
    assert normalized["policies"]["extreme"]["require_mtf_confluence"] is True
    assert normalized["custom_key"] == {"keep": True}

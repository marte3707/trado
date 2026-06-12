from app.core.config import Settings
from app.core.runtime_settings import (
    effective_enabled_timeframes,
    effective_monitoring_only_timeframes,
)
from app.db.models import AppSetting


class FakeDb:
    def __init__(self, rows: dict[str, AppSetting] | None = None) -> None:
        self.rows = rows or {}

    def get(self, model: object, key: str) -> AppSetting | None:
        if model is AppSetting:
            return self.rows.get(key)
        raise AssertionError(f"unexpected get model: {model}")


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

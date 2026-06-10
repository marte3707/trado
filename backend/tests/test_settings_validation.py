import pytest
from pydantic import ValidationError

from app.schemas import LiveExecutionSettings, RiskControlsSettings


def test_risk_controls_rejects_out_of_range_score() -> None:
    with pytest.raises(ValidationError):
        RiskControlsSettings.model_validate({"min_signal_score": 150})


def test_risk_controls_rejects_negative_leverage() -> None:
    with pytest.raises(ValidationError):
        RiskControlsSettings.model_validate({"max_leverage": -1})


def test_risk_controls_preserves_unmodelled_fields() -> None:
    """I campi non modellati (overrides, flag, liste) devono passare invariati."""
    model = RiskControlsSettings.model_validate(
        {
            "min_signal_score": 70,
            "disabled_signal_sources": ["whale_flow"],
            "regime_filter_enabled": True,
        }
    )
    dumped = model.model_dump()
    assert dumped["min_signal_score"] == 70
    assert dumped["disabled_signal_sources"] == ["whale_flow"]
    assert dumped["regime_filter_enabled"] is True


def test_live_execution_rejects_negative_notional() -> None:
    with pytest.raises(ValidationError):
        LiveExecutionSettings.model_validate({"order_notional_cap_usd": -5})


def test_live_execution_accepts_valid_payload_with_extra() -> None:
    model = LiveExecutionSettings.model_validate(
        {"min_account_value_usd": 100.0, "max_slippage_pct": 0.5, "custom_flag": True}
    )
    assert model.model_dump()["custom_flag"] is True

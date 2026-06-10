from __future__ import annotations

from app.core.enums import Direction
from app.engine.scoring import ScoreResult
from app.engine.signal_engine import SignalEngine
from app.indicators.volume_profile import build_volume_profile_from_triplets


def _score(direction: Direction) -> ScoreResult:
    return ScoreResult(
        direction=direction,
        score=70,
        confidence_label="buono",
        reason="test",
        breakdown={},
    )


def _profile():
    profile = build_volume_profile_from_triplets(
        [
            (90.0, 20.0, 0.0),
            (100.0, 50.0, 0.0),
            (110.0, 30.0, 0.0),
        ],
        value_area_pct=0.70,
    )
    assert profile is not None
    assert profile.val_price == 100.0
    assert profile.vah_price == 110.0
    return profile


def _engine_with_btc_price(current: float, previous: float | None = None) -> SignalEngine:
    engine = SignalEngine()
    profile = _profile()
    engine._btc_volume_profile_context = lambda *_: {  # type: ignore[method-assign]
        "profile": profile,
        "exchange": "HYPERLIQUID_FUTURES",
        "raw_symbol": "BTC",
        "interval": "HOUR",
        "lookback_hours": 48,
        "price_timeframe": "1h",
        "current_price": current,
        "previous_price": previous,
    }
    return engine


def test_btc_volume_profile_penalizes_longs_when_btc_below_val() -> None:
    engine = _engine_with_btc_price(current=95.0, previous=101.0)
    score = _score(Direction.LONG)

    penalty = engine._btc_volume_profile_penalty(None, score, {})  # type: ignore[arg-type]

    assert penalty == 6
    assert score.breakdown["_btc_volume_profile"]["state"] == "below_val"
    assert score.breakdown["_btc_volume_profile"]["reason"] == "btc_below_val_risk_off"


def test_btc_volume_profile_penalizes_shorts_when_btc_above_vah() -> None:
    engine = _engine_with_btc_price(current=112.0, previous=108.0)
    score = _score(Direction.SHORT)

    penalty = engine._btc_volume_profile_penalty(None, score, {})  # type: ignore[arg-type]

    assert penalty == 6
    assert score.breakdown["_btc_volume_profile"]["state"] == "above_vah"
    assert score.breakdown["_btc_volume_profile"]["reason"] == "btc_above_vah_risk_on"


def test_btc_volume_profile_penalizes_longs_on_vah_rejection() -> None:
    engine = _engine_with_btc_price(current=109.0, previous=112.0)
    score = _score(Direction.LONG)

    penalty = engine._btc_volume_profile_penalty(None, score, {})  # type: ignore[arg-type]

    assert penalty == 4
    assert score.breakdown["_btc_volume_profile"]["vah_rejection"] is True
    assert score.breakdown["_btc_volume_profile"]["reason"] == "btc_vah_rejection_against_long"


def test_btc_volume_profile_context_only_when_concordant() -> None:
    engine = _engine_with_btc_price(current=112.0, previous=109.0)
    score = _score(Direction.LONG)

    penalty = engine._btc_volume_profile_penalty(None, score, {})  # type: ignore[arg-type]

    assert penalty == 0
    assert score.breakdown["_btc_volume_profile"]["state"] == "above_vah"
    assert score.breakdown["_btc_volume_profile"]["reason"] == "context_only"

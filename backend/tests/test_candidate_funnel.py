from datetime import UTC, datetime, timedelta

from app.engine.signal_engine import SignalEngine


def test_candidate_funnel_counts_deciles_and_resets_each_cycle() -> None:
    engine = SignalEngine()
    opened_at = datetime(2026, 10, 29, 12, tzinfo=UTC)
    assert engine._record_candidate_funnel(
        market_id=1, timeframe="4h", source_candle_open_time=opened_at,
        direction="LONG", score=69, allowed=False,
    ) is True
    engine._record_candidate_funnel(
        market_id=2, timeframe="4h", source_candle_open_time=opened_at,
        direction="LONG", score=70, allowed=True,
    )
    engine._record_candidate_funnel(
        market_id=3, timeframe="4h", source_candle_open_time=opened_at,
        direction="LONG", score=72, allowed=True,
    )
    engine._record_candidate_funnel(
        market_id=4, timeframe="4h", source_candle_open_time=opened_at,
        direction="LONG", score=100, allowed=True,
    )
    assert engine._record_candidate_funnel(
        market_id=1, timeframe="4h", source_candle_open_time=opened_at,
        direction="LONG", score=80, allowed=True,
    ) is False

    assert engine.cycle_candidate_funnel() == {
        "4h:LONG:60+:skip": 1,
        "4h:LONG:70+:pass": 2,
        "4h:LONG:90+:pass": 1,
    }
    engine.begin_cycle()
    assert engine.cycle_candidate_funnel() == {}
    assert engine._record_candidate_funnel(
        market_id=1, timeframe="4h", source_candle_open_time=opened_at,
        direction="LONG", score=80, allowed=True,
    ) is False
    assert engine.cycle_candidate_funnel() == {}
    engine._record_candidate_funnel(
        market_id=1, timeframe="4h", source_candle_open_time=opened_at + timedelta(hours=4),
        direction="LONG", score=80, allowed=True,
    )
    assert engine.cycle_candidate_funnel() == {"4h:LONG:80+:pass": 1}

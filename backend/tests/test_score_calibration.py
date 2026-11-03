from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models import ExperimentOutcome, Position, Signal
from app.engine.score_calibration import (
    DEFAULT_MIN_SAMPLES_FOR_USE,
    CalibrationTable,
    _pool_adjacent_violators,
    _position_r_multiple,
    compute_calibration_table,
    compute_experiment_calibration_table,
    serialize_table,
)


def _signal(signal_id: int, *, direction: str = "LONG", timeframe: str = "1h", score: int = 75) -> Signal:
    return Signal(
        id=signal_id,
        market_id=1,
        timeframe=timeframe,
        direction=direction,
        score=score,
        confidence_label="medium",
        entry_price=Decimal("100"),
        reason="test",
        strategy_breakdown={},
        status="new",
    )


def _position(
    *,
    realized_pnl: float,
    signal_id: int,
    entry: float = 100.0,
    stop: float | None = 95.0,
    size: float = 1.0,
    entry_atr: float | None = None,
    initial_position_risk: float | None = None,
    initial_size: float | None = None,
    initial_stop_loss: float | None = None,
    position_size: float | None = None,
) -> Position:
    metadata: dict = {"source_signal_id": signal_id}
    if entry_atr is not None:
        metadata["entry_atr"] = entry_atr
    if initial_position_risk is not None:
        metadata["initial_position_risk"] = initial_position_risk
    if initial_size is not None:
        metadata["initial_size"] = initial_size
    if initial_stop_loss is not None:
        metadata["initial_stop_loss"] = initial_stop_loss
    return Position(
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal(str(entry)),
        size=Decimal(str(position_size if position_size is not None else size)),
        stop_loss=Decimal(str(stop)) if stop is not None else None,
        status="closed",
        realized_pnl=Decimal(str(realized_pnl)),
        metadata_json=metadata,
    )


def test_position_r_multiple_uses_stop_distance() -> None:
    # entry 100, stop 95 => initial_risk per unit = 5; pnl 10 con size 1 => R = 2.0
    pos = _position(realized_pnl=10.0, signal_id=1, entry=100, stop=95, size=1)
    assert _position_r_multiple(pos) == pytest.approx(2.0)


def test_position_r_multiple_fallback_to_atr() -> None:
    pos = _position(realized_pnl=8.0, signal_id=1, entry=100, stop=None, size=2, entry_atr=2.0)
    # risk = atr (2) * size (2) = 4 => R = 8/4 = 2.0
    assert _position_r_multiple(pos) == pytest.approx(2.0)


def test_position_r_multiple_returns_none_when_no_risk_info() -> None:
    pos = _position(realized_pnl=8.0, signal_id=1, entry=100, stop=None, size=1, entry_atr=None)
    assert _position_r_multiple(pos) is None


def test_position_r_multiple_prefers_initial_position_risk() -> None:
    # Caso reale: size=0 dopo close, ma metadata.initial_position_risk presente
    pos = _position(
        realized_pnl=200.0,
        signal_id=1,
        entry=100,
        stop=None,
        position_size=0,
        initial_position_risk=200.0,
    )
    assert _position_r_multiple(pos) == pytest.approx(1.0)


def test_position_r_multiple_uses_initial_size_when_size_zeroed() -> None:
    # size=0 (post-close) ma initial_size + initial_stop_loss disponibili
    pos = _position(
        realized_pnl=10.0,
        signal_id=1,
        entry=100,
        stop=None,
        position_size=0,
        initial_size=1.0,
        initial_stop_loss=95.0,
    )
    # risk = |100-95| * 1 = 5, R = 10/5 = 2
    assert _position_r_multiple(pos) == pytest.approx(2.0)


def test_compute_calibration_excludes_positions_without_signal() -> None:
    pos_without_signal_link = _position(realized_pnl=5.0, signal_id=999)
    table = compute_calibration_table([pos_without_signal_link], {})
    assert table.buckets == {}
    assert table.excluded_count == 1


def test_compute_calibration_aggregates_by_bucket() -> None:
    signals = {1: _signal(1, direction="LONG", timeframe="1h", score=75)}
    positions = [
        _position(realized_pnl=10.0, signal_id=1),  # R = 2
        _position(realized_pnl=-5.0, signal_id=1),  # R = -1
        _position(realized_pnl=10.0, signal_id=1),  # R = 2
        _position(realized_pnl=-5.0, signal_id=1),  # R = -1
    ]
    table = compute_calibration_table(positions, signals, prior_strength=0.0)
    metrics = table.buckets["LONG:1h:70-79"]
    assert metrics.n_samples == 4
    assert metrics.wins == 2
    assert metrics.raw_win_rate == pytest.approx(0.5)
    # senza prior, shrunk == raw
    assert metrics.shrunk_win_rate == pytest.approx(0.5)
    assert metrics.mean_r_win == pytest.approx(2.0)
    assert metrics.mean_r_loss == pytest.approx(-1.0)
    # expected_r = 0.5 * 2 + 0.5 * -1 = 0.5
    assert metrics.expected_r == pytest.approx(0.5)


def test_shrinkage_pulls_small_samples_toward_prior() -> None:
    signals = {1: _signal(1, score=85)}
    # solo 2 vittorie → raw_wr = 1.0, ma con prior strength 20 il posterior è ~0.55
    positions = [
        _position(realized_pnl=10.0, signal_id=1),
        _position(realized_pnl=10.0, signal_id=1),
    ]
    table = compute_calibration_table(positions, signals, prior_strength=20.0)
    metrics = table.buckets["LONG:1h:80+"]
    assert metrics.raw_win_rate == pytest.approx(1.0)
    # (10 + 2) / (20 + 2) = 12/22 ≈ 0.545
    assert metrics.shrunk_win_rate == pytest.approx(12 / 22)


def test_shrinkage_converges_to_raw_with_large_sample() -> None:
    signals = {1: _signal(1, score=75)}
    positions = []
    # 60 vittorie + 40 perdite → wr = 0.6
    for _ in range(60):
        positions.append(_position(realized_pnl=10.0, signal_id=1))
    for _ in range(40):
        positions.append(_position(realized_pnl=-5.0, signal_id=1))
    table = compute_calibration_table(positions, signals, prior_strength=20.0)
    metrics = table.buckets["LONG:1h:70-79"]
    assert metrics.raw_win_rate == pytest.approx(0.6)
    # con n=100 vs prior strength 20: pull verso 0.5 limitato
    # shrunk = (10 + 60) / (20 + 100) = 70/120 ≈ 0.583
    assert metrics.shrunk_win_rate == pytest.approx(70 / 120)
    assert abs(metrics.shrunk_win_rate - metrics.raw_win_rate) < 0.05


def test_confidence_label_reflects_sample_size() -> None:
    signals = {
        1: _signal(1, timeframe="1h", score=75),
        2: _signal(2, timeframe="5m", score=85),
    }
    positions = []
    # bucket LONG:1h:70-79 ha pochi sample → low
    for _ in range(2):
        positions.append(_position(realized_pnl=5.0, signal_id=1))
    # bucket LONG:5m:80+ ha tanti sample → high
    for _ in range(DEFAULT_MIN_SAMPLES_FOR_USE * 3 + 1):
        positions.append(_position(realized_pnl=5.0, signal_id=2))
    table = compute_calibration_table(positions, signals)
    assert table.buckets["LONG:1h:70-79"].confidence == "low"
    assert table.buckets["LONG:5m:80+"].confidence == "high"


def test_lookup_returns_none_for_unknown_direction() -> None:
    table = CalibrationTable(
        buckets={},
        computed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        lookback_positions=500,
        excluded_count=0,
        prior_strength=20.0,
        min_samples_for_use=10,
    )
    assert table.lookup(direction="NEUTRAL", timeframe="1h", score=75) is None


def test_short_and_long_are_calibrated_separately() -> None:
    signals = {
        1: _signal(1, direction="LONG", timeframe="1h", score=75),
        2: _signal(2, direction="SHORT", timeframe="1h", score=75),
    }
    positions = [
        _position(realized_pnl=10.0, signal_id=1),
        _position(realized_pnl=-5.0, signal_id=2),
    ]
    table = compute_calibration_table(positions, signals, prior_strength=0.0)
    assert "LONG:1h:70-79" in table.buckets
    assert "SHORT:1h:70-79" in table.buckets
    assert table.buckets["LONG:1h:70-79"].raw_win_rate == pytest.approx(1.0)
    assert table.buckets["SHORT:1h:70-79"].raw_win_rate == pytest.approx(0.0)


def test_pool_adjacent_violators_enforces_monotonic() -> None:
    # input non monotonico: 0.4, 0.2, 0.6 → primi due si fondono in 0.3, poi 0.3, 0.6
    smoothed = _pool_adjacent_violators([0.4, 0.2, 0.6], [10.0, 10.0, 10.0])
    assert smoothed[0] == pytest.approx(0.3)
    assert smoothed[1] == pytest.approx(0.3)
    assert smoothed[2] == pytest.approx(0.6)
    assert smoothed[0] <= smoothed[1] <= smoothed[2]


def test_pool_adjacent_violators_preserves_already_monotonic() -> None:
    values = [0.1, 0.3, 0.5, 0.7]
    smoothed = _pool_adjacent_violators(values, [1.0] * 4)
    for v_in, v_out in zip(values, smoothed, strict=False):
        assert v_out == pytest.approx(v_in)


def test_enforce_monotonic_smooths_inversion_within_direction_tf() -> None:
    signals = {
        1: _signal(1, direction="LONG", timeframe="1h", score=65),  # bucket 60-69
        2: _signal(2, direction="LONG", timeframe="1h", score=75),  # bucket 70-79
        3: _signal(3, direction="LONG", timeframe="1h", score=85),  # bucket 80+
    }
    positions = [
        # bucket 60-69 → wins
        _position(realized_pnl=10.0, signal_id=1),
        _position(realized_pnl=10.0, signal_id=1),
        # bucket 70-79 → losses (inversione)
        _position(realized_pnl=-5.0, signal_id=2),
        _position(realized_pnl=-5.0, signal_id=2),
        # bucket 80+ → wins
        _position(realized_pnl=10.0, signal_id=3),
        _position(realized_pnl=10.0, signal_id=3),
    ]
    smoothed = compute_calibration_table(
        positions, signals, prior_strength=0.0, enforce_monotonic=True
    )
    ers = [
        smoothed.buckets[f"LONG:1h:{b}"].expected_r
        for b in ("60-69", "70-79", "80+")
    ]
    assert ers[0] <= ers[1] <= ers[2] + 1e-9


def test_serialize_table_is_json_friendly() -> None:
    signals = {1: _signal(1, score=75)}
    positions = [_position(realized_pnl=10.0, signal_id=1)]
    table = compute_calibration_table(positions, signals)
    payload = serialize_table(table)
    assert "buckets" in payload
    assert "computed_at" in payload
    assert isinstance(payload["computed_at"], str)
    bucket = payload["buckets"]["LONG:1h:70-79"]
    assert isinstance(bucket["expected_r"], float)
    assert bucket["confidence"] in {"low", "medium", "high"}


def test_experiment_calibration_uses_only_forward_net_r() -> None:
    signal = _signal(7, direction="SHORT", timeframe="15m", score=72)
    outcomes = [
        ExperimentOutcome(
            position_id=index,
            signal_id=7,
            market_id=1,
            experiment_version="baseline-frozen-v2",
            configuration_hash="hash",
            assignment="champion",
            timeframe="15m",
            side="sell",
            barrier_label="tp_first" if net_r > 0 else "stop_first",
            opened_at=datetime(2026, 7, 20, tzinfo=UTC),
            closed_at=datetime(2026, 7, 21, tzinfo=UTC),
            entry_price=Decimal("100"),
            net_r=Decimal(str(net_r)),
        )
        for index, net_r in enumerate((1.5, -1.0, 0.5), start=1)
    ]

    table = compute_experiment_calibration_table(
        outcomes,
        {7: signal},
        prior_strength=0,
        min_samples_for_use=2,
        lookback_positions=3,
        experiment_version="baseline-frozen-v2",
        assignment="champion",
    )

    bucket = table.buckets["SHORT:15m:70-79"]
    assert bucket.n_samples == 3
    assert bucket.expected_r == pytest.approx((1.5 - 1.0 + 0.5) / 3)
    assert table.sample_source == "experiment_outcomes"
    assert table.experiment_version == "baseline-frozen-v2"

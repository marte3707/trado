"""Test della penalità di pressione liquidazioni (kiyotaka) nel SignalEngine."""

from __future__ import annotations

from app.core.config import Settings
from app.core.enums import Direction
from app.db.models import Market
from app.engine.scoring import ScoreResult
from app.engine.signal_engine import SignalEngine
from app.services.kiotaka import KiotakaClient, LiqPressure


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://x:x@localhost/x",
        hyperliquid_network="testnet",
    )


def _pressure(
    *,
    short_recent: float = 0.0,
    long_recent: float = 0.0,
    short_baseline_hourly: float = 0.0,
    long_baseline_hourly: float = 0.0,
    points: int = 24,
) -> LiqPressure:
    return LiqPressure(
        short_recent=short_recent,
        long_recent=long_recent,
        short_baseline_hourly=short_baseline_hourly,
        long_baseline_hourly=long_baseline_hourly,
        recent_hours=1,
        baseline_hours=24,
        points=points,
        fetched_at=0.0,
    )


def _score(direction: Direction) -> ScoreResult:
    return ScoreResult(
        direction=direction,
        score=70,
        confidence_label="buono",
        reason="test",
        breakdown={},
    )


def _engine_with_pressure(pressure: LiqPressure | None) -> SignalEngine:
    engine = SignalEngine()
    engine._kiotaka.liquidation_pressure = lambda **_: pressure  # type: ignore[assignment]
    return engine


CONFIG = {
    "liq_pressure_ratio_threshold": 3.0,
    "liq_max_penalty": 15,
}


# --------------------------------------------------------------------------- #
# LiqPressure: calcolo dei ratio
# --------------------------------------------------------------------------- #
def test_pressure_ratio_short() -> None:
    # recent 300k/h vs baseline 50k/h => 6x
    p = _pressure(short_recent=300_000, short_baseline_hourly=50_000)
    assert p.short_pressure_ratio == 6.0


def test_pressure_ratio_zero_baseline() -> None:
    # baseline nullo => ratio 0 (niente divisione per zero, nessuna penalità)
    p = _pressure(short_recent=300_000, short_baseline_hourly=0.0)
    assert p.short_pressure_ratio == 0.0


# --------------------------------------------------------------------------- #
# Penalità: scenario ZEC (short squeeze contro un segnale SHORT)
# --------------------------------------------------------------------------- #
def test_penalty_on_short_squeeze_against_short() -> None:
    # ratio 6x, threshold 3x => scaled (6-3)/3 = 1.0 => max penalty
    engine = _engine_with_pressure(
        _pressure(short_recent=300_000, short_baseline_hourly=50_000)
    )
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.SHORT), CONFIG
    )
    assert penalty == 15


def test_penalty_scales_with_intensity() -> None:
    # ratio 4.5x, threshold 3x => scaled 0.5 => round(15*0.5)=8
    engine = _engine_with_pressure(
        _pressure(short_recent=225_000, short_baseline_hourly=50_000)
    )
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.SHORT), CONFIG
    )
    assert penalty == 8


def test_no_penalty_below_threshold() -> None:
    # ratio 2x < threshold 3x => nessuna penalità, ma breakdown popolato
    engine = _engine_with_pressure(
        _pressure(short_recent=100_000, short_baseline_hourly=50_000)
    )
    score = _score(Direction.SHORT)
    penalty = engine._liquidation_pressure_penalty(Market(base="ZEC"), score, CONFIG)
    assert penalty == 0
    assert score.breakdown["_liquidation_pressure"]["contra_ratio"] == 2.0


def test_concordant_liquidations_ignored() -> None:
    # SHORT con grosso long-flush (concordante, prezzo giù) ma short squeeze nullo:
    # guardiamo solo il lato contrario => nessuna penalità.
    engine = _engine_with_pressure(
        _pressure(long_recent=500_000, long_baseline_hourly=10_000)
    )
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.SHORT), CONFIG
    )
    assert penalty == 0


def test_penalty_on_long_flush_against_long() -> None:
    engine = _engine_with_pressure(
        _pressure(long_recent=300_000, long_baseline_hourly=50_000)
    )
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.LONG), CONFIG
    )
    assert penalty == 15


# --------------------------------------------------------------------------- #
# Fail-open
# --------------------------------------------------------------------------- #
def test_fail_open_when_pressure_missing() -> None:
    engine = _engine_with_pressure(None)
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.SHORT), CONFIG
    )
    assert penalty == 0


def test_fail_open_when_too_few_points() -> None:
    engine = _engine_with_pressure(
        _pressure(short_recent=300_000, short_baseline_hourly=50_000, points=1)
    )
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.SHORT), CONFIG
    )
    assert penalty == 0


def test_disabled_when_max_penalty_zero() -> None:
    engine = _engine_with_pressure(
        _pressure(short_recent=300_000, short_baseline_hourly=50_000)
    )
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.SHORT), {"liq_max_penalty": 0}
    )
    assert penalty == 0


def test_neutral_direction_no_penalty() -> None:
    engine = _engine_with_pressure(
        _pressure(short_recent=300_000, short_baseline_hourly=50_000)
    )
    penalty = engine._liquidation_pressure_penalty(
        Market(base="ZEC"), _score(Direction.NEUTRAL), CONFIG
    )
    assert penalty == 0


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def test_client_liquidation_disabled_without_key() -> None:
    settings = _settings()
    settings.kiotaka_api_key = None
    client = KiotakaClient(settings)
    assert client.liquidation_pressure("ZEC") is None

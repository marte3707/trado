from app.core.enums import Direction
from app.engine.scoring import SignalScoringEngine
from app.strategies.base import StrategyResult


def test_scoring_prefers_aligned_long_results() -> None:
    results = [
        StrategyResult("ema_crossover", "perp", "BTC", "5m", Direction.LONG, 80, "ema", {}),
        StrategyResult("macd", "perp", "BTC", "5m", Direction.LONG, 70, "macd", {}),
        StrategyResult("rsi", "perp", "BTC", "5m", Direction.NEUTRAL, 0, "rsi", {}),
    ]
    score = SignalScoringEngine().score(results, {"ema_crossover": 20, "macd": 15, "rsi": 10})
    assert score.direction == Direction.LONG
    assert score.score > 0
    assert score.breakdown["ema_crossover"]["contribution"] == 16


def test_scoring_high_participation_ratio_drops_weak_signals_to_neutral() -> None:
    # 1 strategia su 3 vota → 33% del peso totale.
    results = [
        StrategyResult("ema_crossover", "perp", "BTC", "5m", Direction.LONG, 80, "ema", {}),
        StrategyResult("macd", "perp", "BTC", "5m", Direction.NEUTRAL, 0, "macd", {}),
        StrategyResult("rsi", "perp", "BTC", "5m", Direction.NEUTRAL, 0, "rsi", {}),
    ]
    weights = {"ema_crossover": 10, "macd": 10, "rsi": 10}
    score_default = SignalScoringEngine().score(results, weights)
    assert score_default.direction == Direction.NEUTRAL or score_default.score == 0
    score_relaxed = SignalScoringEngine().score(results, weights, participation_ratio=0.10)
    assert score_relaxed.direction in {Direction.LONG, Direction.STRONG_LONG}
    assert score_relaxed.score > 0


def test_scoring_participation_ratio_via_init() -> None:
    results = [
        StrategyResult("ema_crossover", "perp", "BTC", "5m", Direction.LONG, 80, "ema", {}),
        StrategyResult("macd", "perp", "BTC", "5m", Direction.LONG, 70, "macd", {}),
        StrategyResult("rsi", "perp", "BTC", "5m", Direction.NEUTRAL, 0, "rsi", {}),
    ]
    weights = {"ema_crossover": 10, "macd": 10, "rsi": 80}
    # default 0.35 → voting_weight 20 / all 100 = 20% < 35% → segnale annullato
    score_strict = SignalScoringEngine().score(results, weights)
    assert score_strict.score == 0
    # con ratio 0.10 → 20% > 10% → passa
    score_loose = SignalScoringEngine(participation_ratio=0.10).score(results, weights)
    assert score_loose.direction in {Direction.LONG, Direction.STRONG_LONG}
    assert score_loose.score > 0


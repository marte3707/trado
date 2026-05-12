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


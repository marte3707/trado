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
    # Dopo il fix F-03 (contributo float) lo score è leggermente più alto del round-truncato
    # precedente; entrambe le direzioni indicano alignment long.
    assert score.direction in {Direction.LONG, Direction.STRONG_LONG}
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


def test_scoring_parity_returns_score_zero() -> None:
    """F-04: in caso di parità tra long e short, il segnale è NEUTRAL e score=0
    (non il valore positivo comune, che confondeva i filtri downstream)."""
    results = [
        StrategyResult("a", "perp", "BTC", "5m", Direction.LONG, 70, "a", {}),
        StrategyResult("b", "perp", "BTC", "5m", Direction.SHORT, 70, "b", {}),
    ]
    weights = {"a": 10, "b": 10}
    score = SignalScoringEngine(participation_ratio=0.10).score(results, weights)
    assert score.direction == Direction.NEUTRAL
    assert score.score == 0


def test_scoring_weak_contribution_preserved_as_float() -> None:
    """F-03: confidenza piccola (es. 4) con peso 10 ora contribuisce ~0.4 invece
    di essere arrotondata a 0 — la strategia non sparisce silenziosamente."""
    results = [
        StrategyResult("weak", "perp", "BTC", "5m", Direction.LONG, 4, "weak", {}),
        StrategyResult("strong", "perp", "BTC", "5m", Direction.LONG, 80, "strong", {}),
    ]
    weights = {"weak": 10, "strong": 10}
    score = SignalScoringEngine(participation_ratio=0.10).score(results, weights)
    # contributo float preservato in breakdown
    assert score.breakdown["weak"]["contribution"] == 0.4


def test_scoring_strong_threshold_configurable() -> None:
    """F-13: la soglia STRONG è ora configurabile via init."""
    results = [
        StrategyResult("a", "perp", "BTC", "5m", Direction.LONG, 70, "a", {}),
    ]
    weights = {"a": 10}
    # threshold default 75 → score 70 → LONG (non STRONG)
    score_default = SignalScoringEngine(participation_ratio=0.10).score(results, weights)
    assert score_default.direction == Direction.LONG
    # threshold 60 → score 70 → STRONG_LONG
    score_low = SignalScoringEngine(
        participation_ratio=0.10, strong_score_threshold=60
    ).score(results, weights)
    assert score_low.direction == Direction.STRONG_LONG


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


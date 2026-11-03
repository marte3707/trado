from app.engine.performance import PerformanceStats
from scripts.edge_oos_report import _verdict


def _stats(profit_factor: float | None) -> PerformanceStats:
    return PerformanceStats(
        trades=20,
        wins=10,
        losses=10,
        win_rate=0.5,
        realized_pnl=0.0,
        gross_profit=100.0,
        gross_loss=100.0,
        profit_factor=profit_factor,
    )


def test_oos_verdict_calls_concordant_history_a_candidate_not_an_edge() -> None:
    assert _verdict(_stats(0.5), _stats(1.5)) == "TEST+"
    assert _verdict(_stats(1.0), _stats(1.5)) == "CANDIDATO"


def test_oos_verdict_keeps_insufficient_and_negative_states_distinct() -> None:
    assert _verdict(None, None) == "INSUFF"
    assert _verdict(_stats(1.1), _stats(0.8)) == "NEG"

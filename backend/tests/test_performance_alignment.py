from decimal import Decimal

from app.db.models import Position, Signal
from app.engine.performance import SignalPerformanceAnalyzer, aligned_strategy_names


def test_aligned_strategy_names_excludes_opposite_votes() -> None:
    breakdown = {
        "wyckoff_spring_upthrust": {"direction": "LONG", "contribution": 12},
        "ema_crossover": {"direction": "SHORT", "contribution": 8},
        "neutral": {"direction": "NEUTRAL", "contribution": 99},
    }

    assert aligned_strategy_names("SHORT", breakdown) == {"ema_crossover"}
    assert aligned_strategy_names("STRONG_LONG", breakdown) == {"wyckoff_spring_upthrust"}


def test_strategy_positions_only_attributes_votes_aligned_with_opened_trade() -> None:
    signal = Signal(
        id=17,
        market_id=1,
        timeframe="4h",
        direction="SHORT",
        score=80,
        confidence_label="high",
        entry_price=Decimal("100"),
        reason="test",
        strategy_breakdown={
            "wyckoff_spring_upthrust": {"direction": "LONG", "contribution": 12},
            "ema_crossover": {"direction": "SHORT", "contribution": 8},
        },
    )
    position = Position(
        id=9,
        market_id=1,
        mode="paper",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="closed",
        realized_pnl=Decimal("5"),
        metadata_json={"source_signal_id": signal.id, "source_timeframe": "4h"},
    )

    class Query:
        def filter(self, *_args: object) -> "Query":
            return self

        def all(self) -> list[Signal]:
            return [signal]

    class Db:
        def query(self, model: object) -> Query:
            assert model is Signal
            return Query()

    analyzer = SignalPerformanceAnalyzer()
    analyzer.closed_paper_positions = lambda *_args, **_kwargs: [position]  # type: ignore[method-assign]

    grouped = analyzer.strategy_positions(Db(), timeframe="4h")  # type: ignore[arg-type]

    assert grouped == {"ema_crossover": [position]}

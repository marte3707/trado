from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import Candle, DecisionLog
from scripts.candidate_shadow_report import (
    ShadowObservation,
    _metrics,
    _period_metrics,
    _take_period,
    first_post_decision_entry,
    fixed_horizon_net_return,
    score_band,
    shadow_report,
    split_fixed_cohort,
)


def _bar(open_time: datetime, open_price: str, close_price: str) -> Candle:
    return Candle(
        market_id=1,
        timeframe="4h",
        open_time=open_time,
        open=Decimal(open_price),
        high=max(Decimal(open_price), Decimal(close_price)),
        low=min(Decimal(open_price), Decimal(close_price)),
        close=Decimal(close_price),
        volume=Decimal("1"),
    )


def test_shadow_return_enters_next_bar_and_waits_for_final_close() -> None:
    source = datetime(2026, 10, 29, 8, tzinfo=UTC)
    bars = [
        _bar(source + timedelta(hours=4), "100", "101"),
        _bar(source + timedelta(hours=8), "101", "102"),
        _bar(source + timedelta(hours=12), "102", "103"),
    ]
    fee = Decimal("0.00045")
    entry = _bar(source + timedelta(hours=4, minutes=15), "100", "100")
    entry.timeframe = "15m"

    assert fixed_horizon_net_return(
        source, bars, entry=entry, direction="LONG", horizon=1,
        taker_fee_rate=fee, as_of=source + timedelta(hours=8),
    ) == Decimal("0.0090955")
    assert fixed_horizon_net_return(
        source, bars, entry=entry, direction="SHORT", horizon=1,
        taker_fee_rate=fee, as_of=source + timedelta(hours=8),
    ) == Decimal("-0.0109045")
    assert fixed_horizon_net_return(
        source, bars, entry=entry, direction="LONG", horizon=3,
        taker_fee_rate=fee, as_of=source + timedelta(hours=15),
    ) is None
    assert fixed_horizon_net_return(
        source, bars, entry=entry, direction="LONG", horizon=3,
        taker_fee_rate=fee, as_of=source + timedelta(hours=16),
    ) == Decimal("0.0290865")


def test_shadow_return_rejects_missing_intermediate_candle() -> None:
    source = datetime(2026, 10, 29, 8, tzinfo=UTC)
    bars = [
        _bar(source + timedelta(hours=4), "100", "101"),
        _bar(source + timedelta(hours=12), "102", "103"),
    ]
    entry = _bar(source + timedelta(hours=4, minutes=15), "100", "100")
    entry.timeframe = "15m"

    assert fixed_horizon_net_return(
        source, bars, entry=entry, direction="LONG", horizon=3,
        taker_fee_rate=Decimal("0.00045"),
        as_of=source + timedelta(hours=16),
    ) is None


def test_shadow_entry_uses_first_15m_open_after_decision() -> None:
    decision_at = datetime(2026, 10, 29, 16, 1, tzinfo=UTC)
    before = _bar(datetime(2026, 10, 29, 16, tzinfo=UTC), "100", "100")
    before.timeframe = "15m"
    after = _bar(datetime(2026, 10, 29, 16, 15, tzinfo=UTC), "102", "102")
    after.timeframe = "15m"
    too_late = _bar(datetime(2026, 10, 29, 16, 30, tzinfo=UTC), "103", "103")
    too_late.timeframe = "15m"

    assert first_post_decision_entry(decision_at, [before, too_late, after]) is after
    assert first_post_decision_entry(decision_at, [before, too_late]) is None


def test_shadow_return_rejects_entry_after_exit() -> None:
    source = datetime(2026, 10, 29, 8, tzinfo=UTC)
    exit_bar = _bar(source + timedelta(hours=4), "100", "101")
    entry = _bar(source + timedelta(hours=8), "101", "101")
    entry.timeframe = "15m"

    assert fixed_horizon_net_return(
        source, [exit_bar], entry=entry, direction="LONG", horizon=1,
        taker_fee_rate=Decimal("0.00045"), as_of=source + timedelta(hours=8),
    ) is None


def test_shadow_score_bands_and_net_criteria() -> None:
    assert [score_band(score) for score in (49, 50, 69, 70, 80)] == [
        "00-49", "50-59", "60-69", "70-79", "80-100",
    ]
    metrics = _metrics([Decimal("0.02"), Decimal("-0.01")])
    assert metrics["average_net_return"] == "0.005"
    assert metrics["profit_factor"] == "2"
    assert metrics["criteria_met"] is True


def test_period_keeps_whole_source_candles_and_missing_outcome() -> None:
    source = datetime(2026, 7, 20, 8, tzinfo=UTC)
    rows = [
        ShadowObservation(source + timedelta(hours=4 * bucket), market, None if market == 1 else Decimal("0.01"),
                          "missing" if market == 1 else "ready")
        for bucket in range(2)
        for market in range(1, 6)
    ]
    taken, cursor, ready = _take_period(rows, min_entries=6, min_source_candles=2)

    assert ready is True
    assert cursor == 10
    assert len(taken) == 10
    assert _period_metrics(taken)["criteria_met"] is False
    assert _period_metrics(taken)["missing_market_data"] == 2


def test_fixed_split_embargoes_overlapping_horizon_and_time_groups() -> None:
    source = datetime(2026, 7, 20, 8, tzinfo=UTC)
    rows = [
        ShadowObservation(source + timedelta(hours=4 * bucket), market, Decimal("0.01"), "ready")
        for bucket in range(40)
        for market in range(1, 6)
    ]
    train, holdout, embargo_until, ready = split_fixed_cohort(rows, horizon=6)

    assert ready is True
    assert len(train) == 100  # 20 whole source candles, five markets each
    assert len(holdout) == 50  # 10 later source candles, five markets each
    assert embargo_until == source + timedelta(hours=4 * 26)
    assert min(row.source_open for row in holdout) == source + timedelta(hours=4 * 27)
    assert {row.source_open for row in train}.isdisjoint(
        {row.source_open for row in holdout}
    )


def test_shadow_report_keeps_follow_and_fade_separate() -> None:
    source = datetime(2026, 7, 20, 8, tzinfo=UTC)
    bars = [
        _bar(source + timedelta(hours=4 * index), "100", str(100 + index))
        for index in range(1, 7)
    ]
    entry = _bar(source + timedelta(hours=4, minutes=15), "100.5", "100.5")
    entry.timeframe = "15m"
    log = DecisionLog(
        id=1,
        timestamp=source + timedelta(hours=4, minutes=1),
        event_type="candidate_shadow_observation",
        market_id=1,
        message="test",
        data={
            "source_candle_open_time": source.isoformat(),
            "timeframe": "4h",
            "direction": "LONG",
            "score": 72,
            "aligned_strategies": ["wyckoff_spring_upthrust"],
            "configuration_hash": "hash",
            "taker_fee_rate": "0.00045",
        },
    )
    duplicate = DecisionLog(
        id=2,
        timestamp=source + timedelta(hours=4, minutes=2),
        event_type="candidate_shadow_observation",
        market_id=1,
        message="duplicate after worker restart",
        data=dict(log.data),
    )

    class Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args):
            return self

        def order_by(self, *args):
            return self

        def all(self):
            return self.rows

    class Database:
        def __init__(self):
            self.candle_query_count = 0

        def query(self, model):
            if model is DecisionLog:
                return Query([log, duplicate])
            self.candle_query_count += 1
            return Query([entry] if self.candle_query_count == 1 else bars)

    report = shadow_report(Database())
    cells = {
        (cell["orientation"], cell["horizon_bars"]): cell
        for cell in report["cells"]
    }

    assert report["raw_observations"] == 2
    assert report["duplicates_ignored"] == 1
    assert {cell["cohort"] for cell in report["cells"]} == {
        "score:70-79", "wyckoff_long:aligned",
    }
    exit_ratio = Decimal("101") / Decimal("100.5")
    fee = Decimal("0.00045") * (1 + exit_ratio)
    assert Decimal(cells[("follow", 1)]["train"]["average_net_return"]) == exit_ratio - 1 - fee
    assert Decimal(cells[("fade", 1)]["train"]["average_net_return"]) == 1 - exit_ratio - fee
    assert all(cell["status"] == "insufficient" for cell in cells.values())

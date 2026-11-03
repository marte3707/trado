from app.api.routers import analytics as routes
from app.db.models import StrategyConfig
from app.engine.performance import PerformanceStats


def test_oos_verdict_requires_train_test_concordance() -> None:
    weak_train = {"trades": 20, "profit_factor": 0.6}
    strong_test = {"trades": 10, "profit_factor": 1.5}
    solid_train = {"trades": 20, "profit_factor": 1.1}

    assert routes._oos_verdict(weak_train, strong_test) == "TEST+"
    assert routes._oos_verdict(solid_train, strong_test) == "ROBUSTO"


def test_analytics_outcome_profile_delegates_to_builder(monkeypatch) -> None:  # noqa: ANN001
    seen: dict[str, object] = {}

    def fake_builder(db, *, lookback: int, mode: str | None = None):  # noqa: ANN001
        seen["db"] = db
        seen["lookback"] = lookback
        seen["mode"] = mode
        return {"filters": {"lookback": lookback, "mode": mode}, "overall": {"trades": 0}}

    db = object()
    monkeypatch.setattr(routes, "build_outcome_profile", fake_builder)

    payload = routes.analytics_outcome_profile(lookback=250, mode=None, db=db)  # type: ignore[arg-type]

    assert seen == {"db": db, "lookback": 250, "mode": None}
    assert payload["filters"]["lookback"] == 250


def test_analytics_outcome_profile_forwards_mode_filter(monkeypatch) -> None:  # noqa: ANN001
    seen: dict[str, object] = {}

    def fake_builder(db, *, lookback: int, mode: str | None = None):  # noqa: ANN001
        seen["mode"] = mode
        return {"filters": {"lookback": lookback, "mode": mode}, "overall": {"trades": 0}}

    monkeypatch.setattr(routes, "build_outcome_profile", fake_builder)

    payload = routes.analytics_outcome_profile(lookback=500, mode="paper", db=object())  # type: ignore[arg-type]

    assert seen["mode"] == "paper"
    assert payload["filters"]["mode"] == "paper"


def test_analytics_strategy_edge_returns_oos_and_attribution_payload(monkeypatch) -> None:  # noqa: ANN001
    stats = PerformanceStats(
        trades=60,
        wins=36,
        losses=24,
        win_rate=0.6,
        realized_pnl=120.0,
        gross_profit=240.0,
        gross_loss=120.0,
        profit_factor=2.0,
    )

    class FakeAnalyzer:
        def closed_paper_positions(self, db, limit=500):  # noqa: ANN001
            return [object()] * 30

        def summarize_positions(self, positions):  # noqa: ANN001
            return stats

        def strategy_positions(self, db, timeframe=None, market_id=None, limit=500):  # noqa: ANN001
            return {"rsi": [object()] * 30}

        @staticmethod
        def holdout_split_stats(
            positions,  # noqa: ANN001
            *,
            train_ratio: float,
            min_samples_per_side: int,
        ):
            return stats, stats

        @staticmethod
        def time_weighted_summarize(positions, *, half_life: int):  # noqa: ANN001
            return stats

    class FakeQuery:
        def all(self) -> list[StrategyConfig]:
            return [
                StrategyConfig(
                    strategy_name="rsi",
                    timeframe="5m",
                    enabled=True,
                    params={},
                    weight=10,
                )
            ]

    class FakeDb:
        def query(self, model):  # noqa: ANN001
            assert model is StrategyConfig
            return FakeQuery()

    monkeypatch.setattr(routes, "SignalPerformanceAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(
        routes,
        "build_strategy_attribution",
        lambda db, *, lookback, recent_window: {  # noqa: ANN001
            "positions_linked": 30,
            "strategies": {
                "rsi": {
                    "overall": routes.serialize_stats(stats),
                    "recent": routes.serialize_stats(stats),
                    "avg_contribution": 12.5,
                    "participation": {"traded_share": 1.0},
                }
            },
        },
    )

    payload = routes.analytics_strategy_edge(
        lookback=500,
        recent_window=150,
        min_test_samples=10,
        db=FakeDb(),  # type: ignore[arg-type]
    )

    assert payload["filters"] == {
        "lookback": 500,
        "recent_window": 150,
        "min_test_samples": 10,
    }
    assert payload["overall"]["classification"] == "robust"
    assert payload["summary"]["cells"]["robust"] == 1
    assert payload["summary"]["strategies"]["robust"] == 1
    assert payload["cells"][0]["key"] == "5m:rsi"
    assert payload["strategies"][0]["name"] == "rsi"


def test_analytics_live_eligibility_forwards_thresholds(monkeypatch) -> None:  # noqa: ANN001
    seen: dict[str, object] = {}

    class FakeSettings:
        live_taker_fee_rate = 0.00042

    def fake_builder(db, **kwargs):  # noqa: ANN001
        seen["db"] = db
        seen.update(kwargs)
        return {"filters": {"lookback": kwargs["lookback"]}, "summary": {"eligible": 0}}

    monkeypatch.setattr(routes, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(routes, "build_live_eligibility_report", fake_builder)

    db = object()
    payload = routes.analytics_live_eligibility(
        lookback=800,
        recent_window=120,
        min_trades=25,
        min_recent_trades=8,
        min_net_profit_factor=1.1,
        min_recent_net_profit_factor=1.0,
        min_avg_net_r=0.03,
        min_recent_avg_net_r=0.01,
        db=db,  # type: ignore[arg-type]
    )

    assert payload["filters"]["lookback"] == 800
    assert seen["db"] is db
    assert seen["taker_fee_rate"] == 0.00042
    assert seen["min_trades"] == 25
    assert seen["min_recent_trades"] == 8
    assert seen["min_avg_net_r"] == 0.03

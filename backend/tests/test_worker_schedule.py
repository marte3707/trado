from app.worker import main as worker


def test_active_timeframes_use_boundary_window_without_history() -> None:
    worker._LAST_TIMEFRAME_RUN_MS.clear()

    assert worker.active_timeframes_for_tick(["5m", "15m"], 15 * 60 * 1000 + 30_000) == [
        "5m",
        "15m",
    ]


def test_active_timeframes_catch_up_after_long_cycle() -> None:
    worker._LAST_TIMEFRAME_RUN_MS.clear()
    worker.mark_timeframes_run(["5m"], 0)

    assert worker.active_timeframes_for_tick(["5m"], 6 * 60 * 1000) == ["5m"]


def test_active_timeframes_do_not_run_unknown_timeframes() -> None:
    worker._LAST_TIMEFRAME_RUN_MS.clear()

    assert worker.active_timeframes_for_tick(["2m"], 0) == []

"""Scheduling dei timeframe per tick: quali TF processare e quando."""

import structlog

from app.core.enums import OrderMode
from app.db.models import Position, Signal
from app.services.candle_sync import TIMEFRAME_MS

logger = structlog.get_logger()
_LAST_TIMEFRAME_RUN_MS: dict[str, int] = {}


def should_run_timeframe(timeframe: str, now_ms: int) -> bool:
    interval_ms = TIMEFRAME_MS[timeframe]
    return now_ms % interval_ms < 60_000


def active_timeframes_for_tick(timeframes: list[str], now_ms: int) -> list[str]:
    active: list[str] = []
    for timeframe in timeframes:
        if timeframe not in TIMEFRAME_MS:
            continue
        interval_ms = TIMEFRAME_MS[timeframe]
        last_run_ms = _LAST_TIMEFRAME_RUN_MS.get(timeframe, 0)
        if should_run_timeframe(timeframe, now_ms) or now_ms - last_run_ms >= interval_ms:
            active.append(timeframe)
    return active


def mark_timeframes_run(timeframes: list[str], started_ms: int) -> None:
    for timeframe in timeframes:
        _LAST_TIMEFRAME_RUN_MS[timeframe] = started_ms


def paper_management_timeframes(db, enabled_timeframes: list[str]) -> list[str]:
    """Keep open positions manageable even after timeframe config changes.

    Includes timeframes from both paper and live open positions so that candle
    sync and trailing-stop management continue even when the source timeframe
    is no longer in the enabled_timeframes list.
    """
    managed: list[str] = []
    seen: set[str] = set()

    for timeframe in enabled_timeframes:
        if timeframe in TIMEFRAME_MS and timeframe not in seen:
            managed.append(timeframe)
            seen.add(timeframe)

    positions = (
        db.query(Position)
        .filter(
            Position.mode.in_([OrderMode.PAPER.value, OrderMode.LIVE.value]),
            Position.status == "open",
        )
        .all()
    )
    discovered: set[str] = set()
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        source_timeframe = metadata.get("source_timeframe")
        if not isinstance(source_timeframe, str) or not source_timeframe:
            source_signal_id = metadata.get("source_signal_id", 0)
            try:
                signal_id = int(source_signal_id)
            except (TypeError, ValueError):
                signal_id = None
            if signal_id is not None:
                signal = db.get(Signal, signal_id)
                if signal is not None and isinstance(signal.timeframe, str):
                    source_timeframe = signal.timeframe
        if isinstance(source_timeframe, str) and source_timeframe in TIMEFRAME_MS:
            discovered.add(source_timeframe)

    for timeframe in sorted(discovered, key=lambda item: TIMEFRAME_MS[item]):
        if timeframe not in seen:
            managed.append(timeframe)
            seen.add(timeframe)
    return managed



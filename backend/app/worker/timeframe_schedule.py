"""Scheduling dei timeframe per tick: quali TF processare e quando."""

import structlog

from app.core.enums import OrderMode
from app.core.runtime_settings import effective_position_management_settings
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


def _position_source_timeframe(db, position: Position) -> str | None:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    source_timeframe = metadata.get("source_timeframe")
    if isinstance(source_timeframe, str) and source_timeframe:
        return source_timeframe

    source_signal_id = metadata.get("source_signal_id", 0)
    try:
        signal_id = int(source_signal_id)
    except (TypeError, ValueError):
        return None
    signal = db.get(Signal, signal_id)
    if signal is not None and isinstance(signal.timeframe, str):
        return signal.timeframe
    return None


def open_position_trailing_market_ids_by_timeframe(db) -> dict[str, set[int]]:
    """Markets that need each timeframe synced for open-position trailing stops."""
    positions = (
        db.query(Position)
        .filter(
            Position.mode.in_([OrderMode.PAPER.value, OrderMode.LIVE.value]),
            Position.status == "open",
        )
        .all()
    )
    check_intervals = (
        effective_position_management_settings(db)
        .get("trailingStop", {})
        .get("checkIntervalByTimeframe", {})
    )
    result: dict[str, set[int]] = {}
    for position in positions:
        source_timeframe = _position_source_timeframe(db, position)
        if not isinstance(source_timeframe, str) or source_timeframe not in TIMEFRAME_MS:
            continue
        reference = check_intervals.get(source_timeframe)
        if (
            not isinstance(reference, str)
            or reference not in TIMEFRAME_MS
            or TIMEFRAME_MS[reference] > TIMEFRAME_MS[source_timeframe]
        ):
            reference = source_timeframe
        market_id = getattr(position, "market_id", None)
        if market_id is None:
            continue
        result.setdefault(reference, set()).add(int(market_id))
    return result


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
    check_intervals = (
        effective_position_management_settings(db)
        .get("trailingStop", {})
        .get("checkIntervalByTimeframe", {})
    )
    discovered: set[str] = set()
    for position in positions:
        source_timeframe = _position_source_timeframe(db, position)
        if isinstance(source_timeframe, str) and source_timeframe in TIMEFRAME_MS:
            # Sia il timeframe sorgente (necessario per ATR/EMA) sia quello di
            # riferimento del trailing (cadenza di controllo + prezzo intraday)
            # devono essere sincronizzati e gestiti.
            discovered.add(source_timeframe)
            reference = check_intervals.get(source_timeframe)
            if (
                isinstance(reference, str)
                and reference in TIMEFRAME_MS
                and TIMEFRAME_MS[reference] <= TIMEFRAME_MS[source_timeframe]
            ):
                discovered.add(reference)

    for timeframe in sorted(discovered, key=lambda item: TIMEFRAME_MS[item]):
        if timeframe not in seen:
            managed.append(timeframe)
            seen.add(timeframe)
    return managed


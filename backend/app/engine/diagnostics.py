from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AppSetting, DecisionLog, Position, Signal
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats

ZERO_SIGNAL_CYCLE_LOOKBACK = 1000
EQUITY_SLOPE_DECLINING_THRESHOLD = -0.1


def consecutive_zero_signal_cycles(db: Session, *, limit: int = ZERO_SIGNAL_CYCLE_LOOKBACK) -> int:
    """Conta gli ultimi cicli worker eleggibili senza segnali generati.

    I tick senza timeframe attivi sono esclusi perché non rappresentano
    opportunità in cui il worker avrebbe potuto generare un segnale.
    """
    recent = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(["worker_cycle", "worker_cycle_failed"]))
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    streak = 0
    for entry in recent:
        if entry.event_type == "worker_cycle_failed":
            streak += 1
            continue
        if not isinstance(entry.data, dict):
            break
        active_timeframes = entry.data.get("active_timeframes")
        if isinstance(active_timeframes, list) and not active_timeframes:
            continue
        try:
            generated = int(entry.data.get("generated", 0))
        except (TypeError, ValueError):
            break
        if generated == 0:
            streak += 1
        else:
            break
    return streak


def exit_reason_distribution(positions: list[Position]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        reason = metadata.get("exit_reason") or "unknown"
        counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts


def _percentile(sorted_values: list[float], rank: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = max(0, min(len(sorted_values) - 1, math.ceil(rank * len(sorted_values)) - 1))
    return sorted_values[idx]


def planned_stop_pct_summary(
    positions: list[Position],
    signals_by_id: dict[int, Signal],
) -> dict[str, Any]:
    pcts: list[float] = []
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        raw_signal_id = metadata.get("source_signal_id")
        if raw_signal_id is None:
            continue
        try:
            signal_id = int(raw_signal_id)
        except (TypeError, ValueError):
            continue
        signal = signals_by_id.get(signal_id)
        if signal is None or signal.stop_loss is None or signal.entry_price is None:
            continue
        entry = float(signal.entry_price)
        stop = float(signal.stop_loss)
        if entry <= 0:
            continue
        pcts.append(abs(entry - stop) / entry)
    pcts.sort()
    return {
        "count": len(pcts),
        "p25": _percentile(pcts, 0.25),
        "p50": _percentile(pcts, 0.50),
        "p75": _percentile(pcts, 0.75),
        "p90": _percentile(pcts, 0.90),
    }


def equity_slope(positions: list[Position]) -> float:
    items: list[tuple[Any, float]] = []
    for position in positions:
        if position.closed_at is None or position.realized_pnl is None:
            continue
        items.append((position.closed_at, float(position.realized_pnl)))
    if len(items) < 10:
        return 0.0
    items.sort(key=lambda row: row[0])
    cum = list(itertools.accumulate(value for _, value in items))
    n = len(cum)
    xs = list(range(n))
    sx = sum(xs)
    sy = sum(cum)
    num = n * sum(x * y for x, y in zip(xs, cum, strict=False)) - sx * sy
    den = n * sum(x * x for x in xs) - sx * sx
    return num / den if den else 0.0


def equity_slope_normalized(positions: list[Position]) -> float:
    """Slope normalizzato per la magnitudine media dei PnL.

    Why: la slope grezza è in USD/trade-index e cambia drasticamente con l'equity
    di riferimento o la size dei trade. Dividendo per il |PnL| mediano otteniamo
    una grandezza unit-less ("frazioni di trade medio per trade-index") che è
    stabile rispetto alla scala. Una soglia -0.1 significa "ogni nuovo trade peggiora
    la equity cumulata del 10% di un PnL tipico" — interpretabile e portabile.
    """
    pnls = [
        abs(float(position.realized_pnl))
        for position in positions
        if position.realized_pnl is not None
    ]
    if not pnls:
        return 0.0
    sorted_pnls = sorted(pnls)
    median = sorted_pnls[len(sorted_pnls) // 2]
    if median <= 0:
        return 0.0
    raw_slope = equity_slope(positions)
    return raw_slope / median


def _median_abs_pnl(positions: list[Position]) -> float:
    values = sorted(
        abs(float(position.realized_pnl))
        for position in positions
        if position.realized_pnl is not None
    )
    if not values:
        return 0.0
    return values[len(values) // 2]


def build_equity_slope_diagnostics(
    db: Session,
    *,
    lookback: int = 500,
) -> dict[str, Any]:
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=lookback)

    threshold = EQUITY_SLOPE_DECLINING_THRESHOLD
    window_sizes = [50, 100, 500]
    windows = []
    for size in window_sizes:
        sample = positions[: min(size, len(positions))]
        raw = equity_slope(sample)
        median_abs = _median_abs_pnl(sample)
        normalized = raw / median_abs if median_abs > 0 else 0.0
        windows.append(
            {
                "window": size,
                "sample_count": len(sample),
                "raw_slope": raw,
                "normalized_slope": normalized,
                "median_abs_pnl": median_abs,
            }
        )

    full_normalized = equity_slope_normalized(positions)
    recent_positions = positions[: min(50, len(positions))]
    recent_normalized = equity_slope_normalized(recent_positions)
    if recent_normalized < threshold or full_normalized < threshold:
        status = "declining"
    elif recent_normalized >= 0 and full_normalized >= 0:
        status = "improving"
    else:
        status = "neutral"

    now = datetime.now(UTC)
    since = now - timedelta(hours=24)
    time_stop_rows = (
        db.query(DecisionLog)
        .filter(
            DecisionLog.event_type == "paper_position_time_stop_closed",
            DecisionLog.timestamp >= since,
        )
        .order_by(DecisionLog.timestamp.desc())
        .limit(1000)
        .all()
    )
    time_stop_pnl = 0.0
    for row in time_stop_rows:
        data = row.data if isinstance(row.data, dict) else {}
        try:
            time_stop_pnl += float(data.get("realized_pnl", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue

    return {
        "lookback": lookback,
        "sample_count": len(positions),
        "status": status,
        "threshold": threshold,
        "raw_slope": equity_slope(positions),
        "normalized_slope": full_normalized,
        "recent_normalized_slope": recent_normalized,
        "median_abs_pnl": _median_abs_pnl(positions),
        "windows": windows,
        "consecutive_zero_signal_cycles": consecutive_zero_signal_cycles(db),
        "recent_time_stop_impact": {
            "hours": 24,
            "closed_count": len(time_stop_rows),
            "realized_pnl": time_stop_pnl,
        },
    }


def build_diagnostics_report(
    db: Session,
    *,
    lookback: int = 200,
) -> dict[str, Any]:
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=lookback)
    overall = analyzer.summarize_positions(positions)

    signal_ids = {
        int(position.metadata_json["source_signal_id"])
        for position in positions
        if isinstance(position.metadata_json, dict)
        and position.metadata_json.get("source_signal_id") is not None
    }
    signals_by_id: dict[int, Signal] = (
        {signal.id: signal for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )

    risk_setting = db.get(AppSetting, "risk")
    risk_value = risk_setting.value if risk_setting and isinstance(risk_setting.value, dict) else {}

    by_timeframe: dict[str, list[Position]] = {}
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        timeframe = metadata.get("source_timeframe")
        if isinstance(timeframe, str):
            by_timeframe.setdefault(timeframe, []).append(position)

    risk_keys = ("min_signal_score", "min_risk_reward", "max_risk_per_trade_pct")
    return {
        "lookback": lookback,
        "trade_count": overall.trades,
        "overall": serialize_stats(overall),
        "exit_reason_distribution": exit_reason_distribution(positions),
        "planned_stop_pct": planned_stop_pct_summary(positions, signals_by_id),
        "equity_slope": equity_slope(positions),
        "equity_slope_diagnostics": build_equity_slope_diagnostics(
            db, lookback=min(max(lookback, 50), 500)
        ),
        "by_timeframe": {
            timeframe: serialize_stats(analyzer.summarize_positions(positions_for_tf))
            for timeframe, positions_for_tf in sorted(by_timeframe.items())
        },
        "by_strategy": {
            name: serialize_stats(stats)
            for name, stats in analyzer.strategy_stats(db, limit=lookback).items()
        },
        "current_risk": {key: risk_value.get(key) for key in risk_keys},
    }

from __future__ import annotations

import itertools
import math
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AppSetting, DecisionLog, Position, Signal
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats


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
    num = n * sum(x * y for x, y in zip(xs, cum)) - sx * sy
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


def build_diagnostics_report(
    db: Session,
    *,
    lookback: int = 200,
    history_limit: int = 50,
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

    history_rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.like("auto_calibration%"))
        .order_by(DecisionLog.timestamp.desc())
        .limit(history_limit)
        .all()
    )

    baseline_setting = db.get(AppSetting, "auto_calibration_baseline")
    risk_setting = db.get(AppSetting, "risk")
    state_setting = db.get(AppSetting, "auto_calibration_state")
    baseline_value = baseline_setting.value if baseline_setting and isinstance(baseline_setting.value, dict) else {}
    risk_value = risk_setting.value if risk_setting and isinstance(risk_setting.value, dict) else {}
    state_value = state_setting.value if state_setting and isinstance(state_setting.value, dict) else {}
    baseline_risk = baseline_value.get("risk", {}) if isinstance(baseline_value.get("risk"), dict) else {}

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
        "by_timeframe": {
            timeframe: serialize_stats(analyzer.summarize_positions(positions_for_tf))
            for timeframe, positions_for_tf in sorted(by_timeframe.items())
        },
        "by_strategy": {
            name: serialize_stats(stats)
            for name, stats in analyzer.strategy_stats(db, limit=lookback).items()
        },
        "baseline_vs_current": {
            "risk": {
                key: {"baseline": baseline_risk.get(key), "current": risk_value.get(key)}
                for key in risk_keys
            }
        },
        "calibration_state": state_value,
        "calibration_history": [
            {
                "id": row.id,
                "timestamp": row.timestamp,
                "event_type": row.event_type,
                "status": (row.data or {}).get("status"),
                "reason": (row.data or {}).get("reason"),
                "changes_count": len((row.data or {}).get("changes", []) or []),
            }
            for row in history_rows
        ],
    }

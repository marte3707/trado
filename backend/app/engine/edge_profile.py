from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session, joinedload

from app.core.enums import Direction
from app.db.models import Position, Signal
from app.engine.performance import PerformanceStats, SignalPerformanceAnalyzer, serialize_stats

DEFAULT_MAX_COMBINED_SCORE_ADJUSTMENT = 15


@dataclass(frozen=True)
class PositionSignal:
    position: Position
    signal: Signal | None


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type,call-overload]
    except (TypeError, ValueError):
        return None


def _safe_int(value: object, default: int = 0) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def direction_family(direction: str | Direction | None) -> str | None:
    value = direction.value if isinstance(direction, Direction) else str(direction or "")
    if value in {Direction.LONG.value, Direction.STRONG_LONG.value}:
        return "LONG"
    if value in {Direction.SHORT.value, Direction.STRONG_SHORT.value}:
        return "SHORT"
    return None


def score_bucket(score: int | float | None) -> str:
    if score is None:
        return "unknown"
    value = int(score)
    if value < 60:
        return "50-59"
    if value < 70:
        return "60-69"
    if value < 80:
        return "70-79"
    return "80+"


def edge_adjustment_for_stats(
    stats: PerformanceStats,
    *,
    weak_trades: int,
    weak_pf: float,
    weak_wr: float | None,
    weak_adjustment: int,
    severe_trades: int,
    severe_pf: float,
    severe_wr: float | None,
    severe_adjustment: int,
    require_negative_pnl: bool = False,
) -> int:
    if stats.trades >= severe_trades and (
        (stats.profit_factor is not None and stats.profit_factor < severe_pf)
        or (severe_wr is not None and stats.win_rate < severe_wr)
    ):
        if not require_negative_pnl or stats.realized_pnl < 0:
            return severe_adjustment
    if stats.trades >= weak_trades and (
        (stats.profit_factor is not None and stats.profit_factor < weak_pf)
        or (weak_wr is not None and stats.win_rate < weak_wr)
    ):
        if not require_negative_pnl or stats.realized_pnl < 0:
            return weak_adjustment
    return 0


def linked_position_signals(db: Session, positions: list[Position]) -> list[PositionSignal]:
    signal_ids: set[int] = set()
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        signal_id = _optional_int(metadata.get("source_signal_id"))
        if signal_id is not None:
            signal_ids.add(signal_id)
    signals = (
        {signal.id: signal for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )
    rows: list[PositionSignal] = []
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        signal_id = _optional_int(metadata.get("source_signal_id"))
        signal = signals.get(signal_id) if signal_id is not None else None
        rows.append(PositionSignal(position=position, signal=signal))
    return rows


def _summarize_groups(
    analyzer: SignalPerformanceAnalyzer,
    rows: list[PositionSignal],
    key_fn: Callable[[PositionSignal], str | None],
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Position]] = {}
    for row in rows:
        key = key_fn(row)
        if key:
            groups.setdefault(key, []).append(row.position)
    return {
        key: serialize_stats(analyzer.summarize_positions(group_positions))
        for key, group_positions in sorted(groups.items())
    }


def compute_edge_adjustments(
    db: Session,
    *,
    lookback: int = 500,
    analyzer: SignalPerformanceAnalyzer | None = None,
) -> dict[str, dict[str, Any]]:
    analyzer = analyzer or SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=lookback)
    rows = [row for row in linked_position_signals(db, positions) if row.signal is not None]

    by_direction_tf = _summarize_groups(
        analyzer,
        rows,
        lambda row: (
            f"{direction_family(row.signal.direction)}:{row.signal.timeframe}"
            if row.signal and direction_family(row.signal.direction)
            else None
        ),
    )
    by_score_bucket = _summarize_groups(
        analyzer,
        rows,
        lambda row: (
            f"{direction_family(row.signal.direction)}:{score_bucket(row.signal.score)}"
            if row.signal and direction_family(row.signal.direction)
            else None
        ),
    )
    by_market = _summarize_groups(
        analyzer,
        rows,
        lambda row: str(row.position.market_id),
    )
    by_timeframe = _summarize_groups(
        analyzer,
        rows,
        lambda row: row.signal.timeframe if row.signal else None,
    )

    directional = {
        key: {
            "adjustment": edge_adjustment_for_stats(
                _stats_from_payload(stats),
                weak_trades=20,
                weak_pf=0.8,
                weak_wr=0.35,
                weak_adjustment=8,
                severe_trades=30,
                severe_pf=0.6,
                severe_wr=0.25,
                severe_adjustment=12,
            ),
            "stats": stats,
        }
        for key, stats in by_direction_tf.items()
    }
    score_buckets = {
        key: {
            "adjustment": edge_adjustment_for_stats(
                _stats_from_payload(stats),
                weak_trades=25,
                weak_pf=0.8,
                weak_wr=None,
                weak_adjustment=5,
                severe_trades=25,
                severe_pf=0.6,
                severe_wr=None,
                severe_adjustment=8,
            ),
            "stats": stats,
        }
        for key, stats in by_score_bucket.items()
    }
    markets = {
        key: {
            "adjustment": edge_adjustment_for_stats(
                _stats_from_payload(stats),
                weak_trades=5,
                weak_pf=0.5,
                weak_wr=None,
                weak_adjustment=6,
                severe_trades=8,
                severe_pf=0.35,
                severe_wr=None,
                severe_adjustment=10,
                require_negative_pnl=True,
            ),
            "stats": stats,
        }
        for key, stats in by_market.items()
    }
    timeframes = {
        key: {
            "adjustment": edge_adjustment_for_stats(
                _stats_from_payload(stats),
                weak_trades=30,
                weak_pf=0.7,
                weak_wr=None,
                weak_adjustment=5,
                severe_trades=30,
                severe_pf=0.7,
                severe_wr=None,
                severe_adjustment=5,
            ),
            "stats": stats,
        }
        for key, stats in by_timeframe.items()
    }

    return {
        "directional_score_adjustments": {
            key: value
            for key, value in directional.items()
            if _safe_int(value.get("adjustment")) > 0
        },
        "score_bucket_adjustments": {
            key: value
            for key, value in score_buckets.items()
            if _safe_int(value.get("adjustment")) > 0
        },
        "market_score_adjustments": {
            key: value
            for key, value in markets.items()
            if _safe_int(value.get("adjustment")) > 0
        },
        "timeframe_score_adjustments": {
            key: value
            for key, value in timeframes.items()
            if _safe_int(value.get("adjustment")) > 0
        },
    }


def _stats_from_payload(payload: dict[str, Any]) -> PerformanceStats:
    return PerformanceStats(
        trades=int(payload.get("trades", 0)),
        wins=int(payload.get("wins", 0)),
        losses=int(payload.get("losses", 0)),
        win_rate=float(payload.get("win_rate", 0.0)),
        realized_pnl=float(payload.get("realized_pnl", 0.0)),
        gross_profit=float(payload.get("gross_profit", 0.0)),
        gross_loss=float(payload.get("gross_loss", 0.0)),
        profit_factor=(
            float(payload["profit_factor"]) if payload.get("profit_factor") is not None else None
        ),
    )


def signal_threshold_adjustment(
    risk: dict[str, Any],
    *,
    market_id: int,
    timeframe: str,
    direction: Direction,
    score: int,
) -> dict[str, Any]:
    family = direction_family(direction)
    if family is None:
        return {"total": 0, "components": {}, "max_adjustment": 0}
    max_adjustment = int(risk.get("max_combined_score_adjustment", DEFAULT_MAX_COMBINED_SCORE_ADJUSTMENT))
    components = {
        "directional": _adjustment_value(
            risk.get("directional_score_adjustments"),
            f"{family}:{timeframe}",
        ),
        "score_bucket": _adjustment_value(
            risk.get("score_bucket_adjustments"),
            f"{family}:{score_bucket(score)}",
        ),
        "market": _adjustment_value(risk.get("market_score_adjustments"), str(market_id)),
        "timeframe": _adjustment_value(risk.get("timeframe_score_adjustments"), timeframe),
    }
    raw_total = sum(components.values())
    return {
        "total": min(max_adjustment, raw_total),
        "raw_total": raw_total,
        "components": components,
        "max_adjustment": max_adjustment,
        "direction": family,
        "score_bucket": score_bucket(score),
    }


def _adjustment_value(raw: object, key: str) -> int:
    if not isinstance(raw, dict):
        return 0
    value = raw.get(key)
    if isinstance(value, dict):
        return max(0, _safe_int(value.get("adjustment")))
    return max(0, _safe_int(value))


def build_outcome_profile(db: Session, *, lookback: int = 500) -> dict[str, Any]:
    analyzer = SignalPerformanceAnalyzer()
    positions = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.status == "closed", Position.realized_pnl.is_not(None))
        .order_by(Position.closed_at.desc())
        .limit(lookback)
        .all()
    )
    rows = linked_position_signals(db, positions)

    def market_key(row: PositionSignal) -> str | None:
        market = row.position.market
        if market is None:
            return str(row.position.market_id)
        return f"{market.id}:{market.symbol}"

    return {
        "filters": {"lookback": lookback},
        "overall": serialize_stats(analyzer.summarize_positions(positions)),
        "by_mode": _summarize_groups(analyzer, rows, lambda row: row.position.mode),
        "by_direction": _summarize_groups(
            analyzer,
            rows,
            lambda row: direction_family(row.signal.direction) if row.signal else None,
        ),
        "by_timeframe": _summarize_groups(
            analyzer,
            rows,
            lambda row: row.signal.timeframe if row.signal else None,
        ),
        "by_score_bucket": _summarize_groups(
            analyzer,
            rows,
            lambda row: (
                f"{direction_family(row.signal.direction)}:{score_bucket(row.signal.score)}"
                if row.signal and direction_family(row.signal.direction)
                else None
            ),
        ),
        "by_market": _summarize_groups(analyzer, rows, market_key),
        "by_exit_reason": _summarize_groups(
            analyzer,
            rows,
            lambda row: (
                row.position.metadata_json.get("exit_reason", "unknown")
                if isinstance(row.position.metadata_json, dict)
                else "unknown"
            ),
        ),
        "by_tp_state": _summarize_groups(
            analyzer,
            rows,
            lambda row: _tp_state(row.position),
        ),
    }


def _tp_state(position: Position) -> str:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    tp1 = bool(metadata.get("tp1_hit"))
    tp2 = bool(metadata.get("tp2_hit"))
    if tp2:
        return "tp2_hit"
    if tp1:
        return "tp1_hit"
    return "no_tp1"

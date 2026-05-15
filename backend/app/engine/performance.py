from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from app.core.enums import OrderMode
from app.db.models import Position, Signal


@dataclass(frozen=True)
class PerformanceStats:
    trades: int
    wins: int
    losses: int
    win_rate: float
    realized_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None


class SignalPerformanceAnalyzer:
    def closed_paper_positions(
        self,
        db: Session,
        timeframe: str | None = None,
        market_id: int | None = None,
        limit: int = 500,
    ) -> list[Position]:
        query = (
            db.query(Position)
            .options(joinedload(Position.market))
            .filter(
                Position.mode == OrderMode.PAPER.value,
                Position.status == "closed",
                Position.realized_pnl.is_not(None),
            )
        )
        if market_id is not None:
            query = query.filter(Position.market_id == market_id)
        query = query.order_by(Position.closed_at.desc()).limit(limit)
        rows = query.all()
        if timeframe is None:
            return rows
        return [
            row
            for row in rows
            if isinstance(row.metadata_json, dict)
            and row.metadata_json.get("source_timeframe") == timeframe
        ]

    def summarize_positions(self, positions: list[Position]) -> PerformanceStats:
        pnl_values = [Decimal(position.realized_pnl or 0) for position in positions]
        wins = sum(1 for pnl in pnl_values if pnl > 0)
        losses = sum(1 for pnl in pnl_values if pnl <= 0)
        gross_profit = sum((pnl for pnl in pnl_values if pnl > 0), Decimal("0"))
        gross_loss = abs(sum((pnl for pnl in pnl_values if pnl <= 0), Decimal("0")))
        profit_factor = float(gross_profit / gross_loss) if gross_loss else None
        realized_pnl = sum(pnl_values, Decimal("0"))
        trades = len(pnl_values)
        return PerformanceStats(
            trades=trades,
            wins=wins,
            losses=losses,
            win_rate=(wins / trades) if trades else 0.0,
            realized_pnl=float(realized_pnl),
            gross_profit=float(gross_profit),
            gross_loss=float(gross_loss),
            profit_factor=profit_factor,
        )

    def strategy_stats(
        self,
        db: Session,
        timeframe: str | None = None,
        market_id: int | None = None,
        limit: int = 500,
    ) -> dict[str, PerformanceStats]:
        positions = self.closed_paper_positions(db, timeframe, market_id, limit)
        signal_ids = {
            int(position.metadata_json["source_signal_id"])
            for position in positions
            if isinstance(position.metadata_json, dict)
            and position.metadata_json.get("source_signal_id") is not None
        }
        signals = {
            signal.id: signal
            for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()
        } if signal_ids else {}
        by_strategy: dict[str, list[Position]] = {}
        for position in positions:
            metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
            signal_id_raw = metadata.get("source_signal_id")
            if signal_id_raw is None:
                continue
            try:
                signal_id = int(signal_id_raw)
                signal = signals.get(signal_id)
            except (TypeError, ValueError):
                signal = None
            if signal is None:
                continue
            for strategy_name, breakdown in (signal.strategy_breakdown or {}).items():
                if strategy_name.startswith("_") or not isinstance(breakdown, dict):
                    continue
                if breakdown.get("direction") == "NEUTRAL" or breakdown.get("contribution", 0) <= 0:
                    continue
                by_strategy.setdefault(strategy_name, []).append(position)
        return {
            strategy_name: self.summarize_positions(strategy_positions)
            for strategy_name, strategy_positions in by_strategy.items()
        }

    def adjusted_weights(
        self,
        db: Session,
        weights: dict[str, int],
        timeframe: str,
        market_id: int,
        min_trades: int = 8,
    ) -> tuple[dict[str, int], dict[str, dict]]:
        scoped_stats = self.strategy_stats(db, timeframe=timeframe, market_id=market_id)
        timeframe_stats = self.strategy_stats(db, timeframe=timeframe)
        global_stats = self.strategy_stats(db)
        adjusted = dict(weights)
        notes: dict[str, dict] = {}
        for strategy_name, base_weight in weights.items():
            scope = "market_timeframe"
            strategy_stats = scoped_stats.get(strategy_name)
            if strategy_stats is None or strategy_stats.trades < min_trades:
                scope = "timeframe"
                strategy_stats = timeframe_stats.get(strategy_name)
            if strategy_stats is None or strategy_stats.trades < min_trades:
                scope = "global"
                strategy_stats = global_stats.get(strategy_name)
            if strategy_stats is None or strategy_stats.trades < min_trades:
                continue
            factor = 1.0
            if strategy_stats.win_rate < 0.35:
                factor = 0.55
            elif strategy_stats.win_rate < 0.45:
                factor = 0.75
            elif strategy_stats.win_rate >= 0.58 and (
                strategy_stats.profit_factor is None or strategy_stats.profit_factor >= 1.2
            ):
                factor = 1.15
            adjusted[strategy_name] = max(1, round(base_weight * factor))
            notes[strategy_name] = {
                "base_weight": base_weight,
                "adjusted_weight": adjusted[strategy_name],
                "factor": factor,
                "scope": scope,
                "trades": strategy_stats.trades,
                "win_rate": strategy_stats.win_rate,
                "realized_pnl": strategy_stats.realized_pnl,
                "profit_factor": strategy_stats.profit_factor,
            }
        return adjusted, notes


def serialize_stats(stats: PerformanceStats) -> dict:
    return {
        "trades": stats.trades,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": stats.win_rate,
        "realized_pnl": stats.realized_pnl,
        "gross_profit": stats.gross_profit,
        "gross_loss": stats.gross_loss,
        "profit_factor": stats.profit_factor,
    }

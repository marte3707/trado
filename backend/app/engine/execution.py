from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction, OrderMode, SignalStatus
from app.core.runtime_settings import effective_paper_equity
from app.db.models import AppSetting, Candle, DecisionLog, Market, Order, Position, Signal
from app.engine.order_validation import OrderValidator
from app.engine.risk import calculate_position_size


TIMEFRAME_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

TAKE_PROFIT_ALLOCATIONS = (
    ("tp1", "take_profit_1", Decimal("0.50")),
    ("tp2", "take_profit_2", Decimal("0.30")),
    ("tp3", "take_profit_3", Decimal("0.20")),
)


class OrderExecutionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.validator = OrderValidator()

    def execute_paper(self, db: Session, signal: Signal) -> Position:
        side = "long" if signal.direction in {Direction.LONG, Direction.STRONG_LONG} else "short"
        risk_settings = db.get(AppSetting, "risk")
        max_risk_per_trade_pct = self.settings.max_risk_per_trade_pct
        if risk_settings:
            max_risk_per_trade_pct = float(
                risk_settings.value.get("max_risk_per_trade_pct", max_risk_per_trade_pct)
            )
        size = calculate_position_size(
            equity=effective_paper_equity(db, self.settings),
            risk_pct=max_risk_per_trade_pct,
            entry_price=float(signal.entry_price),
            stop_loss=float(signal.stop_loss or signal.entry_price),
            leverage=1.0,
        )
        position = Position(
            market_id=signal.market_id,
            mode=OrderMode.PAPER.value,
            side=side,
            entry_price=signal.entry_price,
            size=Decimal(str(size)),
            leverage=Decimal("1"),
            margin_mode=None,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_3 or signal.take_profit_2 or signal.take_profit_1,
            status="open",
            metadata_json={
                "source_signal_id": signal.id,
                "source_timeframe": signal.timeframe,
                "initial_size": str(size),
                "remaining_size": str(size),
                "take_profit_plan": self._build_take_profit_plan(signal),
            },
        )
        db.add(position)
        db.flush()
        order = Order(
            position_id=position.id,
            signal_id=signal.id,
            mode=OrderMode.PAPER.value,
            order_type="market",
            side=side,
            price=signal.entry_price,
            size=Decimal(str(size)),
            status="filled",
            reduce_only=False,
            raw_response={"paper": True},
        )
        signal.status = SignalStatus.EXECUTED.value
        db.add(order)
        db.add(
            DecisionLog(
                event_type="paper_order_executed",
                market_id=signal.market_id,
                signal_id=signal.id,
                position_id=position.id,
                message="Paper position opened from signal",
                data={"size": size, "side": side},
            )
        )
        db.commit()
        db.refresh(position)
        return position

    def execute_live(self, db: Session, signal: Signal) -> None:
        validation = self.validator.validate_live_execution(db, signal, self.settings)
        if not validation.ok:
            db.add(
                DecisionLog(
                    event_type="live_order_rejected",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Live order rejected by safety validation",
                    data={"reasons": validation.reasons},
                )
            )
            db.commit()
            raise PermissionError("; ".join(validation.reasons))
        raise NotImplementedError(
            "Live execution is intentionally not implemented in MVP. Use Hyperliquid SDK signing here."
        )

    def close_position(
        self,
        db: Session,
        position: Position,
        exit_price: Decimal,
        reason: str,
        commit: bool = True,
    ) -> None:
        self._exit_position(
            db,
            position,
            exit_price=exit_price,
            exit_size=position.size,
            reason=reason,
            close_position=True,
            commit=commit,
        )

    def _exit_position(
        self,
        db: Session,
        position: Position,
        exit_price: Decimal,
        exit_size: Decimal,
        reason: str,
        close_position: bool,
        commit: bool = True,
    ) -> None:
        if exit_size <= 0:
            return
        if exit_size > position.size:
            exit_size = position.size
        side = position.side.lower()
        pnl_multiplier = Decimal("1") if side == "long" else Decimal("-1")
        realized_pnl = (exit_price - position.entry_price) * exit_size * pnl_multiplier
        cumulative_realized = Decimal(position.realized_pnl or 0) + realized_pnl
        remaining_size = position.size - exit_size
        if close_position or remaining_size <= Decimal("0.0000000001"):
            position.status = "closed"
            position.closed_at = datetime.now(timezone.utc)
            position.size = Decimal("0")
            position.unrealized_pnl = Decimal("0")
            close_position = True
        else:
            position.size = remaining_size
        position.realized_pnl = cumulative_realized
        metadata = dict(position.metadata_json or {})
        initial_size = metadata.get("initial_size")
        if initial_size is None:
            metadata["initial_size"] = str(position.size + exit_size)
        metadata.update(
            {
                "remaining_size": str(position.size),
                "realized_pnl": str(cumulative_realized),
                "last_exit_price": str(exit_price),
                "last_exit_reason": reason,
            }
        )
        if close_position:
            metadata.update({"exit_price": str(exit_price), "exit_reason": reason})
        self._mark_take_profit_filled(metadata, reason)
        position.metadata_json = metadata
        source_signal_id = metadata.get("source_signal_id")
        signal_id: int | None = None
        if source_signal_id is not None:
            try:
                signal_id = int(source_signal_id)
            except (TypeError, ValueError):
                signal_id = None
            if close_position and signal_id is not None:
                signal = db.get(Signal, signal_id)
                if signal is not None:
                    signal.status = SignalStatus.CLOSED.value
                    db.add(signal)
        db.add(position)
        db.add(
            Order(
                position_id=position.id,
                signal_id=signal_id,
                mode=position.mode,
                order_type=reason,
                side=position.side,
                price=exit_price,
                size=exit_size,
                status="filled",
                reduce_only=True,
                raw_response={"paper": position.mode == OrderMode.PAPER.value, "trigger": reason},
            )
        )
        db.add(
            DecisionLog(
                event_type=self._exit_event_type(reason, close_position),
                market_id=position.market_id,
                position_id=position.id,
                signal_id=signal_id,
                message=(
                    f"Position closed by {reason}"
                    if close_position
                    else f"Position partially closed by {reason}"
                ),
                data={
                    "mode": position.mode,
                    "side": position.side,
                    "size": str(exit_size),
                    "remaining_size": str(position.size),
                    "exit_price": str(exit_price),
                    "realized_pnl": str(realized_pnl),
                    "cumulative_realized_pnl": str(cumulative_realized),
                },
            )
        )
        if commit:
            db.commit()

    def _exit_event_type(self, reason: str, close_position: bool) -> str:
        if reason == "stop_loss":
            return "paper_position_auto_closed"
        if reason in {"take_profit", "tp1", "tp2", "tp3"}:
            return "paper_position_auto_closed" if close_position else "paper_position_partial_closed"
        return "position_closed" if close_position else "position_partially_closed"

    def close_paper_positions_for_candle(self, db: Session, market: Market, candle: Candle) -> int:
        positions = (
            db.query(Position)
            .filter(
                Position.market_id == market.id,
                Position.mode == OrderMode.PAPER.value,
                Position.status == "open",
            )
            .all()
        )
        exits = 0
        closed = 0
        for position in positions:
            self._ensure_take_profit_plan(db, position)
            if not self._candle_can_affect_position(position, candle, candle.timeframe):
                continue
            exit_count, closed_count = self._apply_paper_triggers_for_candle(db, position, candle)
            exits += exit_count
            closed += closed_count
        if exits:
            db.commit()
        return closed

    def close_paper_positions_for_timeframe(self, db: Session, market: Market, timeframe: str) -> int:
        interval_ms = TIMEFRAME_MS[timeframe]
        positions = (
            db.query(Position)
            .filter(
                Position.market_id == market.id,
                Position.mode == OrderMode.PAPER.value,
                Position.status == "open",
            )
            .all()
        )
        exits = 0
        closed = 0
        for position in positions:
            if not self._position_matches_timeframe(db, position, timeframe):
                continue
            self._ensure_take_profit_plan(db, position)
            earliest_open = position.opened_at - timedelta(milliseconds=interval_ms)
            candles = (
                db.query(Candle)
                .filter(
                    Candle.market_id == market.id,
                    Candle.timeframe == timeframe,
                    Candle.open_time >= earliest_open,
                )
                .order_by(Candle.open_time.asc())
                .all()
            )
            for candle in candles:
                if position.status != "open":
                    break
                if not self._candle_can_affect_position(position, candle, timeframe):
                    continue
                exit_count, closed_count = self._apply_paper_triggers_for_candle(db, position, candle)
                exits += exit_count
                closed += closed_count
        if exits:
            db.commit()
        return closed

    def _position_matches_timeframe(self, db: Session, position: Position, timeframe: str) -> bool:
        metadata = position.metadata_json or {}
        source_timeframe = metadata.get("source_timeframe")
        if isinstance(source_timeframe, str):
            return source_timeframe == timeframe
        source_signal_id = metadata.get("source_signal_id")
        if source_signal_id is None:
            return True
        try:
            signal_id = int(source_signal_id)
        except (TypeError, ValueError):
            return True
        signal = db.get(Signal, signal_id)
        return signal is None or signal.timeframe == timeframe

    def _candle_can_affect_position(self, position: Position, candle: Candle, timeframe: str) -> bool:
        interval_ms = TIMEFRAME_MS[timeframe]
        candle_close_time = candle.open_time + timedelta(milliseconds=interval_ms)
        return candle_close_time > position.opened_at

    def _paper_trigger_for_candle(self, position: Position, candle: Candle) -> tuple[str, Decimal] | None:
        triggers = self._paper_triggers_for_candle(position, candle)
        if not triggers:
            return None
        reason, exit_price, _allocation = triggers[0]
        return reason, exit_price

    def _paper_triggers_for_candle(self, position: Position, candle: Candle) -> list[tuple[str, Decimal, Decimal]]:
        side = position.side.lower()
        stop_loss = position.stop_loss
        if side == "long":
            stop_hit = stop_loss is not None and candle.low <= stop_loss
        else:
            stop_hit = stop_loss is not None and candle.high >= stop_loss
        if stop_hit and stop_loss is not None:
            return [("stop_loss", stop_loss, Decimal("1"))]

        triggers: list[tuple[str, Decimal, Decimal]] = []
        for item in self._take_profit_plan(position):
            if item.get("filled"):
                continue
            try:
                price = Decimal(str(item["price"]))
                allocation = Decimal(str(item["allocation"]))
            except (KeyError, TypeError, ValueError):
                continue
            take_hit = candle.high >= price if side == "long" else candle.low <= price
            if take_hit:
                triggers.append((str(item.get("level", "take_profit")), price, allocation))
        return triggers

    def _apply_paper_triggers_for_candle(
        self,
        db: Session,
        position: Position,
        candle: Candle,
    ) -> tuple[int, int]:
        triggers = self._paper_triggers_for_candle(position, candle)
        if not triggers:
            return 0, 0
        exit_count = 0
        closed_count = 0
        for reason, exit_price, allocation in triggers:
            if position.status != "open":
                break
            if reason == "stop_loss":
                self.close_position(db, position, exit_price=exit_price, reason=reason, commit=False)
                exit_count += 1
                closed_count += 1
                break
            initial_size = self._initial_size(position)
            exit_size = min(position.size, initial_size * allocation)
            close_position = allocation >= Decimal("0.20") and reason == "tp3"
            self._exit_position(
                db,
                position,
                exit_price=exit_price,
                exit_size=exit_size,
                reason=reason,
                close_position=close_position,
                commit=False,
            )
            exit_count += 1
            if position.status != "open":
                closed_count += 1
        return exit_count, closed_count

    def _initial_size(self, position: Position) -> Decimal:
        metadata = position.metadata_json or {}
        try:
            return Decimal(str(metadata.get("initial_size") or position.size))
        except Exception:
            return position.size

    def _build_take_profit_plan(self, signal: Signal) -> list[dict[str, str | bool]]:
        plan: list[dict[str, str | bool]] = []
        for level, field_name, allocation in TAKE_PROFIT_ALLOCATIONS:
            price = getattr(signal, field_name)
            if price is None:
                continue
            plan.append(
                {
                    "level": level,
                    "price": str(price),
                    "allocation": str(allocation),
                    "filled": False,
                }
            )
        return plan

    def _ensure_take_profit_plan(self, db: Session, position: Position) -> None:
        metadata = dict(position.metadata_json or {})
        if not metadata.get("initial_size"):
            metadata["initial_size"] = str(position.size)
        if not metadata.get("remaining_size"):
            metadata["remaining_size"] = str(position.size)
        if metadata.get("take_profit_plan"):
            position.metadata_json = metadata
            return
        signal = self._source_signal(db, metadata)
        if signal is not None:
            metadata["take_profit_plan"] = self._build_take_profit_plan(signal)
        elif position.take_profit is not None:
            metadata["take_profit_plan"] = [
                {
                    "level": "take_profit",
                    "price": str(position.take_profit),
                    "allocation": "1",
                    "filled": False,
                }
            ]
        position.metadata_json = metadata

    def _source_signal(self, db: Session, metadata: dict[str, Any]) -> Signal | None:
        source_signal_id = metadata.get("source_signal_id")
        if source_signal_id is None:
            return None
        try:
            signal_id = int(source_signal_id)
        except (TypeError, ValueError):
            return None
        return db.get(Signal, signal_id)

    def _take_profit_plan(self, position: Position) -> list[dict[str, Any]]:
        plan = (position.metadata_json or {}).get("take_profit_plan")
        if isinstance(plan, list) and plan:
            return plan
        if position.take_profit is None:
            return []
        return [
            {
                "level": "take_profit",
                "price": str(position.take_profit),
                "allocation": "1",
                "filled": False,
            }
        ]

    def _mark_take_profit_filled(self, metadata: dict[str, Any], reason: str) -> None:
        plan = metadata.get("take_profit_plan")
        if not isinstance(plan, list):
            return
        for item in plan:
            if isinstance(item, dict) and item.get("level") == reason:
                item["filled"] = True

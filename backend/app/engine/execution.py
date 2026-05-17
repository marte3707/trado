import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction, OrderMode, SignalStatus
from app.core.runtime_settings import effective_live_execution_settings, effective_paper_equity
from app.db.models import AppSetting, Candle, DecisionLog, Market, Order, Position, Signal
from app.engine.order_validation import LiveOrderPlan, OrderValidator
from app.engine.position_management import PositionManagementEngine
from app.engine.risk import calculate_position_size
from app.services.hyperliquid import HyperliquidError, HyperliquidTradingClient

logger = structlog.get_logger()

CLOID_NAMESPACE = uuid.UUID("c5d8a3f2-1b6e-4c9a-9e3d-7f4b8c2e9a0d")


def make_cloid(signal_id: int, kind: str) -> str:
    return uuid.uuid5(CLOID_NAMESPACE, f"sig:{signal_id}:{kind}").hex


def coin_for_market(market: Market) -> str:
    """Return the Hyperliquid coin id for a Market row.

    Prefers `metadata_json["hl_coin"]` (set by MarketSync), falls back to the
    canonical `symbol`. Raises ValueError on an unmapped market so the caller
    cannot accidentally submit an empty coin to the exchange.
    """
    coin = (market.metadata_json or {}).get("hl_coin") or market.symbol
    if not isinstance(coin, str) or not coin:
        raise ValueError("market has no Hyperliquid coin mapping")
    return coin


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
# Residual size below which we treat a position as fully closed (rounding noise).
POSITION_DUST_THRESHOLD = Decimal("0.0000000001")
# Minimum TP allocation that, when hit, also closes the rest of the position.
# Matches the tp3 allocation by design so hitting tp3 sweeps the tail.
TP_FINAL_CLOSE_ALLOCATION = Decimal("0.20")


class OrderExecutionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.validator = OrderValidator()
        self.position_manager = PositionManagementEngine(settings)

    def execute_paper(self, db: Session, signal: Signal) -> Position | None:
        existing = (
            db.query(Position)
            .filter(
                Position.market_id == signal.market_id,
                Position.mode == OrderMode.PAPER.value,
                Position.status == "open",
            )
            .first()
        )
        if existing is not None:
            db.add(
                DecisionLog(
                    event_type="paper_order_skipped_duplicate_ticker",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Paper position skipped: already open for this ticker",
                    data={
                        "existing_position_id": existing.id,
                        "existing_timeframe": (existing.metadata_json or {}).get("source_timeframe"),
                        "signal_timeframe": signal.timeframe,
                    },
                )
            )
            db.commit()
            return None
        side = "long" if signal.direction in {Direction.LONG, Direction.STRONG_LONG} else "short"
        risk_settings = db.get(AppSetting, "risk")
        max_risk_per_trade_pct = self.settings.max_risk_per_trade_pct
        max_leverage = self.settings.max_leverage
        if risk_settings:
            max_risk_per_trade_pct = float(
                risk_settings.value.get("max_risk_per_trade_pct", max_risk_per_trade_pct)
            )
            max_leverage = float(risk_settings.value.get("max_leverage", max_leverage))
        equity = effective_paper_equity(db, self.settings)
        size = calculate_position_size(
            equity=equity,
            risk_pct=max_risk_per_trade_pct,
            entry_price=float(signal.entry_price),
            stop_loss=float(signal.stop_loss or signal.entry_price),
            leverage=max_leverage,
        )
        notional = Decimal(str(size)) * signal.entry_price
        leverage = notional / Decimal(str(equity)) if equity > 0 else Decimal("0")
        metadata = self.position_manager.initial_metadata(
            db,
            signal,
            size=Decimal(str(size)),
            planned_notional=notional,
            base={
                "source_signal_id": signal.id,
                "source_timeframe": signal.timeframe,
                "initial_size": str(size),
                "remaining_size": str(size),
                "account_value_at_entry": str(equity),
                "take_profit_plan": self._build_take_profit_plan(db, signal),
            },
        )
        position = Position(
            market_id=signal.market_id,
            mode=OrderMode.PAPER.value,
            side=side,
            entry_price=signal.entry_price,
            size=Decimal(str(size)),
            leverage=leverage,
            margin_mode=None,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_3 or signal.take_profit_2 or signal.take_profit_1,
            status="open",
            metadata_json=metadata,
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

    def execute_live(
        self,
        db: Session,
        signal: Signal,
        trading_client: HyperliquidTradingClient | None = None,
    ) -> Position:
        trading = trading_client or HyperliquidTradingClient(self.settings)
        account_value = trading.account_value()
        plan = self._build_live_order_plan(db, signal, account_value)
        if not self._validate_or_reject(db, signal, plan):
            raise PermissionError(
                "; ".join(
                    self.validator.validate_live_execution(db, signal, self.settings, plan).reasons
                )
            )
        market = db.get(Market, signal.market_id)
        if market is None:
            raise PermissionError("market not found")
        live_settings = effective_live_execution_settings(db, self.settings)
        try:
            coin = coin_for_market(market)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        sz_dec = _sz_decimals(market)
        order_size = _round_size(plan.size, sz_dec)
        market_cloid = make_cloid(signal.id, "mkt")
        existing_position = self._find_idempotent_position(db, signal, market_cloid)
        if existing_position is not None:
            return existing_position
        trading.set_leverage(coin, plan.max_leverage, is_cross=False)
        entry_result = trading.submit_market_order(
            coin=coin,
            is_buy=plan.side == "long",
            size=order_size,
            max_slippage_pct=float(live_settings["max_slippage_pct"]),
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
            cloid=market_cloid,
        )
        if entry_result.status != "ok":
            db.add(
                DecisionLog(
                    event_type="live_order_rejected_by_exchange",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Live order rejected by Hyperliquid",
                    data={"response": entry_result.raw_response},
                )
            )
            db.commit()
            raise HyperliquidError("live order rejected by Hyperliquid")
        position = self._persist_live_position(
            db, signal, plan, entry_result, order_size, account_value, market_cloid
        )
        db.commit()
        db.refresh(position)
        self._submit_live_protection_orders(db, trading, position, signal, coin, sz_dec)
        return position

    def _validate_or_reject(self, db: Session, signal: Signal, plan: LiveOrderPlan) -> bool:
        """Run safety validation and persist a rejection log on failure.

        Returns True when execution may proceed, False when the caller should
        raise PermissionError with the validation reasons.
        """
        validation = self.validator.validate_live_execution(db, signal, self.settings, plan)
        if validation.ok:
            return True
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
        return False

    def _find_idempotent_position(
        self, db: Session, signal: Signal, market_cloid: str
    ) -> Position | None:
        """If a previous attempt already submitted the same market order, return
        its existing Position so the caller can skip the re-submit."""
        existing_market_order = (
            db.query(Order)
            .filter(Order.cloid == market_cloid, Order.status != "canceled")
            .first()
        )
        if existing_market_order is None or existing_market_order.position_id is None:
            return None
        existing_position = db.get(Position, existing_market_order.position_id)
        if existing_position is None:
            return None
        logger.warning(
            "execute_live_idempotent_skip",
            signal_id=signal.id,
            cloid=market_cloid,
            position_id=existing_position.id,
        )
        return existing_position

    def _persist_live_position(
        self,
        db: Session,
        signal: Signal,
        plan: LiveOrderPlan,
        entry_result: Any,
        requested_size: Decimal,
        account_value: Decimal,
        market_cloid: str,
    ) -> Position:
        filled_size = entry_result.filled_size or requested_size
        entry_price = entry_result.avg_price or signal.entry_price
        metadata = self.position_manager.initial_metadata(
            db,
            signal,
            size=filled_size,
            planned_notional=plan.notional,
            base={
                "source_signal_id": signal.id,
                "source_timeframe": signal.timeframe,
                "initial_size": str(filled_size),
                "remaining_size": str(filled_size),
                "account_value_at_entry": str(account_value),
                "contract_leverage": plan.max_leverage,
                "entry_response": entry_result.raw_response,
                "take_profit_plan": self._build_take_profit_plan(db, signal),
                "funding_rate": (signal.strategy_breakdown or {}).get("_funding_rate"),
            },
        )
        position = Position(
            market_id=signal.market_id,
            hyperliquid_position_id=entry_result.oid,
            mode=OrderMode.LIVE.value,
            side=plan.side,
            entry_price=entry_price,
            size=filled_size,
            leverage=plan.leverage,
            margin_mode="isolated",
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_3 or signal.take_profit_2 or signal.take_profit_1,
            status="open",
            metadata_json=metadata,
        )
        db.add(position)
        db.flush()
        db.add(
            Order(
                position_id=position.id,
                signal_id=signal.id,
                hyperliquid_order_id=entry_result.oid,
                cloid=market_cloid,
                mode=OrderMode.LIVE.value,
                order_type="market",
                side=plan.side,
                price=entry_price,
                size=filled_size,
                status="filled" if entry_result.filled_size is not None else "submitted",
                reduce_only=False,
                raw_response=entry_result.raw_response,
            )
        )
        signal.status = SignalStatus.EXECUTED.value
        db.add(
            DecisionLog(
                event_type="live_order_submitted",
                market_id=signal.market_id,
                signal_id=signal.id,
                position_id=position.id,
                message="Live position opened from signal",
                data={
                    "size": str(filled_size),
                    "side": plan.side,
                    "entry_price": str(entry_price),
                    "hyperliquid_order_id": entry_result.oid,
                },
            )
        )
        return position

    def _build_live_order_plan(self, db: Session, signal: Signal, account_value: Decimal) -> LiveOrderPlan:
        side = "long" if signal.direction in {Direction.LONG, Direction.STRONG_LONG} else "short"
        risk_settings = db.get(AppSetting, "risk")
        risk_pct = self.settings.max_risk_per_trade_pct
        max_leverage = self.settings.max_leverage
        if risk_settings:
            risk_pct = float(risk_settings.value.get("max_risk_per_trade_pct", risk_pct))
            max_leverage = float(risk_settings.value.get("max_leverage", max_leverage))
        stop_loss = signal.stop_loss or signal.entry_price
        size = Decimal(
            str(
                calculate_position_size(
                    equity=float(account_value),
                    risk_pct=risk_pct,
                    entry_price=float(signal.entry_price),
                    stop_loss=float(stop_loss),
                    leverage=max_leverage,
                )
            )
        )
        live_settings = effective_live_execution_settings(db, self.settings)
        notional_cap = Decimal(str(live_settings["order_notional_cap_usd"]))
        notional_floor = Decimal(str(live_settings["min_order_notional_usd"]))
        if signal.entry_price > 0 and size * signal.entry_price > notional_cap:
            size = notional_cap / signal.entry_price
        notional = size * signal.entry_price
        if (
            live_settings["bump_to_min_notional"]
            and signal.entry_price > 0
            and notional < notional_floor
        ):
            target_notional = min(notional_floor, notional_cap)
            market = db.get(Market, signal.market_id)
            sz_dec = _sz_decimals(market) if market is not None else 8
            quantizer = Decimal(10) ** -sz_dec
            size = (target_notional / signal.entry_price).quantize(
                quantizer, rounding=ROUND_CEILING
            )
            notional = size * signal.entry_price
            logger.warning(
                "live_order_size_bumped_to_min_notional",
                signal_id=signal.id,
                market_id=signal.market_id,
                target_notional=str(target_notional),
                final_notional=str(notional),
                final_size=str(size),
            )
        leverage = notional / account_value if account_value > 0 else Decimal("0")
        risk_amount = abs(signal.entry_price - stop_loss) * size
        target_leverage = _target_leverage(signal, int(max_leverage))
        return LiveOrderPlan(
            side=side,
            size=size,
            notional=notional,
            leverage=leverage,
            account_value=account_value,
            max_leverage=target_leverage,
            risk_amount=risk_amount,
        )

    def _submit_live_protection_orders(
        self,
        db: Session,
        trading: HyperliquidTradingClient,
        position: Position,
        signal: Signal,
        coin: str,
        sz_decimals: int = 8,
    ) -> None:
        live_settings = effective_live_execution_settings(db, self.settings)
        expires_after_seconds = int(live_settings["order_expires_after_seconds"])
        close_is_buy = position.side.lower() == "short"
        protection_errors: list[str] = []

        if signal.stop_loss is not None:
            sl_cloid = make_cloid(signal.id, "sl")
            existing_sl = (
                db.query(Order)
                .filter(Order.cloid == sl_cloid, Order.status != "canceled")
                .first()
            )
            if existing_sl is not None:
                logger.warning(
                    "execute_live_idempotent_sl_skip",
                    signal_id=signal.id,
                    cloid=sl_cloid,
                    order_id=existing_sl.id,
                )
            else:
                try:
                    stop_result = trading.submit_trigger_order(
                        coin=coin,
                        is_buy=close_is_buy,
                        size=_round_size(position.size, sz_decimals),
                        trigger_price=signal.stop_loss,
                        tpsl="sl",
                        expires_after_seconds=expires_after_seconds,
                        cloid=sl_cloid,
                    )
                    db.add(
                        Order(
                            position_id=position.id,
                            signal_id=signal.id,
                            hyperliquid_order_id=stop_result.oid,
                            cloid=sl_cloid,
                            mode=OrderMode.LIVE.value,
                            order_type="stop_loss",
                            side="buy" if close_is_buy else "sell",
                            price=signal.stop_loss,
                            size=position.size,
                            status=stop_result.status,
                            reduce_only=True,
                            raw_response=stop_result.raw_response,
                        )
                    )
                except Exception as exc:
                    protection_errors.append(f"stop_loss: {exc}")

        initial_size = position.size
        skipped_tp_levels: list[dict[str, Any]] = []
        for item in self._take_profit_plan(position):
            if item.get("filled") or item.get("runner"):
                continue
            try:
                level = str(item.get("level", "take_profit"))
                price = Decimal(str(item["price"]))
                allocation = Decimal(str(item["allocation"]))
            except (KeyError, TypeError, ValueError):
                continue
            tp_size = _round_size(initial_size * allocation, sz_decimals)
            if tp_size <= 0:
                skipped_tp_levels.append(
                    {
                        "level": level,
                        "allocation": str(allocation),
                        "rounded_size": str(tp_size),
                        "initial_size": str(initial_size),
                        "sz_decimals": sz_decimals,
                    }
                )
                logger.warning(
                    "live_protection_tp_size_too_small",
                    signal_id=signal.id,
                    position_id=position.id,
                    level=level,
                    allocation=str(allocation),
                    initial_size=str(initial_size),
                )
                continue
            tp_cloid = make_cloid(signal.id, level)
            existing_tp = (
                db.query(Order)
                .filter(Order.cloid == tp_cloid, Order.status != "canceled")
                .first()
            )
            if existing_tp is not None:
                logger.warning(
                    "execute_live_idempotent_tp_skip",
                    signal_id=signal.id,
                    cloid=tp_cloid,
                    level=level,
                    order_id=existing_tp.id,
                )
                continue
            try:
                tp_result = trading.submit_trigger_order(
                    coin=coin,
                    is_buy=close_is_buy,
                    size=tp_size,
                    trigger_price=price,
                    tpsl="tp",
                    expires_after_seconds=expires_after_seconds,
                    cloid=tp_cloid,
                )
                db.add(
                    Order(
                        position_id=position.id,
                        signal_id=signal.id,
                        hyperliquid_order_id=tp_result.oid,
                        cloid=tp_cloid,
                        mode=OrderMode.LIVE.value,
                        order_type=level,
                        side="buy" if close_is_buy else "sell",
                        price=price,
                        size=tp_size,
                        status=tp_result.status,
                        reduce_only=True,
                        raw_response=tp_result.raw_response,
                    )
                )
            except Exception as exc:
                protection_errors.append(f"{level}: {exc}")

        metadata = dict(position.metadata_json or {})
        if skipped_tp_levels:
            metadata["protection_orders_skipped"] = skipped_tp_levels
        if protection_errors:
            metadata["protection_order_errors"] = protection_errors
            event_type = "live_protection_orders_partial_failure"
            message = "One or more live protection orders failed"
        elif skipped_tp_levels:
            event_type = "live_protection_orders_partial_skipped"
            message = "Some TP levels rounded to zero and were skipped"
        else:
            metadata["protection_orders_submitted"] = True
            event_type = "live_protection_orders_submitted"
            message = "Live stop loss and take profit orders submitted"
        position.metadata_json = metadata
        db.add(position)
        db.add(
            DecisionLog(
                event_type=event_type,
                market_id=position.market_id,
                signal_id=signal.id,
                position_id=position.id,
                message=message,
                data={"errors": protection_errors, "skipped": skipped_tp_levels},
            )
        )
        db.commit()

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

    def close_live_position(
        self,
        db: Session,
        position: Position,
        reason: str = "manual",
        trading_client: HyperliquidTradingClient | None = None,
    ) -> None:
        if position.mode != OrderMode.LIVE.value:
            self.close_position(db, position, position.entry_price, reason)
            return
        market = db.get(Market, position.market_id)
        if market is None:
            raise ValueError("market not found")
        coin = coin_for_market(market)
        live_settings = effective_live_execution_settings(db, self.settings)
        close_is_buy = position.side.lower() == "short"
        trading = trading_client or HyperliquidTradingClient(self.settings)
        result = trading.submit_market_close(
            coin=coin,
            is_buy=close_is_buy,
            size=position.size,
            max_slippage_pct=float(live_settings["max_slippage_pct"]),
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
        )
        if result.status != "ok":
            db.add(
                DecisionLog(
                    event_type="live_close_rejected_by_exchange",
                    market_id=position.market_id,
                    position_id=position.id,
                    message="Live close order rejected by Hyperliquid",
                    data={"response": result.raw_response},
                )
            )
            db.commit()
            raise HyperliquidError("live close rejected by Hyperliquid")
        exit_price = result.avg_price or position.entry_price
        exit_size = result.filled_size or position.size
        self._exit_position(
            db,
            position,
            exit_price=exit_price,
            exit_size=exit_size,
            reason=reason,
            close_position=exit_size >= position.size,
            commit=False,
            record_order=False,
        )
        db.add(
            Order(
                position_id=position.id,
                signal_id=_signal_id_from_position(position),
                hyperliquid_order_id=result.oid,
                mode=OrderMode.LIVE.value,
                order_type=reason,
                side="buy" if close_is_buy else "sell",
                price=exit_price,
                size=exit_size,
                status="filled" if result.filled_size is not None else "submitted",
                reduce_only=True,
                raw_response=result.raw_response,
            )
        )
        db.commit()

    def _exit_position(
        self,
        db: Session,
        position: Position,
        exit_price: Decimal,
        exit_size: Decimal,
        reason: str,
        close_position: bool,
        commit: bool = True,
        record_order: bool = True,
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
        if close_position or remaining_size <= POSITION_DUST_THRESHOLD:
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
        if record_order:
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
            if position.status == "open":
                updated = self.position_manager.manage_paper_position_for_candle(
                    db, market, position, [candle], candle
                )
                exits += 1 if updated else 0
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
                if position.status == "open":
                    updated = self.position_manager.manage_paper_position_for_candle(
                        db, market, position, candles, candle
                    )
                    exits += 1 if updated else 0
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
            except Exception:
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
            close_position = allocation >= TP_FINAL_CLOSE_ALLOCATION and reason == "tp3"
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

    def _build_take_profit_plan(self, db: Session, signal: Signal) -> list[dict[str, str | bool]]:
        dynamic_plan = self.position_manager.build_take_profit_plan(db, signal)
        if dynamic_plan:
            return dynamic_plan
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
            metadata["take_profit_plan"] = self._build_take_profit_plan(db, signal)
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
                metadata["partial_exit_count"] = int(metadata.get("partial_exit_count") or 0) + 1
                if reason == "tp1":
                    metadata["tp1_hit"] = True
                elif reason == "tp2":
                    metadata["tp2_hit"] = True
                    metadata["runner_active"] = True


def _sz_decimals(market: Market) -> int:
    try:
        return int((market.metadata_json or {}).get("raw", {}).get("szDecimals", 8))
    except (TypeError, ValueError):
        return 8


def _round_size(size: Decimal, decimals: int) -> Decimal:
    quantizer = Decimal(10) ** -decimals
    return size.quantize(quantizer)


def _target_leverage(signal: Signal, max_leverage: int) -> int:
    """Scale leverage linearly with signal score; STRONG direction adds 20% bonus."""
    score = float(signal.score or 0)
    factor = score / 100.0
    if signal.direction in {Direction.STRONG_LONG, Direction.STRONG_SHORT}:
        factor = min(1.0, factor * 1.2)
    return max(1, min(max_leverage, round(max_leverage * factor)))


def _signal_id_from_position(position: Position) -> int | None:
    source_signal_id = (position.metadata_json or {}).get("source_signal_id")
    if source_signal_id is None:
        return None
    try:
        return int(source_signal_id)
    except (TypeError, ValueError):
        return None

import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction, OperationMode, OrderMode, SignalStatus
from app.core.runtime_settings import (
    effective_live_execution_settings,
    effective_live_trading_enabled,
    effective_new_live_entries_enabled,
    effective_operation_mode,
    effective_paper_equity,
    effective_position_management_settings,
    setting_value,
)
from app.db.models import (
    AppSetting,
    Candle,
    DecisionLog,
    Market,
    MarketL2Snapshot,
    Order,
    Position,
    Signal,
)
from app.engine.experiment import signal_experiment_tag
from app.engine.live_eligibility import evaluate_live_readiness_gate
from app.engine.order_validation import LiveOrderPlan, OrderValidator
from app.engine.portfolio_guard import PortfolioGuard, PortfolioGuardDecision
from app.engine.position_management import (
    LIQUIDATION_GUARD_SL_SAFETY_BUFFER,
    LiquidationGuardResult,
    PositionManagementEngine,
)
from app.engine.risk import calculate_position_size
from app.services.hyperliquid import (
    HyperliquidError,
    HyperliquidTradingClient,
    HyperliquidTransientError,
)

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


def _live_guard_block_reason(
    db: Session,
    settings: Settings,
    *,
    signal: Signal | None = None,
) -> str | None:
    """Re-check, just before submitting a live market order, the runtime guards an
    operator could have toggled during the network-bound pre-flight window (kill
    switch / operation mode / live-trading flag). Returns a human-readable reason
    when the order must be aborted, or None when it may proceed."""
    if effective_operation_mode(db, settings) != OperationMode.AUTO_TRADE:
        return "operation mode is not AUTO_TRADE"
    if not effective_live_trading_enabled(db, settings):
        return "ENABLE_LIVE_TRADING is false"
    if _is_paper_research_candidate_signal(signal):
        return "research candidate is paper-only"
    if not effective_new_live_entries_enabled(db, settings) and not _is_the_bull_live_signal(
        db, signal
    ):
        return "new live entries are paused"
    kill_switch = db.get(AppSetting, "kill_switch")
    if kill_switch and kill_switch.value.get("enabled"):
        return "kill switch enabled"
    return None


def _exception_status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None) if response is not None else None
    return response_status if isinstance(response_status, int) else None


def _safe_error_message(exc: Exception, max_length: int = 500) -> str:
    message = str(exc)
    if len(message) <= max_length:
        return message
    return f"{message[:max_length]}..."


def _paper_l2_fill_price(
    db: Session,
    market_id: int,
    *,
    side: str,
    size: Decimal,
    now: datetime | None = None,
) -> Decimal | None:
    """Conservative full-size VWAP from a recent local book; no network or orders."""
    if size <= 0 or side not in {"long", "short"}:
        return None
    snapshot = db.get(MarketL2Snapshot, market_id)
    if snapshot is None or snapshot.updated_at is None:
        return None
    observed_at = snapshot.updated_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - observed_at).total_seconds()
    if not 0 <= age <= 15:
        return None
    levels = snapshot.asks if side == "long" else snapshot.bids
    if not isinstance(levels, list) or not levels:
        return None
    parsed: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        if not isinstance(level, dict):
            return None
        try:
            price = Decimal(str(level["px"]))
            quantity = Decimal(str(level["sz"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return None
        if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
            return None
        parsed.append((price, quantity))
    parsed.sort(key=lambda item: item[0], reverse=side == "short")
    remaining = size
    quote_cost = Decimal("0")
    for price, quantity in parsed:
        filled = min(remaining, quantity)
        quote_cost += filled * price
        remaining -= filled
        if remaining <= 0:
            return quote_cost / size
    return None


def _depth_estimate_data(depth_estimate: Any | None) -> dict[str, Any]:
    if depth_estimate is None:
        return {}
    data: dict[str, Any] = {}
    fields = {
        "limit_px": "limit_price",
        "best_bid": "best_bid",
        "best_ask": "best_ask",
        "side_level_count": "side_level_count",
        "fillable_level_count": "fillable_level_count",
    }
    for attr, key in fields.items():
        value = getattr(depth_estimate, attr, None)
        if value is None:
            continue
        data[key] = str(value) if isinstance(value, Decimal) else value
    return data


def _depth_price_outside_slippage(depth_estimate: Any | None, *, is_long: bool) -> bool:
    if depth_estimate is None:
        return False
    limit_px = getattr(depth_estimate, "limit_px", None)
    side_px = getattr(depth_estimate, "best_ask" if is_long else "best_bid", None)
    if limit_px is None or side_px is None:
        return False
    try:
        return side_px > limit_px if is_long else side_px < limit_px
    except (InvalidOperation, TypeError):
        return False


_HYPERLIQUID_RATE_LIMIT_COOLDOWN_SECONDS = 600
_HYPERLIQUID_RATE_LIMIT_EVENTS = {
    "fast_risk_cycle_failed",
    "live_order_preflight_failed",
    "live_set_leverage_failed",
    "live_stop_update_cancel_failed",
    "live_stop_update_submit_failed",
}


def _recent_hyperliquid_rate_limit(db: Session) -> DecisionLog | None:
    cutoff = datetime.now(UTC) - timedelta(seconds=_HYPERLIQUID_RATE_LIMIT_COOLDOWN_SECONDS)
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.timestamp >= cutoff)
        .filter(DecisionLog.event_type.in_(_HYPERLIQUID_RATE_LIMIT_EVENTS))
        .all()
    )
    recent: DecisionLog | None = None
    for row in rows:
        data = row.data if isinstance(row.data, dict) else {}
        if "429" not in f"{row.message} {data}":
            continue
        if recent is None or row.timestamp > recent.timestamp:
            recent = row
    return recent


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


def rescale_take_profit_plan_for_min_notional(
    plan: list[dict[str, Any]],
    position_size: Decimal,
    min_reduce_notional: Decimal,
    sz_decimals: int,
    *,
    repair_skipped: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Consolidate TP tranches that Hyperliquid cannot accept individually.

    The earliest pending TP keeps its price and absorbs later allocations until
    its rounded order size reaches the reduce-only minimum. If all fixed TPs
    are still too small, only the minimum required runner allocation is used.
    """
    original = [dict(item) for item in plan if isinstance(item, dict)]
    if position_size <= 0 or min_reduce_notional <= 0:
        return original, []

    runner_entries = [dict(item) for item in original if item.get("runner")]
    runner = runner_entries[0] if runner_entries else None
    runner_allocation = Decimal("0")
    if runner is not None and not runner.get("filled"):
        try:
            runner_allocation = Decimal(str(runner.get("allocation") or 0))
        except (InvalidOperation, TypeError, ValueError):
            runner_allocation = Decimal("0")

    rescaled: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], Decimal, Decimal]] = []

    def is_tradable(price: Decimal, allocation: Decimal) -> tuple[bool, Decimal, Decimal]:
        size = _round_size(position_size * allocation, sz_decimals)
        notional = abs(size * price)
        return size > 0 and notional >= min_reduce_notional, size, notional

    def clean_entry(entry: dict[str, Any], allocation: Decimal) -> dict[str, Any]:
        cleaned = dict(entry)
        cleaned["allocation"] = str(allocation)
        for key in (
            "skipped",
            "skip_reason",
            "notional_at_skip",
            "min_reduce_only_notional",
        ):
            cleaned.pop(key, None)
        return cleaned

    def append_consolidated(group: list[tuple[dict[str, Any], Decimal, Decimal]]) -> None:
        allocation = sum((item[1] for item in group), Decimal("0"))
        base, _, price = group[0]
        rescaled.append(clean_entry(base, allocation))
        for merged, merged_allocation, _ in group[1:]:
            changes.append(
                {
                    "level": str(merged.get("level", "")),
                    "action": "merged_into_previous_tp",
                    "merged_into": str(base.get("level", "")),
                    "allocation": str(merged_allocation),
                    "notional": str(is_tradable(price, merged_allocation)[2]),
                }
            )

    for item in original:
        if item.get("runner"):
            continue
        level = str(item.get("level", ""))
        repairable_skip = bool(
            repair_skipped
            and item.get("skipped")
            and item.get("skip_reason") == "below_min_reduce_only_notional"
        )
        if (
            level not in {"tp1", "tp2", "tp3"}
            or item.get("filled")
            or (item.get("skipped") and not repairable_skip)
        ):
            rescaled.append(dict(item))
            continue
        try:
            price = Decimal(str(item["price"]))
            allocation = Decimal(str(item["allocation"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            rescaled.append(dict(item))
            continue

        tradable, _, notional = is_tradable(price, allocation)
        previous_tp = next(
            (
                entry
                for entry in reversed(rescaled)
                if not entry.get("runner")
                and not entry.get("filled")
                and not entry.get("skipped")
                and str(entry.get("level", "")) in {"tp1", "tp2", "tp3"}
            ),
            None,
        )
        if not tradable and previous_tp is not None and not pending:
            previous_allocation = Decimal(str(previous_tp["allocation"]))
            previous_tp["allocation"] = str(previous_allocation + allocation)
            changes.append(
                {
                    "level": level,
                    "action": "merged_into_previous_tp",
                    "merged_into": previous_tp.get("level"),
                    "allocation": str(allocation),
                    "notional": str(notional),
                }
            )
            continue

        if tradable and not pending:
            rescaled.append(clean_entry(item, allocation))
            continue

        pending.append((dict(item), allocation, price))
        pending_allocation = sum((entry[1] for entry in pending), Decimal("0"))
        pending_tradable, _, _ = is_tradable(pending[0][2], pending_allocation)
        if pending_tradable:
            append_consolidated(pending)
            pending = []

    if pending:
        pending_allocation = sum((item[1] for item in pending), Decimal("0"))
        target_price = pending[0][2]
        size_step = Decimal(10) ** -sz_decimals
        required_size = (min_reduce_notional / abs(target_price)).quantize(
            size_step,
            rounding=ROUND_CEILING,
        )
        required_allocation = required_size / position_size
        borrowed = max(Decimal("0"), required_allocation - pending_allocation)
        if borrowed <= runner_allocation and required_allocation <= Decimal("1"):
            base, _, _ = pending[0]
            rescaled.append(clean_entry(base, required_allocation))
            for merged, merged_allocation, _ in pending[1:]:
                changes.append(
                    {
                        "level": str(merged.get("level", "")),
                        "action": "merged_into_previous_tp",
                        "merged_into": str(base.get("level", "")),
                        "allocation": str(merged_allocation),
                    }
                )
            if borrowed > 0:
                runner_allocation -= borrowed
                changes.append(
                    {
                        "level": "runner",
                        "action": "partially_merged_into_tp",
                        "merged_into": str(base.get("level", "")),
                        "allocation": str(borrowed),
                    }
                )
        else:
            for entry, allocation, price in pending:
                _, _, notional = is_tradable(price, allocation)
                skipped = dict(entry)
                skipped["skipped"] = True
                skipped["skip_reason"] = "below_min_reduce_only_notional"
                skipped["notional_at_skip"] = str(notional)
                skipped["min_reduce_only_notional"] = str(min_reduce_notional)
                rescaled.append(skipped)
                changes.append(
                    {
                        "level": str(entry.get("level", "")),
                        "action": "skipped",
                        "allocation": str(allocation),
                        "notional": str(notional),
                    }
                )

    if runner is not None and not runner.get("filled"):
        if runner_allocation > 0:
            runner["allocation"] = str(runner_allocation)
            rescaled.append(runner)
        elif Decimal(str(runner.get("allocation") or 0)) > 0:
            changes.append(
                {
                    "level": "runner",
                    "action": "fully_merged_into_tp",
                    "allocation": str(runner.get("allocation")),
                }
            )
    elif runner is not None:
        rescaled.append(runner)
    for extra_runner in runner_entries[1:]:
        rescaled.append(extra_runner)

    return rescaled, changes

# Residual size below which we treat a position as fully closed (rounding noise).
POSITION_DUST_THRESHOLD = Decimal("0.0000000001")
# Minimum TP allocation that, when hit, also closes the rest of the position.
# Matches the tp3 allocation by design so hitting tp3 sweeps the tail.
TP_FINAL_CLOSE_ALLOCATION = Decimal("0.20")
# Keep isolated liquidation comfortably beyond the planned stop. Hyperliquid's
# exact liquidation price includes maintenance margin, so a multiplicative
# buffer can still be too permissive for wider stops.
LIQUIDATION_STOP_DISTANCE_ABSOLUTE_BUFFER = Decimal("0.10")
# Buffer applicato allo stop aggiustato rispetto al liq price quando il guard fallisce:
# lo stop viene posizionato a liq_price * (1 ± BUFFER) per restare sul lato sicuro.

def risk_at_open_snapshot(db: Session) -> dict[str, Any]:
    """Snapshot dei parametri di rischio operativi al momento dell'apertura.

    I setting cambiano nel tempo, quindi vengono persistiti sulla posizione per
    rendere riproducibile l'attribuzione forward senza stato implicito.
    """
    risk = db.get(AppSetting, "risk")
    risk_value = risk.value if risk is not None and isinstance(risk.value, dict) else {}
    return {
        "min_signal_score": risk_value.get("min_signal_score"),
        "min_risk_reward": risk_value.get("min_risk_reward"),
        "max_risk_per_trade_pct": risk_value.get("max_risk_per_trade_pct"),
    }


class OrderExecutionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.validator = OrderValidator()
        self.position_manager = PositionManagementEngine(settings)
        self.portfolio_guard = PortfolioGuard(settings)

    def execute_paper(self, db: Session, signal: Signal) -> Position | None:
        existing_order = (
            db.query(Order)
            .filter(
                Order.signal_id == signal.id,
                Order.mode == OrderMode.PAPER.value,
                Order.order_type == "market",
                Order.position_id.is_not(None),
            )
            .first()
        )
        if existing_order is not None and existing_order.position_id is not None:
            existing_position = db.get(Position, existing_order.position_id)
            if existing_position is not None:
                logger.warning(
                    "execute_paper_idempotent_skip",
                    signal_id=signal.id,
                    position_id=existing_position.id,
                )
                return existing_position
            db.add(
                DecisionLog(
                    event_type="paper_order_skipped_duplicate_signal",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Paper position skipped: signal already has a paper order",
                    data={
                        "existing_order_id": existing_order.id,
                        "existing_position_id": existing_order.position_id,
                    },
                )
            )
            db.commit()
            return None
        open_paper_position = (
            db.query(Position)
            .filter(
                Position.market_id == signal.market_id,
                Position.mode == OrderMode.PAPER.value,
                Position.status == "open",
            )
            .first()
        )
        if open_paper_position is not None:
            logger.warning(
                "execute_paper_duplicate_market_skip",
                signal_id=signal.id,
                market_id=signal.market_id,
                position_id=open_paper_position.id,
            )
            db.add(
                DecisionLog(
                    event_type="paper_order_skipped_duplicate_market",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Paper position skipped: market already has an open paper position",
                    data={"existing_position_id": open_paper_position.id},
                )
            )
            db.commit()
            return open_paper_position
        side = "long" if signal.direction in {Direction.LONG, Direction.STRONG_LONG} else "short"
        risk_settings = db.get(AppSetting, "risk")
        max_risk_per_trade_pct = self.settings.max_risk_per_trade_pct
        max_leverage = self.settings.max_leverage
        if risk_settings:
            max_risk_per_trade_pct = float(
                risk_settings.value.get("max_risk_per_trade_pct", max_risk_per_trade_pct)
            )
            max_leverage = float(risk_settings.value.get("max_leverage", max_leverage))
        max_risk_per_trade_pct *= self.portfolio_guard.position_size_multiplier(db, signal)
        max_risk_per_trade_pct *= _signal_risk_multiplier(signal)
        equity = effective_paper_equity(db, self.settings)
        external_source = _external_source_metadata(signal)
        size = calculate_position_size(
            equity=equity,
            risk_pct=max_risk_per_trade_pct,
            entry_price=float(signal.entry_price),
            stop_loss=float(signal.stop_loss or signal.entry_price),
            leverage=max_leverage,
        )
        entry_price = signal.entry_price
        fill_source = "external_signal_plan" if external_source is not None else "fresh_l2_vwap"
        if external_source is None and size > 0:
            book_fill = _paper_l2_fill_price(
                db, signal.market_id, side=side, size=Decimal(str(size)),
            )
            if book_fill is None:
                db.add(DecisionLog(
                    event_type="paper_order_skipped_missing_fresh_l2",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Paper entry skipped: no fresh book with full fill depth",
                    data={"required_size": str(size)},
                ))
                db.commit()
                return None
            stop = signal.stop_loss
            first_target = signal.take_profit_1
            invalid_plan = (
                stop is None or first_target is None or
                (side == "long" and not stop < book_fill < first_target) or
                (side == "short" and not first_target < book_fill < stop)
            )
            if invalid_plan:
                db.add(DecisionLog(
                    event_type="paper_order_skipped_stale_plan",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Paper entry skipped: current fill no longer fits stop and first target",
                    data={"signal_price": str(signal.entry_price), "book_fill": str(book_fill)},
                ))
                db.commit()
                return None
            entry_price = book_fill
            resized = calculate_position_size(
                equity=equity,
                risk_pct=max_risk_per_trade_pct,
                entry_price=float(entry_price),
                stop_loss=float(stop),
                leverage=max_leverage,
            )
            risk_budget = (
                Decimal(str(equity)) * Decimal(str(max_risk_per_trade_pct)) / Decimal("100")
            )
            size = min(
                Decimal(str(size)),
                Decimal(str(resized)),
                risk_budget / abs(entry_price - stop),
            )
        notional = Decimal(str(size)) * entry_price
        live_settings = effective_live_execution_settings(db, self.settings)
        min_notional = Decimal(str(live_settings["min_order_notional_usd"]))
        if size <= 0 or notional < min_notional:
            logger.warning(
                "execute_paper_skipped_below_min_notional",
                signal_id=signal.id,
                market_id=signal.market_id,
                notional=str(notional),
                min_notional=str(min_notional),
            )
            db.add(
                DecisionLog(
                    event_type="paper_order_skipped_below_min_notional",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=f"Paper position skipped: notional ${float(notional):.2f} below minimum ${float(min_notional):.2f}",
                    data={"notional": str(notional), "min_notional": str(min_notional)},
                )
            )
            db.commit()
            return None
        decision = self.portfolio_guard.evaluate_entry(
            db,
            signal,
            mode=OrderMode.PAPER.value,
            side=side,
        )
        if not decision.allowed:
            self._record_portfolio_guard_rejection(
                db,
                signal,
                decision,
                mode=OrderMode.PAPER.value,
            )
            db.commit()
            return None
        leverage = notional / Decimal(str(equity)) if equity > 0 else Decimal("0")
        base_metadata: dict[str, Any] = {
            "source_signal_id": signal.id,
            "source_timeframe": signal.timeframe,
            "initial_size": str(size),
            "remaining_size": str(size),
            "account_value_at_entry": str(equity),
            "take_profit_plan": self._build_take_profit_plan(db, signal),
            "paper_fill_source": fill_source,
            "signal_plan_entry_price": str(signal.entry_price),
        }
        if external_source is not None:
            base_metadata["external_source"] = external_source
        breakdown = signal.strategy_breakdown or {}
        experiment_tag = signal_experiment_tag(breakdown)
        if experiment_tag is not None:
            base_metadata["experiment"] = experiment_tag
        regime_info = breakdown.get("_regime") if isinstance(breakdown, dict) else None
        if isinstance(regime_info, dict):
            base_metadata["regime_at_open"] = regime_info.get("regime", "unknown")
        base_metadata["risk_at_open"] = risk_at_open_snapshot(db)
        metadata = self.position_manager.initial_metadata(
            db,
            signal,
            size=Decimal(str(size)),
            planned_notional=notional,
            base=base_metadata,
            entry_price=entry_price,
        )
        position = Position(
            market_id=signal.market_id,
            mode=OrderMode.PAPER.value,
            side=side,
            entry_price=entry_price,
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
            price=entry_price,
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
                data={"size": str(size), "side": side, "fill_source": fill_source},
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
        guard_reason = _live_guard_block_reason(db, self.settings, signal=signal)
        if guard_reason is not None:
            db.add(
                DecisionLog(
                    event_type="live_order_aborted_kill_switch",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=f"Live order blocked before pre-flight: {guard_reason}",
                    data={"reason": guard_reason, "stage": "before_preflight"},
                )
            )
            db.commit()
            raise PermissionError(f"live order blocked before preflight: {guard_reason}")
        readiness = evaluate_live_readiness_gate(
            db,
            taker_fee_rate=self.settings.live_taker_fee_rate,
        )
        if not bool(readiness["ready"]):
            checks = readiness.get("checks", [])
            db.add(
                DecisionLog(
                    event_type="live_order_blocked_readiness",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Live entry blocked: paper profitability gate not satisfied",
                    data={"checks": checks, "metrics": readiness.get("metrics", {})},
                )
            )
            db.commit()
            raise PermissionError(
                "live readiness gate failed: " + "; ".join(str(item) for item in checks)
            )
        recent_rate_limit = _recent_hyperliquid_rate_limit(db)
        if recent_rate_limit is not None:
            db.add(
                DecisionLog(
                    event_type="live_order_preflight_deferred_rate_limit",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Live order pre-flight deferred during Hyperliquid rate-limit cooldown",
                    data={
                        "cooldown_seconds": _HYPERLIQUID_RATE_LIMIT_COOLDOWN_SECONDS,
                        "recent_event_type": recent_rate_limit.event_type,
                        "recent_event_at": recent_rate_limit.timestamp.isoformat(),
                    },
                )
            )
            db.commit()
            raise PermissionError("Hyperliquid rate-limit cooldown active; live preflight deferred")
        market = db.get(Market, signal.market_id)
        if market is None:
            raise PermissionError("market not found")
        is_spot = market.market_type == "spot"
        trading = trading_client or HyperliquidTradingClient(self.settings)
        try:
            account_value, available_margin = trading.account_summary()
        except Exception as exc:
            self._record_live_preflight_failure(db, signal, stage="account_summary", exc=exc)
            raise
        if is_spot:
            try:
                available_spot_usdc = trading.available_spot_usdc()
            except Exception as exc:
                self._record_live_preflight_failure(db, signal, stage="spot_usdc_balance", exc=exc)
                raise
            plan = self._build_spot_live_order_plan(
                db,
                signal,
                account_value,
                available_spot_usdc,
            )
        else:
            plan = self._build_live_order_plan(
                db,
                signal,
                account_value,
                sizing_equity=available_margin,
            )
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
        decision = self.portfolio_guard.evaluate_entry(
            db,
            signal,
            mode=OrderMode.LIVE.value,
            side=plan.side,
        )
        if not decision.allowed:
            self._record_portfolio_guard_rejection(
                db,
                signal,
                decision,
                mode=OrderMode.LIVE.value,
            )
            db.commit()
            raise PermissionError(decision.reason or "portfolio guard rejected live entry")
        if not self._validate_or_reject(db, signal, plan):
            raise PermissionError(
                "; ".join(
                    self.validator.validate_live_execution(db, signal, self.settings, plan).reasons
                )
            )
        # Pre-flight: se esiste già una posizione HL su questo coin, set_leverage
        # non cambierà il leverage effettivo (HL lo mantiene per posizioni aperte).
        # Meglio saltare il trade ora piuttosto che aprire al leverage sbagliato e
        # innescare il guard failure post-apertura (con fee di apertura già pagata).
        if not is_spot and self.settings.hyperliquid_account_address:
            try:
                hl_state = trading.user_state(self.settings.hyperliquid_account_address)
                existing_hl_pos = _hl_open_position(hl_state, coin)
                if existing_hl_pos is not None:
                    db.add(
                        DecisionLog(
                            event_type="live_order_skipped_hl_position_exists",
                            market_id=signal.market_id,
                            signal_id=signal.id,
                            message=f"Trade skippato: posizione HL esistente su {coin}",
                            data={
                                "coin": coin,
                                "hl_szi": existing_hl_pos.get("szi"),
                                "hl_liq_price": existing_hl_pos.get("liquidationPx"),
                                "target_leverage": plan.max_leverage,
                            },
                        )
                    )
                    db.commit()
                    raise PermissionError(
                        f"existing HL position on {coin}; skipping to avoid leverage mismatch"
                    )
            except PermissionError:
                raise
            except Exception as exc:
                logger.warning("live_preflight_hl_check_failed", coin=coin, error=str(exc))

        # Pre-flight: controlla la depth del book L2 PRIMA di inviare l'ordine.
        # La soglia è l'order_notional effettivo con un buffer del 20% per
        # assorbire variazioni di book nell'intervallo check→invio.
        # Questo previene sia i mercati illiquidi sia le race condition parziali.
        min_notional_dec = Decimal(str(live_settings["min_order_notional_usd"]))
        is_long = plan.side == "long"
        order_notional = order_size * signal.entry_price
        depth_threshold = max(min_notional_dec, order_notional * Decimal("1.2"))
        max_slippage_pct = float(live_settings["max_slippage_pct"])
        depth_estimate: Any | None = None
        try:
            available_fill_depth = getattr(trading, "available_fill_depth", None)
            if callable(available_fill_depth):
                depth_estimate = available_fill_depth(
                    coin=coin,
                    is_buy=is_long,
                    slippage_pct=max_slippage_pct,
                    mid_price=float(signal.entry_price),
                )
                fillable = depth_estimate.fillable_notional
            else:
                fillable = trading.available_fill_notional(
                    coin=coin,
                    is_buy=is_long,
                    slippage_pct=max_slippage_pct,
                    mid_price=float(signal.entry_price),
                )
        except Exception as exc:
            db.add(
                DecisionLog(
                    event_type="live_order_skipped_depth_unavailable",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=f"Trade skippato: depth book non disponibile per {coin}",
                    data={"coin": coin, "error": str(exc), "is_long": is_long},
                )
            )
            db.commit()
            raise PermissionError(f"book depth unavailable for {coin}") from exc
        depth_log_data = {
            "coin": coin,
            "fillable_notional": str(fillable),
            "order_notional": str(order_notional),
            "depth_threshold": str(depth_threshold),
            "min_notional": str(min_notional_dec),
            "is_long": is_long,
            "reference_price": str(signal.entry_price),
            "max_slippage_pct": str(max_slippage_pct),
            **_depth_estimate_data(depth_estimate),
        }
        if not fillable.is_finite():
            db.add(
                DecisionLog(
                    event_type="live_order_skipped_depth_unavailable",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=f"Trade skippato: depth book non disponibile per {coin}",
                    data=depth_log_data,
                )
            )
            db.commit()
            raise PermissionError(f"book depth unavailable for {coin}")
        if fillable < depth_threshold:
            price_outside_slippage = fillable == 0 and _depth_price_outside_slippage(
                depth_estimate,
                is_long=is_long,
            )
            if price_outside_slippage:
                assert depth_estimate is not None
                side_label = "best ask" if is_long else "best bid"
                comparison = ">" if is_long else "<"
                side_price = depth_estimate.best_ask if is_long else depth_estimate.best_bid
                limit_px = depth_estimate.limit_px
                event_type = "live_order_skipped_price_outside_slippage"
                message = (
                    f"Trade skippato: prezzo book fuori slippage per {coin} "
                    f"({side_label} {side_price} {comparison} limite {limit_px})"
                )
                permission_message = (
                    f"price outside slippage for {coin}: {side_label} {side_price} "
                    f"{comparison} limit {limit_px}"
                )
            else:
                event_type = "live_order_skipped_insufficient_depth"
                message = (
                    f"Trade skippato: depth book insufficiente per {coin} "
                    f"(fillable ${float(fillable):.2f} < soglia ${float(depth_threshold):.2f})"
                )
                permission_message = (
                    f"insufficient book depth for {coin}: fillable ${float(fillable):.2f} "
                    f"< threshold ${float(depth_threshold):.2f}"
                )
            db.add(
                DecisionLog(
                    event_type=event_type,
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=message,
                    data=depth_log_data,
                )
            )
            db.commit()
            raise PermissionError(permission_message)

        if is_spot:
            safe_leverage = 1
        else:
            hl_max_lev = (market.metadata_json or {}).get("raw", {}).get("maxLeverage")
            safe_leverage = (
                min(plan.max_leverage, int(hl_max_lev))
                if hl_max_lev is not None
                else plan.max_leverage
            )
            _FALLBACK_LEVERAGE = 3
            try:
                trading.set_leverage(coin, safe_leverage, is_cross=False)
            except Exception as exc:
                # Il fallback non deve mai AUMENTARE il leverage oltre quello inteso:
                # se safe_leverage < _FALLBACK_LEVERAGE usiamo il valore più conservativo.
                fallback_leverage = min(safe_leverage, _FALLBACK_LEVERAGE)
                retry_fallback = fallback_leverage < safe_leverage and not isinstance(exc, HyperliquidTransientError)
                db.add(
                    DecisionLog(
                        event_type="live_set_leverage_failed",
                        market_id=signal.market_id,
                        signal_id=signal.id,
                        message=(
                            f"set_leverage({safe_leverage}) failed, retrying with fallback {fallback_leverage}x"
                            if retry_fallback
                            else f"set_leverage({safe_leverage}) failed before live submit"
                        ),
                        data={
                            "coin": coin,
                            "leverage": safe_leverage,
                            "fallback_leverage": fallback_leverage,
                            "is_cross": False,
                            "retry_fallback": retry_fallback,
                            "error": str(exc),
                        },
                    )
                )
                db.commit()
                if not retry_fallback:
                    raise
                trading.set_leverage(coin, fallback_leverage, is_cross=False)
                safe_leverage = fallback_leverage

        # Re-check dei guardrail runtime appena prima del submit: kill switch,
        # operation mode e flag di live trading possono essere stati cambiati a
        # runtime durante i pre-flight di rete (finestra tra la validazione iniziale
        # e l'invio dell'ordine).
        guard_reason = _live_guard_block_reason(db, self.settings, signal=signal)
        if guard_reason is not None:
            db.add(
                DecisionLog(
                    event_type="live_order_aborted_kill_switch",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=f"Live order abortito prima del submit: {guard_reason}",
                    data={"reason": guard_reason, "coin": coin},
                )
            )
            db.commit()
            raise PermissionError(f"live order aborted before submit: {guard_reason}")
        entry_result = trading.submit_market_order(
            coin=coin,
            is_buy=is_long,
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
        # Post-fill safety net: se nonostante il pre-flight check HL ha
        # restituito un partial fill sotto il minimo (race condition, book
        # cambiato nel frattempo), tracciamo la posizione con tiny_fill=True.
        # Non la chiudiamo subito: una chiusura immediata cristallizza solo fee e
        # slippage. La size reale viene persistita e gestita dagli ordini di
        # protezione quando piazzabili.
        actual_filled = entry_result.filled_size or order_size
        actual_price = entry_result.avg_price or signal.entry_price
        actual_notional = actual_filled * actual_price
        _post_fill_tiny = actual_notional < min_notional_dec
        position = self._persist_live_position(
            db,
            signal,
            plan,
            entry_result,
            order_size,
            account_value,
            market_cloid,
            safe_leverage,
            instrument_type="spot" if is_spot else "perp",
        )
        if _post_fill_tiny:
            meta = dict(position.metadata_json or {})
            meta["tiny_fill"] = True
            meta["tiny_fill_notional"] = str(actual_notional)
            meta["tiny_fill_min_notional"] = str(min_notional_dec)
            position.metadata_json = meta
            db.add(
                DecisionLog(
                    event_type="live_order_partial_fill_below_min_notional",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=(
                        f"ATTENZIONE: partial fill sotto il minimo nonostante il pre-flight check "
                        f"(race condition) — filled ${float(actual_notional):.4f} < min ${float(min_notional_dec):.2f}. "
                        f"Posizione tracciata con tiny_fill=True e gestita senza chiusura automatica."
                    ),
                    data={
                        "requested_size": str(order_size),
                        "filled_size": str(actual_filled),
                        "avg_price": str(actual_price),
                        "actual_notional": str(actual_notional),
                        "min_notional": str(min_notional_dec),
                        "coin": coin,
                    },
                )
            )
            logger.error(
                "live_order_partial_fill_below_min_notional",
                coin=coin,
                signal_id=signal.id,
                actual_notional=str(actual_notional),
                min_notional=str(min_notional_dec),
            )
        db.commit()
        db.refresh(position)
        if is_spot:
            self._submit_live_spot_stop_loss(db, trading, position, signal, coin, sz_dec)
        else:
            self._submit_live_protection_orders(db, trading, position, signal, coin, sz_dec)
        return position

    def execute_live_addition(
        self,
        db: Session,
        signal: Signal,
        position: Position,
        *,
        max_additions: int = 2,
        max_fraction: Decimal = Decimal("0.30"),
        trading_client: HyperliquidTradingClient | None = None,
    ) -> Position:
        """Add to one tracked The Bull perp without creating a second HL position.

        The original size is immutable for the percentage cap. An addition is
        never bumped to the exchange minimum; if too small, it is skipped.
        """
        if not 0 <= max_additions <= 2 or not Decimal("0") < max_fraction <= Decimal("0.30"):
            raise PermissionError("The Bull addition limits exceed the authorized maximum")
        if position.mode != OrderMode.LIVE.value or position.status != "open":
            raise PermissionError("addition target is not an open live position")
        market = db.get(Market, signal.market_id)
        if market is None or market.market_type != "perp" or position.market_id != market.id:
            raise PermissionError("The Bull additions require the same tracked perpetual market")
        if position.side != "long" or signal.direction not in {Direction.LONG, Direction.STRONG_LONG}:
            raise PermissionError("The Bull additions require an existing long")
        source = (position.metadata_json or {}).get("external_source")
        if not isinstance(source, dict) or source.get("provider") != "the_bull":
            raise PermissionError("The Bull cannot add to a position from another source")
        metadata = dict(position.metadata_json or {})
        additions = metadata.get("the_bull_additions") or []
        if not isinstance(additions, list) or len(additions) >= max_additions:
            raise PermissionError("The Bull addition limit reached")
        if any(
            isinstance(item, dict) and item.get("stop_update") != "protected"
            for item in additions
        ):
            raise PermissionError("a prior addition needs protection or reconciliation review")
        unresolved_addition = (
            db.query(Order)
            .filter(
                Order.position_id == position.id,
                Order.mode == OrderMode.LIVE.value,
                Order.order_type == "market",
                Order.status.in_(("submitting", "submission_unknown", "fill_unknown")),
            )
            .first()
        )
        if unresolved_addition is not None:
            raise PermissionError("a previous market order has an unresolved outcome")
        if position.stop_loss is None:
            raise PermissionError("original position size or protective stop is unavailable")
        try:
            initial_size = Decimal(str(metadata["initial_size"]))
            original_size = Decimal(position.size)
            stop = Decimal(position.stop_loss)
            reference = Decimal(signal.entry_price)
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise PermissionError("original position size or protective stop is unavailable") from exc
        if not all(value.is_finite() for value in (initial_size, original_size, stop, reference)):
            raise PermissionError("non-finite addition price or size")
        if initial_size <= 0 or original_size <= 0 or stop <= 0 or reference <= stop:
            raise PermissionError("addition price is at or below the existing stop")

        guard_reason = _live_guard_block_reason(db, self.settings, signal=signal)
        if guard_reason is not None:
            raise PermissionError(f"live addition blocked: {guard_reason}")
        readiness = evaluate_live_readiness_gate(db, taker_fee_rate=self.settings.live_taker_fee_rate)
        if not bool(readiness["ready"]):
            raise PermissionError("live addition readiness gate failed")
        if _recent_hyperliquid_rate_limit(db) is not None:
            raise PermissionError("Hyperliquid rate-limit cooldown active")
        portfolio = self.portfolio_guard.evaluate_addition(db, signal, position)
        if not portfolio.allowed:
            raise PermissionError(portfolio.reason or "portfolio guard rejected addition")

        trading = trading_client or HyperliquidTradingClient(self.settings)
        coin = coin_for_market(market)
        sz_dec = _sz_decimals(market)
        step = Decimal(10) ** -sz_dec
        capped_size = (initial_size * max_fraction).quantize(step, rounding=ROUND_FLOOR)
        if capped_size <= 0:
            raise PermissionError("30% addition rounds to zero at exchange precision")
        live_settings = effective_live_execution_settings(db, self.settings)
        min_notional = Decimal(str(live_settings["min_order_notional_usd"]))
        max_notional = Decimal(str(live_settings["order_notional_cap_usd"]))
        slippage = Decimal(str(live_settings["max_slippage_pct"])) / Decimal("100")
        worst_entry = reference * (Decimal("1") + slippage)
        capped_size = min(
            capped_size,
            (max_notional / worst_entry).quantize(step, rounding=ROUND_FLOOR),
        )
        notional = capped_size * worst_entry
        if notional < min_notional:
            raise PermissionError(
                f"30% addition below live minimum (${notional:.2f} < ${min_notional:.2f}); not bumped"
            )
        if not callable(getattr(trading, "modify_trigger_order", None)):
            raise PermissionError("exchange stop modification is unavailable")
        account_value, available_margin = trading.account_summary()
        account_value = Decimal(str(account_value))
        available_margin = Decimal(str(available_margin))
        risk_settings = setting_value(db, "risk")
        max_trade_risk = account_value * Decimal(
            str(risk_settings.get("max_risk_per_trade_pct", self.settings.max_risk_per_trade_pct))
        ) / Decimal("100")
        combined_risk = (worst_entry - stop) * capped_size + abs(position.entry_price - stop) * original_size
        if combined_risk > max_trade_risk:
            raise PermissionError("combined position risk exceeds max risk per trade")
        contract_leverage = Decimal(str(metadata.get("contract_leverage") or 0))
        if contract_leverage <= 0 or not contract_leverage.is_finite():
            raise PermissionError("existing contract leverage is unavailable")
        if available_margin < notional / contract_leverage:
            raise PermissionError("available margin is insufficient for the addition")
        plan = LiveOrderPlan(
            side="long",
            size=capped_size,
            notional=notional,
            leverage=notional / account_value if account_value > 0 else Decimal("0"),
            account_value=account_value,
            max_leverage=int(contract_leverage),
            risk_amount=(worst_entry - stop) * capped_size,
            available_margin=available_margin,
        )
        validation = self.validator.validate_live_execution(
            db, signal, self.settings, plan, existing_position=position
        )
        if not validation.ok:
            raise PermissionError("; ".join(validation.reasons))

        remote = _hl_open_position(trading.user_state(self.settings.hyperliquid_account_address), coin)
        if remote is None:
            raise PermissionError("existing Hyperliquid position is missing")
        try:
            remote_size = Decimal(str(remote["szi"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise PermissionError("remote position size is unavailable") from exc
        if remote_size <= 0 or abs(remote_size - original_size) > step:
            raise PermissionError("local and Hyperliquid position sizes differ; reconcile before adding")
        remote_leverage = remote.get("leverage")
        if not isinstance(remote_leverage, dict) or remote_leverage.get("type") != "isolated":
            raise PermissionError("remote isolated leverage could not be verified")
        try:
            leverage_value = Decimal(str(remote_leverage["value"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise PermissionError("remote leverage could not be verified") from exc
        if leverage_value != contract_leverage:
            raise PermissionError("remote leverage differs from the tracked position")

        active_stops = (
            db.query(Order)
            .filter(
                Order.position_id == position.id,
                Order.mode == OrderMode.LIVE.value,
                Order.order_type == "stop_loss",
                Order.status.notin_(("filled", "canceled")),
            )
            .all()
        )
        if len(active_stops) != 1 or active_stops[0].price != stop:
            raise PermissionError("exactly one matching tracked stop is required before adding")
        stop_order = active_stops[0]
        if stop_order.hyperliquid_order_id is None:
            raise PermissionError("tracked stop has no exchange order ID")
        try:
            stop_oid = int(stop_order.hyperliquid_order_id)
        except (TypeError, ValueError) as exc:
            raise PermissionError("tracked stop has no exchange order ID") from exc
        remote_orders = trading.frontend_open_orders(self.settings.hyperliquid_account_address)
        if not any(
            isinstance(item, dict)
            and item.get("coin") == coin
            and str(item.get("oid")) == str(stop_oid)
            for item in remote_orders
        ):
            raise PermissionError("protective stop is not open on Hyperliquid")

        depth = trading.available_fill_depth(
            coin=coin,
            is_buy=True,
            slippage_pct=float(live_settings["max_slippage_pct"]),
            mid_price=float(reference),
        )
        if not depth.fillable_notional.is_finite() or depth.fillable_notional < notional * Decimal("1.2"):
            raise PermissionError("insufficient book depth for the live addition")
        guard_reason = _live_guard_block_reason(db, self.settings, signal=signal)
        if guard_reason is not None:
            raise PermissionError(f"live addition blocked before submit: {guard_reason}")

        add_cloid = make_cloid(signal.id, "add")
        if db.query(Order).filter(Order.cloid == add_cloid).first() is not None:
            raise PermissionError("addition was already submitted or is awaiting reconciliation")
        pending = Order(
            position_id=position.id,
            signal_id=signal.id,
            cloid=add_cloid,
            mode=OrderMode.LIVE.value,
            order_type="market",
            side="buy",
            price=reference,
            size=capped_size,
            status="submitting",
            reduce_only=False,
            raw_response={"the_bull_addition": True},
        )
        db.add(pending)
        db.commit()
        try:
            fill = trading.submit_market_order(
                coin=coin,
                is_buy=True,
                size=capped_size,
                max_slippage_pct=float(live_settings["max_slippage_pct"]),
                expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
                cloid=add_cloid,
            )
        except Exception:
            pending.status = "submission_unknown"
            db.add(pending)
            db.commit()
            raise
        pending.hyperliquid_order_id = fill.oid
        pending.raw_response = {"the_bull_addition": True, "exchange": fill.raw_response}
        if fill.status != "ok" or fill.filled_size is None or fill.filled_size <= 0:
            pending.status = "rejected" if fill.status != "ok" else "fill_unknown"
            db.add(pending)
            db.commit()
            raise PermissionError("addition was rejected or fill size is unknown; manual review required")
        filled_size = Decimal(fill.filled_size)
        fill_price = Decimal(fill.avg_price) if fill.avg_price is not None else reference
        if filled_size > capped_size or filled_size <= 0 or not fill_price.is_finite():
            pending.status = "fill_unknown"
            db.add(pending)
            db.commit()
            raise PermissionError("unexpected addition fill; manual reconciliation required")
        old_entry = Decimal(position.entry_price)
        position.size = original_size + filled_size
        position.entry_price = (old_entry * original_size + fill_price * filled_size) / position.size
        additions.append({
            "signal_id": signal.id,
            "message_id": ((signal.strategy_breakdown or {}).get("_external_source") or {}).get("message_id"),
            "filled_size": str(filled_size),
            "fill_price": str(fill_price),
            "added_at": datetime.now(UTC).isoformat(),
            "stop_update": "pending",
        })
        metadata["the_bull_additions"] = additions
        metadata["remaining_size"] = str(position.size)
        position.metadata_json = metadata
        pending.price = fill_price
        pending.size = filled_size
        pending.fee = fill.fee
        pending.status = "filled"
        signal.status = SignalStatus.EXECUTED.value
        db.add(position)
        db.add(pending)
        db.add(signal)
        db.commit()

        try:
            if fill_price > worst_entry:
                raise PermissionError("addition fill exceeded the validated slippage limit")
            modified = trading.modify_trigger_order(
                oid=stop_oid,
                coin=coin,
                is_buy=False,
                size=position.size,
                trigger_price=stop,
                tpsl="sl",
                expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
                cloid=stop_order.cloid,
            )
            if modified.status != "ok":
                raise HyperliquidError("protective stop resize was rejected")
        except Exception as stop_exc:
            # The original stop still protects the old size. Reduce the newly
            # filled quantity immediately; never leave a deliberate scale-in
            # with only the old-size stop.
            try:
                unwind = trading.submit_market_close(
                    coin=coin,
                    is_buy=False,
                    size=filled_size,
                    max_slippage_pct=float(live_settings["max_slippage_pct"]),
                    expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
                )
                if unwind.status != "ok" or unwind.filled_size != filled_size:
                    raise HyperliquidError("addition emergency unwind not fully filled")
                position.size = original_size
                position.entry_price = old_entry
                unwind_price = Decimal(unwind.avg_price) if unwind.avg_price is not None else fill_price
                realized_delta = (unwind_price - fill_price) * filled_size
                position.realized_pnl = Decimal(position.realized_pnl or 0) + realized_delta
                metadata = dict(position.metadata_json or {})
                metadata["remaining_size"] = str(original_size)
                metadata["realized_pnl"] = str(position.realized_pnl)
                metadata["the_bull_additions"] = [
                    *metadata["the_bull_additions"][:-1],
                    {**metadata["the_bull_additions"][-1], "stop_update": "unwound"},
                ]
                position.metadata_json = metadata
                db.add(position)
                db.add(Order(
                    position_id=position.id,
                    signal_id=signal.id,
                    hyperliquid_order_id=unwind.oid,
                    mode=OrderMode.LIVE.value,
                    order_type="addition_unwind",
                    side="sell",
                    price=unwind_price,
                    size=filled_size,
                    status="filled",
                    reduce_only=True,
                    fee=unwind.fee,
                    raw_response=unwind.raw_response,
                ))
                db.add(DecisionLog(
                    event_type="the_bull_addition_unwound",
                    market_id=position.market_id,
                    signal_id=signal.id,
                    position_id=position.id,
                    message="Addition was reversed because protective stop resize failed",
                    data={"failure_type": type(stop_exc).__name__},
                ))
                db.commit()
            except Exception as unwind_exc:
                metadata = dict(position.metadata_json or {})
                metadata["the_bull_additions"] = [
                    *metadata["the_bull_additions"][:-1],
                    {**metadata["the_bull_additions"][-1], "stop_update": "critical_unprotected"},
                ]
                position.metadata_json = metadata
                db.add(position)
                db.add(DecisionLog(
                    event_type="the_bull_addition_protection_critical",
                    market_id=position.market_id,
                    signal_id=signal.id,
                    position_id=position.id,
                    message="CRITICAL: addition stop resize and emergency unwind both failed",
                    data={
                        "stop_failure_type": type(stop_exc).__name__,
                        "unwind_failure_type": type(unwind_exc).__name__,
                    },
                ))
                db.commit()
                raise PermissionError("addition is unprotected; immediate manual review required") from unwind_exc
            raise PermissionError("addition reversed after protective stop resize failure") from stop_exc

        stop_order.size = position.size
        if modified.oid:
            stop_order.hyperliquid_order_id = modified.oid
        raw_stop = dict(stop_order.raw_response or {})
        raw_stop["the_bull_addition_stop_resize"] = {
            "signal_id": signal.id,
            "size": str(position.size),
            "at": datetime.now(UTC).isoformat(),
        }
        stop_order.raw_response = raw_stop
        metadata = dict(position.metadata_json or {})
        metadata["the_bull_additions"] = [
            *metadata["the_bull_additions"][:-1],
            {**metadata["the_bull_additions"][-1], "stop_update": "protected"},
        ]
        position.metadata_json = metadata
        db.add(stop_order)
        db.add(position)
        db.add(DecisionLog(
            event_type="the_bull_addition_protected",
            market_id=position.market_id,
            signal_id=signal.id,
            position_id=position.id,
            message="The Bull addition filled and protective stop resized",
            data={
                "filled_size": str(filled_size),
                "initial_size": str(initial_size),
                "addition_count": len(additions),
                "position_size": str(position.size),
            },
        ))
        db.commit()
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

    def _record_live_preflight_failure(
        self,
        db: Session,
        signal: Signal,
        *,
        stage: str,
        exc: Exception,
    ) -> None:
        db.add(
            DecisionLog(
                event_type="live_order_preflight_failed",
                market_id=signal.market_id,
                signal_id=signal.id,
                message=f"Live order pre-flight failed before submit: {stage}",
                data={
                    "stage": stage,
                    "error_type": type(exc).__name__,
                    "status_code": _exception_status_code(exc),
                    "error": _safe_error_message(exc),
                },
            )
        )
        db.commit()

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
        contract_leverage: int,
        instrument_type: str = "perp",
    ) -> Position:
        filled_size = entry_result.filled_size or requested_size
        entry_price = entry_result.avg_price or signal.entry_price
        live_base: dict[str, Any] = {
            "source_signal_id": signal.id,
            "source_timeframe": signal.timeframe,
            "initial_size": str(filled_size),
            "remaining_size": str(filled_size),
            "account_value_at_entry": str(account_value),
            "contract_leverage": contract_leverage,
            "entry_response": entry_result.raw_response,
            "instrument_type": instrument_type,
            "take_profit_plan": (
                [] if instrument_type == "spot" else self._build_take_profit_plan(db, signal)
            ),
            "funding_rate": (signal.strategy_breakdown or {}).get("_funding_rate"),
        }
        external_source = _external_source_metadata(signal)
        if external_source is not None:
            live_base["external_source"] = external_source
        breakdown = signal.strategy_breakdown or {}
        experiment_tag = signal_experiment_tag(breakdown)
        if experiment_tag is not None:
            live_base["experiment"] = experiment_tag
        regime_info = breakdown.get("_regime") if isinstance(breakdown, dict) else None
        if isinstance(regime_info, dict):
            live_base["regime_at_open"] = regime_info.get("regime", "unknown")
        live_base["risk_at_open"] = risk_at_open_snapshot(db)
        metadata = self.position_manager.initial_metadata(
            db,
            signal,
            size=filled_size,
            planned_notional=plan.notional,
            base=live_base,
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
        market = db.get(Market, signal.market_id)
        sz_decimals = _sz_decimals(market) if market is not None else 8
        if instrument_type != "spot":
            self._rescale_live_take_profit_plan_min_notional(
                db,
                position,
                signal,
                sz_decimals,
            )
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
                    "instrument_type": instrument_type,
                },
            )
        )
        return position

    def _build_spot_live_order_plan(
        self,
        db: Session,
        signal: Signal,
        account_value: Decimal,
        available_spot_usdc: Decimal,
    ) -> LiveOrderPlan:
        """Size a The Bull spot buy from spendable USDC, never from margin.

        The risk budget and source multiplier remain exactly the same as a
        perpetual entry, but the notional is capped by the current free spot
        balance and leverage is fixed at one.
        """
        risk_settings = db.get(AppSetting, "risk")
        risk_pct = self.settings.max_risk_per_trade_pct
        if risk_settings:
            risk_pct = float(risk_settings.value.get("max_risk_per_trade_pct", risk_pct))
        risk_pct *= self.portfolio_guard.position_size_multiplier(db, signal)
        risk_pct *= _signal_risk_multiplier(signal)
        stop_loss = signal.stop_loss or signal.entry_price
        size = Decimal(
            str(
                calculate_position_size(
                    equity=float(available_spot_usdc),
                    risk_pct=risk_pct,
                    entry_price=float(signal.entry_price),
                    stop_loss=float(stop_loss),
                    leverage=1,
                )
            )
        )
        live_settings = effective_live_execution_settings(db, self.settings)
        configured_cap = Decimal(str(live_settings["order_notional_cap_usd"]))
        notional_cap = min(configured_cap, available_spot_usdc)
        notional_floor = Decimal(str(live_settings["min_order_notional_usd"]))
        if signal.entry_price > 0 and size * signal.entry_price > notional_cap:
            size = notional_cap / signal.entry_price
        notional = min(size * signal.entry_price, notional_cap)
        if (
            live_settings["bump_to_min_notional"]
            and signal.entry_price > 0
            and notional < notional_floor
            and available_spot_usdc >= notional_floor
        ):
            target_notional = min(notional_floor, notional_cap)
            market = db.get(Market, signal.market_id)
            sz_dec = _sz_decimals(market) if market is not None else 8
            quantizer = Decimal(10) ** -sz_dec
            size = (target_notional / signal.entry_price).quantize(
                quantizer, rounding=ROUND_CEILING
            )
            notional = size * signal.entry_price
        leverage = notional / account_value if account_value > 0 else Decimal("0")
        risk_amount = abs(signal.entry_price - stop_loss) * size
        return LiveOrderPlan(
            side="long",
            size=size,
            notional=notional,
            leverage=leverage,
            account_value=account_value,
            max_leverage=1,
            risk_amount=risk_amount,
            available_margin=available_spot_usdc,
        )

    def _rescale_live_take_profit_plan_min_notional(
        self,
        db: Session,
        position: Position,
        signal: Signal,
        sz_decimals: int,
    ) -> None:
        metadata = dict(position.metadata_json or {})
        plan = metadata.get("take_profit_plan")
        if not isinstance(plan, list) or not plan:
            return
        live_settings = effective_live_execution_settings(db, self.settings)
        min_reduce_notional = Decimal(str(live_settings["min_reduce_only_notional_usd"]))
        if min_reduce_notional <= 0:
            return
        initial_size = Decimal(str(metadata.get("initial_size") or position.size))
        original_plan = [dict(item) for item in plan if isinstance(item, dict)]
        rescaled, changes = rescale_take_profit_plan_for_min_notional(
            original_plan,
            initial_size,
            min_reduce_notional,
            sz_decimals,
        )

        if not changes:
            return
        metadata["take_profit_plan"] = rescaled
        metadata["take_profit_plan_min_notional_rescaled"] = True
        metadata["take_profit_plan_min_notional_rescale"] = {
            "min_reduce_only_notional_usd": str(min_reduce_notional),
            "changes": changes,
        }
        position.metadata_json = metadata
        db.add(position)
        db.add(
            DecisionLog(
                event_type="live_take_profit_plan_min_notional_rescaled",
                market_id=signal.market_id,
                signal_id=signal.id,
                position_id=position.id,
                message="Live take-profit plan rescaled for reduce-only minimum notional",
                data={
                    "min_reduce_only_notional_usd": str(min_reduce_notional),
                    "initial_size": str(initial_size),
                    "original_plan": original_plan,
                    "rescaled_plan": rescaled,
                    "changes": changes,
                },
            )
        )

    def _build_live_order_plan(
        self,
        db: Session,
        signal: Signal,
        account_value: Decimal,
        sizing_equity: Decimal | None = None,
    ) -> LiveOrderPlan:
        # sizing_equity = margine disponibile (accountValue - totalMarginUsed).
        # Se non fornito, usa account_value (retrocompatibile con i test).
        equity_for_sizing = sizing_equity if sizing_equity is not None else account_value
        side = "long" if signal.direction in {Direction.LONG, Direction.STRONG_LONG} else "short"
        risk_settings = db.get(AppSetting, "risk")
        risk_pct = self.settings.max_risk_per_trade_pct
        max_leverage = self.settings.max_leverage
        if risk_settings:
            risk_pct = float(risk_settings.value.get("max_risk_per_trade_pct", risk_pct))
            max_leverage = float(risk_settings.value.get("max_leverage", max_leverage))
        risk_pct *= self.portfolio_guard.position_size_multiplier(db, signal)
        risk_pct *= _signal_risk_multiplier(signal)
        stop_loss = signal.stop_loss or signal.entry_price
        size = Decimal(
            str(
                calculate_position_size(
                    equity=float(equity_for_sizing),
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
        notional = min(size * signal.entry_price, notional_cap)
        required_margin_for_bump = notional_floor / Decimal(str(max_leverage))
        can_bump = equity_for_sizing >= required_margin_for_bump
        if (
            live_settings["bump_to_min_notional"]
            and signal.entry_price > 0
            and notional < notional_floor
            and can_bump
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
            available_margin=equity_for_sizing,
        )

    def _record_portfolio_guard_rejection(
        self,
        db: Session,
        signal: Signal,
        decision: PortfolioGuardDecision,
        *,
        mode: str,
    ) -> None:
        db.add(
            DecisionLog(
                event_type=f"{mode}_order_rejected_portfolio_guard",
                market_id=signal.market_id,
                signal_id=signal.id,
                message=f"{mode.capitalize()} entry rejected by portfolio guard",
                data={
                    "reason": decision.reason,
                    "details": decision.details or {},
                },
            )
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
        min_reduce_notional = Decimal(str(live_settings["min_reduce_only_notional_usd"]))
        close_is_buy = position.side.lower() == "short"
        protection_errors: list[str] = []
        submitted_parts: list[str] = []

        guard = self.position_manager.check_liquidation_guard(
            db,
            position,
            coin=coin,
            trading_client=trading,
            source="post_entry",
        )
        if guard.failed:
            guard = self._try_recover_liquidation_guard(
                db,
                trading,
                position,
                signal,
                coin,
                guard,
            )
        if guard.failed:
            self.close_live_position(
                db,
                position,
                reason="liquidation_guard_failed",
                trading_client=trading,
            )
            raise PermissionError(
                "stop loss is not before liquidation price; live position closed immediately"
            )

        # Usa guard.stop_loss che può essere stato aggiustato dalla recovery
        # (es. spostato prima del liq price) invece dello stop originale del segnale.
        effective_sl = guard.stop_loss if guard.stop_loss is not None else signal.stop_loss
        if effective_sl is not None:
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
                    sl_size = _round_size(position.size, sz_decimals)
                    stop_result = trading.submit_trigger_order(
                        coin=coin,
                        is_buy=close_is_buy,
                        size=sl_size,
                        trigger_price=effective_sl,
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
                            price=effective_sl,
                            size=position.size,
                            status=stop_result.status,
                            reduce_only=True,
                            raw_response=stop_result.raw_response,
                        )
                    )
                    submitted_parts.append(f"SL @ {effective_sl} (size {sl_size})")
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
            tp_notional = abs(tp_size * price)
            if min_reduce_notional > 0 and tp_notional < min_reduce_notional:
                skipped_tp_levels.append(
                    {
                        "level": level,
                        "allocation": str(allocation),
                        "size": str(tp_size),
                        "price": str(price),
                        "notional": str(tp_notional),
                        "min_reduce_only_notional": str(min_reduce_notional),
                        "reason": "below_min_reduce_only_notional",
                    }
                )
                metadata = dict(position.metadata_json or {})
                plan = metadata.get("take_profit_plan")
                if isinstance(plan, list):
                    for entry in plan:
                        if isinstance(entry, dict) and entry.get("level") == level:
                            entry["skipped"] = True
                            entry["skip_reason"] = "below_min_reduce_only_notional"
                            entry["notional_at_skip"] = str(tp_notional)
                            entry["min_reduce_only_notional"] = str(min_reduce_notional)
                    position.metadata_json = metadata
                    db.add(position)
                logger.warning(
                    "live_protection_tp_below_min_reduce_only_notional",
                    signal_id=signal.id,
                    position_id=position.id,
                    level=level,
                    notional=str(tp_notional),
                    min_reduce_only_notional=str(min_reduce_notional),
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
                submitted_parts.append(f"{level} @ {price} (size {tp_size})")
            except Exception as exc:
                protection_errors.append(f"{level}: {exc}")

        orders_summary = " | ".join(submitted_parts) if submitted_parts else "none"
        metadata = dict(position.metadata_json or {})
        if skipped_tp_levels:
            metadata["protection_orders_skipped"] = skipped_tp_levels
        if protection_errors:
            metadata["protection_order_errors"] = protection_errors
            event_type = "live_protection_orders_partial_failure"
            message = f"Protection orders partial failure: {orders_summary}"
        elif skipped_tp_levels:
            event_type = "live_protection_orders_partial_skipped"
            message = f"Some TP levels skipped (rounded to zero): {orders_summary}"
        else:
            metadata["protection_orders_submitted"] = True
            event_type = "live_protection_orders_submitted"
            message = f"Protection orders submitted: {orders_summary}"
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

    def _submit_live_spot_stop_loss(
        self,
        db: Session,
        trading: HyperliquidTradingClient,
        position: Position,
        signal: Signal,
        coin: str,
        sz_decimals: int = 8,
    ) -> None:
        """Place the one protective stop supported by the spot fallback.

        Spot has no position-level reduce-only semantics and a full stop plus
        partial TPs could reserve more asset than the wallet owns.  The fallback
        therefore uses one non-reduce-only sell trigger for the exact filled
        size. If it cannot be accepted, the just-opened asset is sold back
        immediately rather than leaving a new unprotected live holding.
        """
        if position.side.lower() != "long" or signal.stop_loss is None:
            raise PermissionError("spot fallback requires a long position with a stop loss")
        live_settings = effective_live_execution_settings(db, self.settings)
        expires_after_seconds = int(live_settings["order_expires_after_seconds"])
        sl_cloid = make_cloid(signal.id, "sl")
        sl_size = _round_size(position.size, sz_decimals)
        try:
            stop_result = trading.submit_trigger_order(
                coin=coin,
                is_buy=False,
                size=sl_size,
                trigger_price=signal.stop_loss,
                tpsl="sl",
                expires_after_seconds=expires_after_seconds,
                cloid=sl_cloid,
                reduce_only=False,
            )
            if stop_result.status != "ok":
                raise HyperliquidError("spot stop loss rejected by Hyperliquid")
        except Exception as stop_exc:
            db.add(
                DecisionLog(
                    event_type="live_spot_stop_loss_failed",
                    market_id=position.market_id,
                    signal_id=signal.id,
                    position_id=position.id,
                    message="Spot protection failed; attempting immediate market exit",
                    data={"coin": coin, "error": _safe_error_message(stop_exc)},
                )
            )
            db.commit()
            try:
                self.close_live_position(
                    db,
                    position,
                    reason="spot_stop_loss_unavailable",
                    trading_client=trading,
                )
            except Exception as close_exc:
                db.add(
                    DecisionLog(
                        event_type="live_spot_emergency_close_failed",
                        market_id=position.market_id,
                        signal_id=signal.id,
                        position_id=position.id,
                        message="CRITICAL: spot protection and emergency close both failed",
                        data={
                            "coin": coin,
                            "stop_error": _safe_error_message(stop_exc),
                            "close_error": _safe_error_message(close_exc),
                        },
                    )
                )
                db.commit()
                raise PermissionError(
                    "spot stop loss and emergency close both failed; manual intervention required"
                ) from close_exc
            raise PermissionError(
                "spot stop loss was unavailable; the new spot position was closed immediately"
            ) from stop_exc

        db.add(
            Order(
                position_id=position.id,
                signal_id=signal.id,
                hyperliquid_order_id=stop_result.oid,
                cloid=sl_cloid,
                mode=OrderMode.LIVE.value,
                order_type="stop_loss",
                side="sell",
                price=signal.stop_loss,
                size=position.size,
                status=stop_result.status,
                reduce_only=False,
                raw_response=stop_result.raw_response,
            )
        )
        metadata = dict(position.metadata_json or {})
        metadata.update(
            {
                "instrument_type": "spot",
                "spot_protection": "single_stop_loss",
                "protection_orders_submitted": True,
                "take_profit_plan": [],
            }
        )
        position.metadata_json = metadata
        db.add(position)
        db.add(
            DecisionLog(
                event_type="live_spot_stop_loss_submitted",
                market_id=position.market_id,
                signal_id=signal.id,
                position_id=position.id,
                message="Spot fallback opened with a single full-size stop loss",
                data={"coin": coin, "stop_loss": str(signal.stop_loss), "size": str(sl_size)},
            )
        )
        db.commit()

    def _try_recover_liquidation_guard(
        self,
        db: Session,
        trading: HyperliquidTradingClient,
        position: Position,
        signal: Signal,
        coin: str,
        guard: LiquidationGuardResult,
    ) -> LiquidationGuardResult:
        """Tenta di recuperare un guard failure in due fasi:
        1. Re-applica il leverage previsto (contract_leverage) — HL a volte non lo aggiorna
           nonostante il set_leverage iniziale abbia risposto OK.
        2. Se il guard non passa ancora, sposta lo stop loss appena prima del liq price
           con un buffer di sicurezza (LIQUIDATION_GUARD_SL_SAFETY_BUFFER).
        """
        metadata = dict(position.metadata_json or {})
        intended_leverage = _metadata_int(metadata.get("contract_leverage"))

        # Fase 1: prova a ri-settare il leverage al valore previsto.
        # Ridurre di pochi step richiede meno margin libero che ridurre a 1x.
        if intended_leverage is not None and intended_leverage >= 1:
            try:
                trading.set_leverage(coin, intended_leverage, is_cross=False)
                recheck = self.position_manager.check_liquidation_guard(
                    db,
                    position,
                    coin=coin,
                    trading_client=trading,
                    source="post_entry_recheck_after_leverage_reapply",
                )
                if not recheck.failed:
                    metadata["liquidation_guard_recovered"] = True
                    position.metadata_json = metadata
                    db.add(position)
                    db.add(
                        DecisionLog(
                            event_type="live_liquidation_guard_recovered",
                            market_id=position.market_id,
                            signal_id=signal.id,
                            position_id=position.id,
                            message="Leverage ri-applicato, guard superato",
                            data={
                                "coin": coin,
                                "intended_leverage": intended_leverage,
                                "stop_loss": str(recheck.stop_loss) if recheck.stop_loss is not None else None,
                                "liquidation_price": str(recheck.liquidation_price)
                                if recheck.liquidation_price is not None
                                else None,
                            },
                        )
                    )
                    return recheck
                # Il leverage è stato ri-settato ma il guard non passa ancora.
                # Usa l'ultimo liq price noto (potenzialmente aggiornato dopo il recheck).
                guard = recheck
            except Exception as exc:
                db.add(
                    DecisionLog(
                        event_type="live_liquidation_guard_recovery_failed",
                        market_id=position.market_id,
                        signal_id=signal.id,
                        position_id=position.id,
                        message="Failed to re-apply leverage after liquidation guard failure",
                        data={
                            "coin": coin,
                            "intended_leverage": intended_leverage,
                            "error": str(exc),
                        },
                    )
                )

        # Fase 2: aggiusta lo stop loss appena prima del liq price.
        liq_price = guard.liquidation_price
        if liq_price is None or liq_price <= 0:
            return guard  # senza liq price non possiamo aggiustare lo stop

        side = position.side.lower()
        entry = position.entry_price
        if side == "long":
            safe_sl = liq_price * (1 + LIQUIDATION_GUARD_SL_SAFETY_BUFFER)
            valid = entry is not None and safe_sl < entry
        elif side == "short":
            safe_sl = liq_price * (1 - LIQUIDATION_GUARD_SL_SAFETY_BUFFER)
            valid = entry is not None and safe_sl > entry
        else:
            return guard

        if not valid:
            # Il liq price è così vicino all'entry che non c'è spazio per uno stop utile.
            return guard

        position.stop_loss = safe_sl
        metadata = dict(position.metadata_json or {})
        metadata["liquidation_guard_sl_adjusted"] = True
        metadata["liquidation_guard_sl_original"] = str(guard.stop_loss) if guard.stop_loss is not None else None
        position.metadata_json = metadata
        db.add(position)
        db.add(
            DecisionLog(
                event_type="live_liquidation_guard_sl_adjusted",
                market_id=position.market_id,
                signal_id=signal.id,
                position_id=position.id,
                message="Stop loss aggiustato prima del liq price",
                data={
                    "coin": coin,
                    "side": side,
                    "original_stop": str(guard.stop_loss) if guard.stop_loss is not None else None,
                    "adjusted_stop": str(safe_sl),
                    "liquidation_price": str(liq_price),
                    "safety_buffer_pct": str(LIQUIDATION_GUARD_SL_SAFETY_BUFFER * 100),
                },
            )
        )
        # Restituisce un guard "ok" con lo stop aggiustato — il chiamante procederà
        # a piazzare il SL usando guard.stop_loss.
        return LiquidationGuardResult(
            status="ok",
            reason="stop_adjusted_before_liquidation",
            liquidation_price=liq_price,
            stop_loss=safe_sl,
            coin=guard.coin,
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

    def apply_time_stops(self, db: Session) -> dict[str, int]:
        config = effective_position_management_settings(db).get("timeStop", {})
        if not bool(config.get("enabled", True)):
            return {"paper_closed": 0, "live_alerted": 0}
        max_age_by_tf = config.get("maxAgeHoursByTimeframe")
        if not isinstance(max_age_by_tf, dict):
            return {"paper_closed": 0, "live_alerted": 0}

        paper_closed = 0
        live_alerted = 0
        if bool(config.get("paperEnabled", True)):
            paper_closed = self._close_paper_time_stops(
                db,
                max_age_by_tf,
                close_losing_positions=bool(config.get("closeLosingPaperPositions", True)),
                close_profitable_positions=bool(
                    config.get("closeProfitablePaperPositions", False)
                ),
                preserve_take_profit_runners=bool(
                    config.get("preserveTakeProfitRunners", True)
                ),
            )
        if str(config.get("liveMode", "alert")) == "alert":
            live_alerted = self._alert_live_time_stops(db, max_age_by_tf)
        if paper_closed or live_alerted:
            db.commit()
        return {"paper_closed": paper_closed, "live_alerted": live_alerted}

    def _close_paper_time_stops(
        self,
        db: Session,
        max_age_by_tf: dict[str, Any],
        *,
        close_losing_positions: bool,
        close_profitable_positions: bool,
        preserve_take_profit_runners: bool,
    ) -> int:
        positions = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.PAPER.value,
                Position.status == "open",
            )
            .all()
        )
        closed = 0
        for position in positions:
            timeframe = self._source_timeframe(db, position)
            max_age_hours = _max_age_hours(max_age_by_tf, timeframe)
            if max_age_hours is None or not _position_age_exceeded(position, max_age_hours):
                continue
            candle = self._latest_candle_after_open(db, position, timeframe)
            if candle is None:
                continue
            pos_metadata = position.metadata_json or {}
            if preserve_take_profit_runners and (
                pos_metadata.get("tp1_hit") or pos_metadata.get("tp2_hit")
            ):
                continue
            in_profit = _position_is_in_profit(position, candle)
            if in_profit and not close_profitable_positions:
                continue
            if not in_profit and not close_losing_positions:
                continue
            self.close_position(
                db,
                position,
                exit_price=candle.close,
                reason="time_stop",
                commit=False,
            )
            interval_ms = TIMEFRAME_MS.get(timeframe)
            if interval_ms is not None:
                position.closed_at = candle.open_time + timedelta(milliseconds=interval_ms)
            metadata = dict(position.metadata_json or {})
            metadata["time_stop_timeframe"] = timeframe
            metadata["time_stop_max_age_hours"] = max_age_hours
            position.metadata_json = metadata
            db.add(position)
            closed += 1
        return closed

    def apply_stale_market_data_guard(self, db: Session) -> dict[str, int]:
        """Close only paper positions that can no longer be managed by candles.

        A fresh mark price is mandatory. Live positions are intentionally out of
        scope: stale live data must never turn into an exchange close based on a
        local fallback price.
        """
        config = effective_position_management_settings(db).get("staleMarketData", {})
        if not bool(config.get("enabled", True)):
            return {"paper_closed": 0, "paper_alerted": 0}
        paper_mode = str(config.get("paperMode", "close")).strip().lower()
        if paper_mode not in {"close", "alert"}:
            return {"paper_closed": 0, "paper_alerted": 0}
        max_age_by_tf = config.get("maxCandleAgeHoursByTimeframe")
        if not isinstance(max_age_by_tf, dict):
            return {"paper_closed": 0, "paper_alerted": 0}
        try:
            fresh_mark_minutes = float(config.get("freshMarkMaxAgeMinutes", 5))
        except (TypeError, ValueError):
            fresh_mark_minutes = 5.0
        try:
            alert_cooldown_minutes = float(config.get("alertCooldownMinutes", 360))
        except (TypeError, ValueError):
            alert_cooldown_minutes = 360.0

        positions = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.PAPER.value,
                Position.status == "open",
            )
            .all()
        )
        now = datetime.now(UTC)
        closed = 0
        alerted = 0
        for position in positions:
            # Mantiene i fake DB dei test fail-closed e rende il guard robusto
            # anche se il query adapter non applica correttamente i filtri.
            if position.mode != OrderMode.PAPER.value or position.status != "open":
                continue
            timeframe = self._source_timeframe(db, position)
            max_age_hours = _max_age_hours(max_age_by_tf, timeframe)
            if max_age_hours is None or not _position_age_exceeded(position, max_age_hours):
                continue
            candle = self._latest_candle(db, position, timeframe)
            if candle is not None and not _candle_age_exceeded(
                candle,
                timeframe,
                max_age_hours,
                now=now,
            ):
                continue

            mark_price, mark_updated_at = _fresh_position_mark(
                position,
                now=now,
                max_age_minutes=fresh_mark_minutes,
            )
            latest_candle_at = candle.open_time.isoformat() if candle is not None else None
            if paper_mode == "close" and mark_price is not None:
                self.close_position(
                    db,
                    position,
                    exit_price=mark_price,
                    reason="stale_market_data",
                    commit=False,
                )
                metadata = dict(position.metadata_json or {})
                metadata.update(
                    {
                        "stale_market_data_timeframe": timeframe,
                        "stale_market_data_latest_candle_at": latest_candle_at,
                        "stale_market_data_max_age_hours": max_age_hours,
                        "stale_market_data_mark_updated_at": (
                            mark_updated_at.isoformat() if mark_updated_at is not None else None
                        ),
                    }
                )
                position.metadata_json = metadata
                db.add(position)
                closed += 1
                continue

            metadata = dict(position.metadata_json or {})
            last_alert = _parse_dt(metadata.get("last_stale_market_data_alert_at"))
            if (
                last_alert is not None
                and now - last_alert < timedelta(minutes=max(alert_cooldown_minutes, 1))
            ):
                continue
            metadata["last_stale_market_data_alert_at"] = now.isoformat()
            metadata["stale_market_data_timeframe"] = timeframe
            metadata["stale_market_data_latest_candle_at"] = latest_candle_at
            metadata["stale_market_data_max_age_hours"] = max_age_hours
            position.metadata_json = metadata
            db.add(position)
            db.add(
                DecisionLog(
                    event_type="paper_stale_market_data_alert",
                    market_id=position.market_id,
                    position_id=position.id,
                    signal_id=_signal_id_from_position(position),
                    message="Paper position cannot be managed with current candle data",
                    data={
                        "timeframe": timeframe,
                        "latest_candle_at": latest_candle_at,
                        "max_age_hours": max_age_hours,
                        "paper_mode": paper_mode,
                        "fresh_mark_available": mark_price is not None,
                    },
                )
            )
            alerted += 1
        if closed or alerted:
            db.commit()
        return {"paper_closed": closed, "paper_alerted": alerted}

    def _alert_live_time_stops(self, db: Session, max_age_by_tf: dict[str, Any]) -> int:
        positions = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.LIVE.value,
                Position.status == "open",
            )
            .all()
        )
        now = datetime.now(UTC)
        alerted = 0
        for position in positions:
            timeframe = self._source_timeframe(db, position)
            max_age_hours = _max_age_hours(max_age_by_tf, timeframe)
            if max_age_hours is None or not _position_age_exceeded(position, max_age_hours):
                continue
            pos_metadata = position.metadata_json or {}
            if pos_metadata.get("tp1_hit") or pos_metadata.get("tp2_hit"):
                continue
            candle = self._latest_candle_after_open(db, position, timeframe)
            if candle is not None and _position_is_in_profit(position, candle):
                continue
            metadata = dict(pos_metadata)
            last_alert = _parse_dt(metadata.get("last_time_stop_alert_at"))
            if last_alert is not None and now - last_alert < timedelta(hours=6):
                continue
            metadata["last_time_stop_alert_at"] = now.isoformat()
            metadata["time_stop_timeframe"] = timeframe
            metadata["time_stop_max_age_hours"] = max_age_hours
            position.metadata_json = metadata
            db.add(position)
            db.add(
                DecisionLog(
                    event_type="live_position_time_stop_alert",
                    market_id=position.market_id,
                    position_id=position.id,
                    signal_id=_signal_id_from_position(position),
                    message="Live position exceeded configured time-stop age",
                    data={
                        "timeframe": timeframe,
                        "max_age_hours": max_age_hours,
                        "opened_at": position.opened_at.isoformat()
                        if position.opened_at is not None
                        else None,
                    },
                )
            )
            alerted += 1
        return alerted

    def _source_timeframe(self, db: Session, position: Position) -> str:
        metadata = position.metadata_json or {}
        source_timeframe = metadata.get("source_timeframe")
        if isinstance(source_timeframe, str) and source_timeframe in TIMEFRAME_MS:
            return source_timeframe
        source_signal_id = metadata.get("source_signal_id", 0)
        try:
            signal_id = int(source_signal_id)
        except (TypeError, ValueError):
            return "1d"
        signal = db.get(Signal, signal_id)
        if signal is not None and signal.timeframe in TIMEFRAME_MS:
            return signal.timeframe
        return "1d"

    def _latest_candle_after_open(
        self,
        db: Session,
        position: Position,
        timeframe: str,
    ) -> Candle | None:
        interval_ms = TIMEFRAME_MS.get(timeframe)
        if interval_ms is None or position.opened_at is None:
            return None
        earliest_open = position.opened_at - timedelta(milliseconds=interval_ms)
        candles = (
            db.query(Candle)
            .filter(
                Candle.market_id == position.market_id,
                Candle.timeframe == timeframe,
                Candle.open_time >= earliest_open,
            )
            .order_by(Candle.open_time.desc())
            .all()
        )
        for candle in candles:
            candle_close = candle.open_time + timedelta(milliseconds=interval_ms)
            if candle_close > position.opened_at:
                return candle
        return None

    def _latest_candle(
        self,
        db: Session,
        position: Position,
        timeframe: str,
    ) -> Candle | None:
        if timeframe not in TIMEFRAME_MS:
            return None
        return (
            db.query(Candle)
            .filter(
                Candle.market_id == position.market_id,
                Candle.timeframe == timeframe,
            )
            .order_by(Candle.open_time.desc())
            .first()
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
        is_spot = market.market_type == "spot"
        close_is_buy = False if is_spot else position.side.lower() == "short"
        trading = trading_client or HyperliquidTradingClient(self.settings)

        # Capture cumulative funding since open before submitting the close.
        # Once closed, the position disappears from user_state so this is the last chance.
        cum_funding_since_open: Decimal | None = None
        if is_spot:
            # A non-reduce-only spot stop reserves the asset. It must be
            # canceled before a source/manual market sell can consume it.
            active_stops = (
                db.query(Order)
                .filter(
                    Order.position_id == position.id,
                    Order.mode == OrderMode.LIVE.value,
                    Order.order_type == "stop_loss",
                    Order.status.notin_(("filled", "canceled")),
                )
                .all()
            )
            for stop_order in active_stops:
                if stop_order.hyperliquid_order_id is None:
                    continue
                try:
                    trading.cancel_order(
                        coin=coin,
                        oid=int(stop_order.hyperliquid_order_id),
                        expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
                    )
                except Exception as exc:
                    db.add(
                        DecisionLog(
                            event_type="live_spot_close_stop_cancel_failed",
                            market_id=position.market_id,
                            position_id=position.id,
                            message="Spot close blocked because its protective stop could not be canceled",
                            data={"coin": coin, "order_id": stop_order.id, "error": _safe_error_message(exc)},
                        )
                    )
                    db.commit()
                    raise PermissionError("spot protective stop could not be canceled") from exc
                stop_order.status = "canceled"
                db.add(stop_order)
            result = trading.submit_market_order(
                coin=coin,
                is_buy=False,
                size=position.size,
                max_slippage_pct=float(live_settings["max_slippage_pct"]),
                expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
            )
        else:
            try:
                state = trading.user_state()
                for item in state.get("assetPositions", []):
                    pos_data = item.get("position") if isinstance(item, dict) else None
                    if isinstance(pos_data, dict) and pos_data.get("coin") == coin:
                        cf = pos_data.get("cumFunding")
                        if isinstance(cf, dict) and cf.get("sinceOpen") is not None:
                            # Negate: HL sinceOpen positive = paid (cost), negative = received (income).
                            # Store as P&L impact (positive = earned, negative = paid).
                            cum_funding_since_open = -Decimal(str(cf["sinceOpen"]))
                        break
            except Exception:
                pass
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
        if cum_funding_since_open is not None:
            position.cum_funding_since_open = cum_funding_since_open
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
                reduce_only=not is_spot,
                fee=result.fee,
                raw_response=result.raw_response,
            )
        )
        if exit_size >= position.size and reason == "liquidation_guard_failed":
            self._close_companion_paper(db, position, exit_price, reason)
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
            position.closed_at = datetime.now(UTC)
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
            metadata["exit_reason_detail"] = self._exit_reason_detail(
                position, metadata, exit_price, reason
            )
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
                    "reason": reason,
                },
            )
        )
        if commit:
            db.commit()

    def _exit_reason_detail(
        self,
        position: Position,
        metadata: dict[str, Any],
        exit_price: Decimal,
        reason: str,
    ) -> str:
        """Etichetta granulare per le chiusure da stop.

        In `exit_reason` ogni stop finisce come "stop_loss", inclusi breakeven e
        trailing in profitto: il breakdown by_exit_reason diventa illeggibile.
        Qui distinguiamo stop iniziale pieno, breakeven, trailing in profitto e
        stop stretto ma ancora in perdita. `exit_reason` resta invariato per
        compatibilità (portfolio guard, decision log, dati storici).
        """
        if reason != "stop_loss":
            return reason
        entry = position.entry_price
        if entry is None or entry <= 0:
            return reason
        side = str(position.side or "").lower()
        if exit_price == entry:
            return "breakeven_stop"
        in_profit = exit_price > entry if side == "long" else exit_price < entry
        if in_profit:
            return "trailing_stop"
        initial_sl_raw = metadata.get("initial_stop_loss")
        if initial_sl_raw is not None:
            try:
                initial_sl = Decimal(str(initial_sl_raw))
            except (InvalidOperation, TypeError, ValueError):
                initial_sl = None
            if initial_sl is not None and exit_price != initial_sl:
                return "tightened_stop"
        return "stop_loss"

    def _exit_event_type(self, reason: str, close_position: bool) -> str:
        if reason == "time_stop":
            return "paper_position_time_stop_closed"
        if reason == "stale_market_data":
            return "paper_position_stale_market_data_closed"
        if reason == "stop_loss":
            return "paper_position_auto_closed"
        if reason in {"take_profit", "tp1", "tp2", "tp3"}:
            return "paper_position_auto_closed" if close_position else "paper_position_partial_closed"
        return "position_closed" if close_position else "position_partially_closed"

    def _close_companion_paper(
        self,
        db: Session,
        live_position: Position,
        exit_price: Decimal,
        reason: str,
    ) -> None:
        signal_id = _signal_id_from_position(live_position)
        if signal_id is None:
            return
        paper = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.PAPER.value,
                Position.status == "open",
            )
            .filter(
                Position.metadata_json["source_signal_id"].as_string() == str(signal_id)
            )
            .first()
        )
        if paper is None:
            return
        self.close_position(db, paper, exit_price=exit_price, reason=reason, commit=False)

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
                protected = self.position_manager.protect_paper_stop_at_breakeven_after_tp1(
                    db, position
                )
                exits += 1 if protected else 0
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
            # Il trailing viene applicato qui solo se questo timeframe e' anche
            # quello di riferimento del trailing (source == reference). Per le
            # posizioni con un timeframe di riferimento piu' fine il trailing e'
            # gestito da manage_paper_trailing_for_timeframe, per evitare doppi
            # aggiornamenti dello stesso stop.
            trail_here = self.position_manager.trailing_timeframe(db, position) == timeframe
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
                candle_key = candle.open_time.isoformat()
                last_checked = (position.metadata_json or {}).get("last_trigger_checked_candle_open_time")
                if last_checked is not None and candle_key <= last_checked:
                    if position.status == "open" and trail_here:
                        self.position_manager.manage_paper_position_for_candle(
                            db, market, position, candles, candle
                        )
                    continue
                exit_count, closed_count = self._apply_paper_triggers_for_candle(db, position, candle)
                exits += exit_count
                closed += closed_count
                if position.status == "open":
                    metadata = dict(position.metadata_json or {})
                    metadata["last_trigger_checked_candle_open_time"] = candle_key
                    position.metadata_json = metadata
                    protected = self.position_manager.protect_paper_stop_at_breakeven_after_tp1(
                        db, position
                    )
                    exits += 1 if protected else 0
                    if trail_here:
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
        skipped: list[str] = []
        for level, field_name, allocation in TAKE_PROFIT_ALLOCATIONS:
            price = getattr(signal, field_name)
            if price is None or price <= 0:
                skipped.append(level)
                continue
            plan.append(
                {
                    "level": level,
                    "price": str(price),
                    "allocation": str(allocation),
                    "filled": False,
                }
            )
        if skipped and plan:
            # Visibilità: se uno dei TP è stato saltato (es. tp3 negativo per short
            # con R stretto), redistribuisco proporzionalmente le allocazioni
            # sui TP rimanenti così l'intera posizione resta gestita.
            total_alloc = sum(Decimal(str(item["allocation"])) for item in plan)
            if total_alloc > 0 and total_alloc < Decimal("1"):
                multiplier = Decimal("1") / total_alloc
                for item in plan:
                    item["allocation"] = str(
                        (Decimal(str(item["allocation"])) * multiplier).quantize(
                            Decimal("0.0001")
                        )
                    )
            db.add(
                DecisionLog(
                    event_type="take_profit_rescaled",
                    signal_id=signal.id,
                    market_id=signal.market_id,
                    message=f"TP riscalato: livelli saltati {skipped}",
                    data={"skipped": skipped, "remaining_levels": [p["level"] for p in plan]},
                )
            )
        return plan

    def _ensure_take_profit_plan(self, db: Session, position: Position) -> None:
        metadata = dict(position.metadata_json or {})
        if not metadata.get("initial_size"):
            metadata["initial_size"] = str(position.size)
        if not metadata.get("remaining_size"):
            metadata["remaining_size"] = str(position.size)
        if metadata.get("take_profit_plan"):
            metadata["take_profit_plan"] = self._sanitize_take_profit_plan(position, metadata["take_profit_plan"])
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
            return self._sanitize_take_profit_plan(position, plan)
        if position.take_profit is None:
            return []
        if position.take_profit <= 0:
            return []
        if position.side.lower() == "long" and position.take_profit <= position.entry_price:
            return []
        if position.side.lower() == "short" and position.take_profit >= position.entry_price:
            return []
        return [
            {
                "level": "take_profit",
                "price": str(position.take_profit),
                "allocation": "1",
                "filled": False,
            }
        ]

    def _sanitize_take_profit_plan(
        self, position: Position, plan: list[Any]
    ) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        side = position.side.lower()
        for item in plan:
            if not isinstance(item, dict):
                continue
            if item.get("runner"):
                sanitized.append(dict(item))
                continue
            try:
                price = Decimal(str(item["price"]))
                allocation = Decimal(str(item["allocation"]))
            except Exception:
                continue
            if price <= 0 or allocation <= 0:
                continue
            if side == "long" and price <= position.entry_price:
                continue
            if side == "short" and price >= position.entry_price:
                continue
            sanitized.append(dict(item))
        return sanitized

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
                break


def _sz_decimals(market: Market) -> int:
    try:
        return int((market.metadata_json or {}).get("raw", {}).get("szDecimals", 8))
    except (TypeError, ValueError):
        return 8


def _round_size(size: Decimal, decimals: int) -> Decimal:
    quantizer = Decimal(10) ** -decimals
    return size.quantize(quantizer)


def _external_source_metadata(signal: Signal) -> dict[str, Any] | None:
    breakdown = signal.strategy_breakdown or {}
    if not isinstance(breakdown, dict):
        return None
    source = breakdown.get("_external_source")
    return dict(source) if isinstance(source, dict) else None


def _is_paper_research_candidate_signal(signal: Signal | None) -> bool:
    if signal is None or not isinstance(signal.strategy_breakdown, dict):
        return False
    candidate = signal.strategy_breakdown.get("_research_candidate")
    return isinstance(candidate, dict) and bool(candidate.get("active"))


def _is_the_bull_live_signal(db: Session, signal: Signal | None) -> bool:
    """Allow only the configured The Bull feed through an entry-only live pause.

    This is deliberately narrow: it never bypasses AUTO_TRADE, the global live
    opt-in, the kill switch, readiness, validation, portfolio controls, or any
    exchange pre-flight.  It only distinguishes a verified The Bull signal from
    a normal signal after the operator has paused generic live entries.
    """
    if signal is None:
        return False
    source = _external_source_metadata(signal)
    if source is None or source.get("provider") != "the_bull":
        return False
    config = setting_value(db, "the_bull")
    channel = _normalized_channel_username(config.get("channel_username"))
    source_channel = _normalized_channel_username(source.get("channel_username"))
    if not channel or source_channel != channel:
        return False
    raw_message_id = source.get("message_id")
    if isinstance(raw_message_id, bool):
        return False
    if raw_message_id is None:
        return False
    try:
        message_id = int(raw_message_id)
    except (TypeError, ValueError):
        return False
    if message_id <= 0:
        return False
    expected_url = f"https://t.me/{channel}/{message_id}"
    return (
        bool(config.get("enabled"))
        and str(config.get("execution_mode")) == "live"
        and source.get("message_url") == expected_url
    )


def _normalized_channel_username(value: object) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _signal_risk_multiplier(signal: Signal) -> float:
    """Allow external sources to reduce, never increase, global trade risk."""
    source = _external_source_metadata(signal)
    if source is None:
        return 1.0
    try:
        multiplier = float(source.get("risk_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, multiplier))


def _target_leverage(signal: Signal, max_leverage: int) -> int:
    """Scale leverage by score, capped by the stop distance for isolated margin."""
    score = float(signal.score or 0)
    factor = score / 100.0
    if signal.direction in {Direction.STRONG_LONG, Direction.STRONG_SHORT}:
        factor = min(1.0, factor * 1.2)
    score_leverage = max(1, min(max_leverage, round(max_leverage * factor)))
    stop_leverage = _stop_distance_leverage_cap(signal)
    if stop_leverage is None:
        return score_leverage
    return max(1, min(score_leverage, stop_leverage))


def _stop_distance_leverage_cap(signal: Signal) -> int | None:
    if signal.entry_price is None or signal.stop_loss is None or signal.entry_price <= 0:
        return None
    entry = Decimal(signal.entry_price)
    stop = Decimal(signal.stop_loss)
    if signal.direction in {Direction.LONG.value, Direction.STRONG_LONG.value} and stop >= entry:
        return None
    if signal.direction in {Direction.SHORT.value, Direction.STRONG_SHORT.value} and stop <= entry:
        return None
    stop_distance_pct = abs(entry - stop) / entry
    if stop_distance_pct <= 0:
        return None
    conservative_distance = stop_distance_pct + LIQUIDATION_STOP_DISTANCE_ABSOLUTE_BUFFER
    if conservative_distance >= 1:
        return 1
    return max(1, int((Decimal("1") / conservative_distance).to_integral_value(rounding=ROUND_FLOOR)))


def _metadata_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _signal_id_from_position(position: Position) -> int | None:
    source_signal_id = (position.metadata_json or {}).get("source_signal_id")
    if source_signal_id is None:
        return None
    try:
        return int(source_signal_id)
    except (TypeError, ValueError):
        return None


def _position_is_in_profit(position: Position, candle: "Candle") -> bool:
    try:
        entry = float(position.entry_price)
        current = float(candle.close)
    except (TypeError, ValueError):
        return False
    side = str(position.side or "").lower()
    if side == "long":
        return current > entry
    if side == "short":
        return current < entry
    return False


def _max_age_hours(max_age_by_tf: dict[str, Any], timeframe: str) -> float | None:
    try:
        value = float(max_age_by_tf[timeframe])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _position_age_exceeded(position: Position, max_age_hours: float) -> bool:
    opened_at = position.opened_at
    if opened_at is None:
        return False
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - opened_at >= timedelta(hours=max_age_hours)


def _candle_age_exceeded(
    candle: Candle,
    timeframe: str,
    max_age_hours: float,
    *,
    now: datetime,
) -> bool:
    interval_ms = TIMEFRAME_MS.get(timeframe)
    if interval_ms is None:
        return True
    candle_open = candle.open_time
    if candle_open.tzinfo is None:
        candle_open = candle_open.replace(tzinfo=UTC)
    candle_close = candle_open + timedelta(milliseconds=interval_ms)
    return now - candle_close >= timedelta(hours=max_age_hours)


def _fresh_position_mark(
    position: Position,
    *,
    now: datetime,
    max_age_minutes: float,
) -> tuple[Decimal | None, datetime | None]:
    metadata = position.metadata_json or {}
    raw_mark = metadata.get("last_mark_price")
    try:
        mark = Decimal(str(raw_mark))
    except (InvalidOperation, TypeError, ValueError):
        return None, None
    if not mark.is_finite() or mark <= 0:
        return None, None
    updated_at = position.pnl_updated_at
    if updated_at is None:
        updated_at = _parse_dt(metadata.get("unrealized_pnl_updated_at"))
    elif updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    if updated_at is None:
        return None, None
    if now - updated_at > timedelta(minutes=max(max_age_minutes, 0)):
        return None, updated_at
    return mark, updated_at


def _parse_dt(value: object | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _hl_open_position(state: dict[str, Any], coin: str) -> dict[str, Any] | None:
    """Ritorna i dati della posizione aperta su HL per `coin`, oppure None.

    Usato nel pre-flight check: se HL mostra già una posizione su questo coin
    con szi != 0, set_leverage non modificherà il leverage effettivo.
    """
    for item in state.get("assetPositions", []):
        if not isinstance(item, dict):
            continue
        pos = item.get("position")
        if not isinstance(pos, dict):
            continue
        if pos.get("coin") != coin:
            continue
        try:
            if Decimal(str(pos.get("szi", "0"))) != 0:
                return pos
        except Exception:
            pass
    return None

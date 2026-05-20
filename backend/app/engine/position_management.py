from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import OperationMode, OrderMode
from app.core.runtime_settings import (
    effective_live_execution_settings,
    effective_live_trading_enabled,
    effective_operation_mode,
    effective_position_management_settings,
)
from app.db.models import AppSetting, Candle, DecisionLog, Market, Order, Position, PositionStopUpdate, Signal
from app.indicators.core import atr, ema
from app.services.candle_sync import TIMEFRAME_MS
from app.services.hyperliquid import HyperliquidTradingClient

logger = structlog.get_logger()

POSITION_DUST_THRESHOLD = Decimal("0.0000000001")
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled"})


@dataclass(frozen=True)
class StopCandidate:
    price: Decimal
    reason: str
    indicators: dict[str, Any]
    r_multiple: Decimal


class PositionManagementEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def settings_snapshot(self, db: Session) -> dict[str, Any]:
        return effective_position_management_settings(db)

    def build_take_profit_plan(self, db: Session, signal: Signal) -> list[dict[str, str | bool]]:
        config = effective_position_management_settings(db)
        tp_config = config.get("takeProfit", {})
        if not bool(tp_config.get("enabled", True)):
            return []
        if signal.stop_loss is None:
            return []
        risk = abs(signal.entry_price - signal.stop_loss)
        if risk <= 0:
            return []
        direction = str(signal.direction)
        long = direction in {"LONG", "STRONG_LONG"}
        levels = (
            ("tp1", _decimal(tp_config.get("tp1R"), Decimal("1")), _pct(tp_config.get("tp1ClosePct"))),
            ("tp2", _decimal(tp_config.get("tp2R"), Decimal("2")), _pct(tp_config.get("tp2ClosePct"))),
        )
        plan: list[dict[str, str | bool]] = []
        for level, r_multiple, allocation in levels:
            if r_multiple is None or r_multiple <= 0 or allocation <= 0:
                continue
            price = signal.entry_price + (risk * r_multiple if long else -(risk * r_multiple))
            if price <= 0:
                continue
            plan.append(
                {
                    "level": level,
                    "price": str(price),
                    "allocation": str(allocation),
                    "filled": False,
                }
            )
        runner_pct = _pct(tp_config.get("runnerPct"))
        if runner_pct > 0:
            plan.append(
                {
                    "level": "runner",
                    "price": "",
                    "allocation": str(runner_pct),
                    "filled": False,
                    "runner": True,
                }
            )
        return plan

    def initial_metadata(
        self,
        db: Session,
        signal: Signal,
        size: Decimal,
        planned_notional: Decimal,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(base)
        config = effective_position_management_settings(db)
        initial_stop = signal.stop_loss
        risk_per_unit = abs(signal.entry_price - initial_stop) if initial_stop is not None else None
        metadata.update(
            {
                "initial_stop_loss": str(initial_stop) if initial_stop is not None else None,
                "initial_risk_per_unit": str(risk_per_unit) if risk_per_unit is not None else None,
                "initial_position_risk": str(risk_per_unit * size)
                if risk_per_unit is not None
                else None,
                "highest_high_since_entry": str(signal.entry_price),
                "lowest_low_since_entry": str(signal.entry_price),
                "current_r_multiple": "0",
                "breakeven_triggered": False,
                "tp1_hit": False,
                "tp2_hit": False,
                "runner_active": False,
                "partial_exit_count": 0,
                "last_stop_update_at": None,
                "last_stop_update_reason": None,
                "last_trailing_candle_open_time": None,
                "position_management_config": config,
                "planned_notional": str(planned_notional),
            }
        )
        return metadata

    def manage_paper_position_for_candle(
        self,
        db: Session,
        market: Market,
        position: Position,
        candles: Sequence[Candle],
        candle: Candle,
    ) -> bool:
        candidate = self.evaluate_stop_candidate(db, market, position, candles, candle)
        if candidate is None:
            return False
        self.apply_stop_update(db, position, candidate, mode=OrderMode.PAPER.value)
        return True

    def manage_live_positions_for_timeframe(
        self,
        db: Session,
        market: Market,
        timeframe: str,
        trading_client: HyperliquidTradingClient | None = None,
    ) -> int:
        if not self._live_updates_allowed(db):
            return 0
        positions = (
            db.query(Position)
            .filter(
                Position.market_id == market.id,
                Position.mode == OrderMode.LIVE.value,
                Position.status == "open",
            )
            .all()
        )
        if not positions:
            return 0
        updated = 0
        for position in positions:
            if not self.position_matches_timeframe(db, position, timeframe):
                continue
            candles = (
                db.query(Candle)
                .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
                .order_by(Candle.open_time.asc())
                .all()
            )
            if not candles:
                continue
            candidate = self.evaluate_stop_candidate(db, market, position, candles, candles[-1])
            if candidate is None:
                continue
            if self.replace_live_stop_order(db, market, position, candidate, trading_client):
                updated += 1
        if updated:
            db.commit()
        return updated

    def evaluate_stop_candidate(
        self,
        db: Session,
        market: Market,
        position: Position,
        candles: Sequence[Candle],
        candle: Candle,
    ) -> StopCandidate | None:
        config = effective_position_management_settings(db)
        trailing = config.get("trailingStop", {})
        if not bool(trailing.get("enabled", True)):
            return None
        if not self._position_update_allowed(db, market, position):
            return None
        if not self.position_matches_timeframe(db, position, candle.timeframe):
            return None
        metadata = self.ensure_position_state(position)
        candle_key = candle.open_time.isoformat()
        if trailing.get("updateOnlyOnCandleClose", True):
            if metadata.get("last_trailing_candle_open_time") == candle_key:
                return None
            interval_ms = TIMEFRAME_MS.get(candle.timeframe)
            if interval_ms is not None:
                expected_close = candle.open_time + timedelta(milliseconds=interval_ms)
                now = datetime.now(timezone.utc)
                if expected_close.tzinfo is None:
                    expected_close = expected_close.replace(tzinfo=timezone.utc)
                if now < expected_close:
                    return None

        risk_per_unit = _decimal(metadata.get("initial_risk_per_unit"), None)
        initial_stop = _decimal(metadata.get("initial_stop_loss"), position.stop_loss)
        if risk_per_unit is None and initial_stop is not None:
            risk_per_unit = abs(position.entry_price - initial_stop)
        if risk_per_unit is None or risk_per_unit <= 0:
            return None

        relevant = [row for row in candles if row.open_time <= candle.open_time]
        if len(relevant) < max(int(trailing.get("atrPeriod", 14)) + 1, 2):
            return None
        highs = [float(row.high) for row in relevant]
        lows = [float(row.low) for row in relevant]
        closes = [float(row.close) for row in relevant]
        atr_period = int(trailing.get("atrPeriod", 14))
        atr_value = atr(highs, lows, closes, atr_period)[-1]
        if atr_value is None or atr_value <= 0:
            return None
        atr_dec = Decimal(str(atr_value))
        ema_period = int(trailing.get("emaPeriod", 21))
        ema_value = ema(closes, ema_period)[-1] if len(closes) >= ema_period else None

        current_r = r_multiple(position, candle.close, risk_per_unit)
        metadata["current_r_multiple"] = str(current_r)
        metadata["highest_high_since_entry"] = str(
            max(_decimal(metadata.get("highest_high_since_entry"), candle.high) or candle.high, candle.high)
        )
        metadata["lowest_low_since_entry"] = str(
            min(_decimal(metadata.get("lowest_low_since_entry"), candle.low) or candle.low, candle.low)
        )
        position.metadata_json = metadata

        side = position.side.lower()
        current_stop = position.stop_loss
        candidates: list[tuple[Decimal, str]] = []
        move_be_at = _decimal(trailing.get("moveToBreakevenAtR"), Decimal("1"))
        if move_be_at is not None and current_r >= move_be_at:
            candidates.append((position.entry_price, "breakeven"))
        lock_profit_at = _decimal(trailing.get("lockProfitAtR"), Decimal("2"))
        lock_profit_stop_r = _decimal(trailing.get("lockProfitStopR"), Decimal("1"))
        if (
            lock_profit_at is not None
            and lock_profit_stop_r is not None
            and current_r >= lock_profit_at
        ):
            locked = (
                position.entry_price + risk_per_unit * lock_profit_stop_r
                if side == "long"
                else position.entry_price - risk_per_unit * lock_profit_stop_r
            )
            candidates.append((locked, "lock_profit"))

        activation = _decimal(trailing.get("activationR"), Decimal("1"))
        if activation is not None and current_r >= activation:
            atr_mult = self._atr_multiplier(trailing, position)
            mode = str(trailing.get("mode", "hybrid"))
            use_chandelier = mode in {"hybrid", "chandelier"} and bool(
                trailing.get("useChandelierExit", True)
            )
            if mode == "atr" or not use_chandelier:
                atr_stop = (
                    candle.close - (atr_dec * atr_mult)
                    if side == "long"
                    else candle.close + (atr_dec * atr_mult)
                )
                candidates.append((atr_stop, "atr_trailing"))
            if use_chandelier:
                high_since = _decimal(metadata.get("highest_high_since_entry"), candle.high) or candle.high
                low_since = _decimal(metadata.get("lowest_low_since_entry"), candle.low) or candle.low
                chandelier = (
                    high_since - (atr_dec * atr_mult)
                    if side == "long"
                    else low_since + (atr_dec * atr_mult)
                )
                candidates.append((chandelier, "chandelier"))
            if mode in {"hybrid", "swing"} and bool(trailing.get("useSwingTrailing", True)):
                lookback = max(1, int(trailing.get("swingLookback", 20)))
                window = relevant[-lookback:]
                buffer_atr = atr_dec * (_decimal(trailing.get("swingBufferAtr"), Decimal("0.5")) or Decimal("0"))
                swing = (
                    min(row.low for row in window) - buffer_atr
                    if side == "long"
                    else max(row.high for row in window) + buffer_atr
                )
                candidates.append((swing, "swing_trailing"))
            if (
                mode in {"hybrid", "ema"}
                and bool(trailing.get("useEmaTrailing", True))
                and ema_value is not None
            ):
                buffer_atr = atr_dec * (_decimal(trailing.get("emaBufferAtr"), Decimal("0.5")) or Decimal("0"))
                ema_dec = Decimal(str(ema_value))
                ema_stop = ema_dec - buffer_atr if side == "long" else ema_dec + buffer_atr
                candidates.append((ema_stop, "ema_trailing"))

        if not candidates:
            metadata["last_trailing_candle_open_time"] = candle_key
            position.metadata_json = metadata
            return None

        if side == "long":
            viable = [(price, reason) for price, reason in candidates if price < candle.close]
            if current_stop is not None:
                viable = [(price, reason) for price, reason in viable if price > current_stop]
            if not viable:
                metadata["last_trailing_candle_open_time"] = candle_key
                position.metadata_json = metadata
                return None
            new_stop, reason = max(viable, key=lambda item: item[0])
        else:
            viable = [(price, reason) for price, reason in candidates if price > candle.close]
            if current_stop is not None:
                viable = [(price, reason) for price, reason in viable if price < current_stop]
            if not viable:
                metadata["last_trailing_candle_open_time"] = candle_key
                position.metadata_json = metadata
                return None
            new_stop, reason = min(viable, key=lambda item: item[0])

        if current_stop is not None and current_stop > 0:
            distance_pct = abs(new_stop - current_stop) / current_stop * Decimal("100")
            min_distance = _decimal(
                trailing.get("minStopUpdateDistancePct"), Decimal("0.15")
            ) or Decimal("0")
            if distance_pct < min_distance:
                metadata["last_trailing_candle_open_time"] = candle_key
                position.metadata_json = metadata
                return None

        metadata["last_trailing_candle_open_time"] = candle_key
        position.metadata_json = metadata
        indicators = {
            "current_price": str(candle.close),
            "atr": str(atr_dec),
            "atr_period": atr_period,
            "ema": str(ema_value) if ema_value is not None else None,
            "ema_period": ema_period,
            "highest_high_since_entry": metadata.get("highest_high_since_entry"),
            "lowest_low_since_entry": metadata.get("lowest_low_since_entry"),
            "candidates": [{"price": str(price), "reason": reason} for price, reason in candidates],
        }
        return StopCandidate(
            price=new_stop,
            reason=reason,
            indicators=indicators,
            r_multiple=current_r,
        )

    def apply_stop_update(
        self,
        db: Session,
        position: Position,
        candidate: StopCandidate,
        mode: str,
        live_order: Order | None = None,
        old_live_order: Order | None = None,
    ) -> None:
        old_stop = position.stop_loss
        position.stop_loss = candidate.price
        metadata = dict(position.metadata_json or {})
        now = datetime.now(timezone.utc).isoformat()
        if candidate.reason == "breakeven" or (
            position.side.lower() == "long"
            and candidate.price >= position.entry_price
        ) or (
            position.side.lower() == "short"
            and candidate.price <= position.entry_price
        ):
            metadata["breakeven_triggered"] = True
        metadata.update(
            {
                "trailing_stop_enabled": True,
                "trailing_stop_price": str(candidate.price),
                "current_r_multiple": str(candidate.r_multiple),
                "last_stop_update_at": now,
                "last_stop_update_reason": candidate.reason,
            }
        )
        if live_order is not None:
            metadata["active_stop_order_id"] = live_order.id
            metadata["active_stop_hyperliquid_order_id"] = live_order.hyperliquid_order_id
        position.metadata_json = metadata
        db.add(position)
        db.add(
            PositionStopUpdate(
                position_id=position.id,
                old_stop_loss=old_stop,
                new_stop_loss=candidate.price,
                current_price=Decimal(str(candidate.indicators.get("current_price", candidate.price))),
                r_multiple=candidate.r_multiple,
                reason=candidate.reason,
                indicators=candidate.indicators,
            )
        )
        old_stop_str = str(old_stop) if old_stop is not None else "none"
        new_stop_str = str(candidate.price)
        db.add(
            DecisionLog(
                event_type="position_stop_updated",
                market_id=position.market_id,
                position_id=position.id,
                message=f"Position stop loss updated - from {old_stop_str} to {new_stop_str}",
                data={
                    "mode": mode,
                    "old_stop_loss": str(old_stop) if old_stop is not None else None,
                    "new_stop_loss": str(candidate.price),
                    "r_multiple": str(candidate.r_multiple),
                    "reason": candidate.reason,
                    "live_order_id": live_order.id if live_order is not None else None,
                    "old_live_order_id": old_live_order.id if old_live_order is not None else None,
                },
            )
        )

    _STUCK_STOP_ALERT_THRESHOLD = 3

    def _record_stop_update_failure(
        self,
        db: Session,
        position: Position,
        *,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        metadata = dict(position.metadata_json or {})
        attempts = int(metadata.get("stop_replacement_attempts", 0)) + 1
        metadata["stop_replacement_attempts"] = attempts
        metadata["last_stop_replacement_failure"] = {
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        position.metadata_json = metadata
        db.add(
            DecisionLog(
                event_type="live_stop_update_submit_failed",
                market_id=position.market_id,
                position_id=position.id,
                message="Live stop update failed; old stop left unchanged",
                data={**payload, "attempts": attempts, "reason": reason},
            )
        )
        if attempts >= self._STUCK_STOP_ALERT_THRESHOLD:
            db.add(
                DecisionLog(
                    event_type="live_stop_update_stuck",
                    market_id=position.market_id,
                    position_id=position.id,
                    message=(
                        f"Stop replacement stuck after {attempts} attempts "
                        f"({reason}); manual intervention needed"
                    ),
                    data={**payload, "attempts": attempts, "reason": reason},
                )
            )

    def replace_live_stop_order(
        self,
        db: Session,
        market: Market,
        position: Position,
        candidate: StopCandidate,
        trading_client: HyperliquidTradingClient | None = None,
    ) -> bool:
        if not self._live_updates_allowed(db) or position.mode != OrderMode.LIVE.value:
            return False
        old_order = self._active_stop_order(db, position)
        live_settings = effective_live_execution_settings(db, self.settings)
        close_is_buy = position.side.lower() == "short"
        trading = trading_client or HyperliquidTradingClient(self.settings)
        coin = _coin_for_market(market)
        sz_decimals = _sz_decimals(market)
        cloid = uuid.uuid4().hex
        try:
            result = trading.submit_trigger_order(
                coin=coin,
                is_buy=close_is_buy,
                size=_round_size(position.size, sz_decimals),
                trigger_price=candidate.price,
                tpsl="sl",
                expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
                cloid=cloid,
            )
        except Exception as exc:
            self._record_stop_update_failure(
                db,
                position,
                reason="submit_exception",
                payload={"error": str(exc), "candidate_stop": str(candidate.price)},
            )
            return False
        if result.status != "ok":
            self._record_stop_update_failure(
                db,
                position,
                reason="submit_rejected",
                payload={
                    "response": result.raw_response,
                    "candidate_stop": str(candidate.price),
                },
            )
            return False
        # Successo: azzeriamo il contatore degli attempt falliti
        metadata = dict(position.metadata_json or {})
        metadata["stop_replacement_attempts"] = 0
        position.metadata_json = metadata

        new_order = Order(
            position_id=position.id,
            signal_id=_signal_id_from_position(position),
            hyperliquid_order_id=result.oid,
            cloid=cloid,
            mode=OrderMode.LIVE.value,
            order_type="stop_loss",
            side="buy" if close_is_buy else "sell",
            price=candidate.price,
            size=position.size,
            status=result.status,
            reduce_only=True,
            raw_response={
                **(result.raw_response or {}),
                "dynamic_stop_update": True,
                "replaces_order_id": old_order.id if old_order is not None else None,
            },
        )
        db.add(new_order)
        db.flush()
        self.apply_stop_update(
            db,
            position,
            candidate,
            mode=OrderMode.LIVE.value,
            live_order=new_order,
            old_live_order=old_order,
        )
        if old_order is not None and old_order.hyperliquid_order_id:
            try:
                trading.cancel_order(
                    coin,
                    int(old_order.hyperliquid_order_id),
                    int(live_settings["order_expires_after_seconds"]),
                )
                old_order.status = "canceled"
                old_raw = dict(old_order.raw_response or {})
                old_raw["replaced_by_order_id"] = new_order.id
                old_order.raw_response = old_raw
                db.add(old_order)
            except Exception as exc:
                db.add(
                    DecisionLog(
                        event_type="live_stop_update_cancel_failed",
                        market_id=position.market_id,
                        position_id=position.id,
                        message="New live stop submitted but old stop cancel failed",
                        data={
                            "error": str(exc),
                            "old_order_id": old_order.id,
                            "new_order_id": new_order.id,
                        },
                    )
                )
        return True

    def ensure_position_state(self, position: Position) -> dict[str, Any]:
        metadata = dict(position.metadata_json or {})
        initial_stop = _decimal(metadata.get("initial_stop_loss"), position.stop_loss)
        if initial_stop is not None:
            metadata["initial_stop_loss"] = str(initial_stop)
        risk_per_unit = _decimal(metadata.get("initial_risk_per_unit"), None)
        if risk_per_unit is None and initial_stop is not None:
            risk_per_unit = abs(position.entry_price - initial_stop)
            metadata["initial_risk_per_unit"] = str(risk_per_unit)
        initial_size = _decimal(metadata.get("initial_size"), position.size) or position.size
        if risk_per_unit is not None:
            metadata.setdefault("initial_position_risk", str(risk_per_unit * initial_size))
        metadata.setdefault("highest_high_since_entry", str(position.entry_price))
        metadata.setdefault("lowest_low_since_entry", str(position.entry_price))
        metadata.setdefault("current_r_multiple", "0")
        metadata.setdefault("breakeven_triggered", False)
        metadata.setdefault("tp1_hit", False)
        metadata.setdefault("tp2_hit", False)
        metadata.setdefault("runner_active", False)
        metadata.setdefault("partial_exit_count", 0)
        metadata.setdefault("last_stop_update_at", None)
        metadata.setdefault("last_stop_update_reason", None)
        metadata.setdefault("last_trailing_candle_open_time", None)
        position.metadata_json = metadata
        return metadata

    def position_matches_timeframe(self, db: Session, position: Position, timeframe: str) -> bool:
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

    def _position_update_allowed(self, db: Session, market: Market, position: Position) -> bool:
        if position.status != "open" or position.size <= POSITION_DUST_THRESHOLD:
            return False
        if not market.active:
            return False
        kill_switch = db.get(AppSetting, "kill_switch")
        if kill_switch is not None and isinstance(kill_switch.value, dict):
            if kill_switch.value.get("enabled"):
                return False
        return True

    def _live_updates_allowed(self, db: Session) -> bool:
        mode = effective_operation_mode(db, self.settings)
        return mode == OperationMode.AUTO_TRADE and effective_live_trading_enabled(db, self.settings)

    def _atr_multiplier(self, trailing: dict[str, Any], position: Position) -> Decimal:
        metadata = position.metadata_json or {}
        if metadata.get("tp2_hit"):
            key = "atrMultiplierAfterTp2"
        elif metadata.get("tp1_hit"):
            key = "atrMultiplierAfterTp1"
        else:
            key = "atrMultiplierInitial"
        return _decimal(trailing.get(key), Decimal("3")) or Decimal("3")

    def _active_stop_order(self, db: Session, position: Position) -> Order | None:
        query = (
            db.query(Order)
            .filter(
                Order.position_id == position.id,
                Order.mode == OrderMode.LIVE.value,
                Order.order_type == "stop_loss",
                Order.status.notin_(TERMINAL_ORDER_STATUSES),
            )
        )
        try:
            query = query.order_by(Order.created_at.desc())
        except AttributeError:
            pass
        return query.first()


def r_multiple(position: Position, current_price: Decimal, initial_risk: Decimal) -> Decimal:
    if initial_risk <= 0:
        return Decimal("0")
    if position.side.lower() == "short":
        return (position.entry_price - current_price) / initial_risk
    return (current_price - position.entry_price) / initial_risk


def _pct(value: object) -> Decimal:
    parsed = _decimal(value, Decimal("0")) or Decimal("0")
    return parsed / Decimal("100")


def _decimal(value: object, default: Decimal | None) -> Decimal | None:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _coin_for_market(market: Market) -> str:
    coin = (market.metadata_json or {}).get("hl_coin") or market.symbol
    if not isinstance(coin, str) or not coin:
        raise ValueError("market has no Hyperliquid coin mapping")
    return coin


def _sz_decimals(market: Market) -> int:
    try:
        return int((market.metadata_json or {}).get("raw", {}).get("szDecimals", 8))
    except (TypeError, ValueError):
        return 8


def _round_size(size: Decimal, decimals: int) -> Decimal:
    return size.quantize(Decimal(10) ** -decimals)


def _signal_id_from_position(position: Position) -> int | None:
    source_signal_id = (position.metadata_json or {}).get("source_signal_id")
    if source_signal_id is None:
        return None
    try:
        return int(source_signal_id)
    except (TypeError, ValueError):
        return None

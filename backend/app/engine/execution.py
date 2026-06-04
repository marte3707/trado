import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction, OrderMode, SignalStatus
from app.core.runtime_settings import (
    effective_live_execution_settings,
    effective_paper_equity,
    effective_position_management_settings,
)
from app.db.models import AppSetting, Candle, DecisionLog, Market, Order, Position, Signal
from app.engine.order_validation import LiveOrderPlan, OrderValidator
from app.engine.position_management import LiquidationGuardResult, PositionManagementEngine
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
# Keep isolated liquidation comfortably beyond the planned stop. Hyperliquid's
# exact liquidation price includes maintenance margin, so a multiplicative
# buffer can still be too permissive for wider stops.
LIQUIDATION_STOP_DISTANCE_ABSOLUTE_BUFFER = Decimal("0.10")
# Buffer applicato allo stop aggiustato rispetto al liq price quando il guard fallisce:
# lo stop viene posizionato a liq_price * (1 ± BUFFER) per restare sul lato sicuro.
LIQUIDATION_GUARD_SL_SAFETY_BUFFER = Decimal("0.03")


class OrderExecutionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.validator = OrderValidator()
        self.position_manager = PositionManagementEngine(settings)

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
        equity = effective_paper_equity(db, self.settings)
        size = calculate_position_size(
            equity=equity,
            risk_pct=max_risk_per_trade_pct,
            entry_price=float(signal.entry_price),
            stop_loss=float(signal.stop_loss or signal.entry_price),
            leverage=max_leverage,
        )
        notional = Decimal(str(size)) * signal.entry_price
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
        leverage = notional / Decimal(str(equity)) if equity > 0 else Decimal("0")
        base_metadata: dict[str, Any] = {
            "source_signal_id": signal.id,
            "source_timeframe": signal.timeframe,
            "initial_size": str(size),
            "remaining_size": str(size),
            "account_value_at_entry": str(equity),
            "take_profit_plan": self._build_take_profit_plan(db, signal),
        }
        # AB tag propagato dal signal_engine: serve all'evaluator dell'auto-tuner
        # per attribuire la posizione all'arm corretto (control vs test).
        breakdown = signal.strategy_breakdown or {}
        ab_tag = breakdown.get("_auto_tuner") if isinstance(breakdown, dict) else None
        if isinstance(ab_tag, dict) and ab_tag.get("arm") in ("control", "test"):
            base_metadata["_auto_tuner"] = {
                "step_key": ab_tag.get("step_key"),
                "arm": ab_tag.get("arm"),
            }
        regime_info = breakdown.get("_regime") if isinstance(breakdown, dict) else None
        if isinstance(regime_info, dict):
            base_metadata["regime_at_open"] = regime_info.get("regime", "unknown")
        metadata = self.position_manager.initial_metadata(
            db,
            signal,
            size=Decimal(str(size)),
            planned_notional=notional,
            base=base_metadata,
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
        account_value, available_margin = trading.account_summary()
        plan = self._build_live_order_plan(db, signal, account_value, sizing_equity=available_margin)
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
        # Pre-flight: se esiste già una posizione HL su questo coin, set_leverage
        # non cambierà il leverage effettivo (HL lo mantiene per posizioni aperte).
        # Meglio saltare il trade ora piuttosto che aprire al leverage sbagliato e
        # innescare il guard failure post-apertura (con fee di apertura già pagata).
        if self.settings.hyperliquid_account_address:
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
        try:
            fillable = trading.available_fill_notional(
                coin=coin,
                is_buy=is_long,
                slippage_pct=float(live_settings["max_slippage_pct"]),
                mid_price=float(signal.entry_price),
            )
            if fillable < depth_threshold:
                db.add(
                    DecisionLog(
                        event_type="live_order_skipped_insufficient_depth",
                        market_id=signal.market_id,
                        signal_id=signal.id,
                        message=(
                            f"Trade skippato: depth book insufficiente per {coin} "
                            f"(fillable ${float(fillable):.2f} < soglia ${float(depth_threshold):.2f})"
                        ),
                        data={
                            "coin": coin,
                            "fillable_notional": str(fillable),
                            "order_notional": str(order_notional),
                            "depth_threshold": str(depth_threshold),
                            "min_notional": str(min_notional_dec),
                            "is_long": is_long,
                        },
                    )
                )
                db.commit()
                raise PermissionError(
                    f"insufficient book depth for {coin}: fillable ${float(fillable):.2f} < threshold ${float(depth_threshold):.2f}"
                )
        except PermissionError:
            raise
        except Exception as exc:
            logger.warning("live_preflight_depth_check_failed", coin=coin, error=str(exc))

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
            db.add(
                DecisionLog(
                    event_type="live_set_leverage_failed",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message=f"set_leverage({safe_leverage}) failed, retrying with fallback {_FALLBACK_LEVERAGE}x",
                    data={"coin": coin, "leverage": safe_leverage, "is_cross": False, "error": str(exc)},
                )
            )
            db.commit()
            trading.set_leverage(coin, _FALLBACK_LEVERAGE, is_cross=False)
            safe_leverage = _FALLBACK_LEVERAGE
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
        # cambiato nel frattempo), tracciamo la posizione nel DB con flag
        # tiny_fill=True per poterla identificare e chiudere manualmente.
        # NON chiudiamo qui per evitare doppia fee — la posizione rimane
        # tracciata per la riconciliazione.
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
                        f"Posizione tracciata con tiny_fill=True, chiudere manualmente."
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
        if not _post_fill_tiny:
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
        contract_leverage: int,
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
            "take_profit_plan": self._build_take_profit_plan(db, signal),
            "funding_rate": (signal.strategy_breakdown or {}).get("_funding_rate"),
        }
        breakdown = signal.strategy_breakdown or {}
        ab_tag = breakdown.get("_auto_tuner") if isinstance(breakdown, dict) else None
        if isinstance(ab_tag, dict) and ab_tag.get("arm") in ("control", "test"):
            live_base["_auto_tuner"] = {
                "step_key": ab_tag.get("step_key"),
                "arm": ab_tag.get("arm"),
            }
        regime_info = breakdown.get("_regime") if isinstance(breakdown, dict) else None
        if isinstance(regime_info, dict):
            live_base["regime_at_open"] = regime_info.get("regime", "unknown")
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
                close_losing_positions=bool(config.get("closeLosingPaperPositions", False)),
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
            if pos_metadata.get("tp1_hit") or pos_metadata.get("tp2_hit"):
                continue
            if _position_is_in_profit(position, candle):
                continue
            if not close_losing_positions:
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

    def _alert_live_time_stops(self, db: Session, max_age_by_tf: dict[str, Any]) -> int:
        positions = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.LIVE.value,
                Position.status == "open",
            )
            .all()
        )
        now = datetime.now(timezone.utc)
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

        # Capture cumulative funding since open before submitting the close.
        # Once closed, the position disappears from user_state so this is the last chance.
        cum_funding_since_open: Decimal | None = None
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
                reduce_only=True,
                fee=result.fee,
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
                    "reason": reason,
                },
            )
        )
        if commit:
            db.commit()

    def _exit_event_type(self, reason: str, close_position: bool) -> str:
        if reason == "time_stop":
            return "paper_position_time_stop_closed"
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
                    if position.status == "open":
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
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - opened_at >= timedelta(hours=max_age_hours)


def _parse_dt(value: object | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
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

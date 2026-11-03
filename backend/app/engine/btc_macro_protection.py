from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction, OrderMode, SignalStatus
from app.db.models import AppSetting, Candle, DecisionLog, Market, Position, Signal
from app.engine.position_management import PositionManagementEngine, StopCandidate
from app.indicators.core import ema
from app.services.kiotaka import KiotakaClient

logger = structlog.get_logger()

_CONFIG_KEY = "btc_macro_protection"


class BtcTrendState(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class BtcMacroProtectionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._position_manager = PositionManagementEngine(settings)
        self._kiotaka = KiotakaClient(settings)

    def run(
        self,
        db: Session,
        executor: Any,
        trading_client: Any = None,
    ) -> int:
        config = self._load_config(db)
        if not config.get("enabled", False):
            return 0

        btc_market = (
            db.query(Market)
            .filter(
                Market.base == "BTC",
                Market.market_type == "perp",
                Market.active.is_(True),
            )
            .first()
        )
        if btc_market is None:
            logger.warning("btc_macro_protection_no_btc_market")
            return 0

        timeframe = str(config.get("timeframe", "5m"))
        lookback = int(config.get("lookback_candles", 6))

        candles = (
            db.query(Candle)
            .filter(Candle.market_id == btc_market.id, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(lookback + 1)
            .all()
        )[::-1]

        if len(candles) < lookback + 1:
            logger.debug(
                "btc_macro_protection_insufficient_candles",
                available=len(candles),
                required=lookback + 1,
            )
            return 0

        current_trend, delta_pct, vol_ratio = self._compute_trend(candles, config)

        # EMA confluence opzionale: il reversal è valido solo se concorde con il trend
        # macro su un timeframe più alto (default: 1h, EMA 200).
        if current_trend != BtcTrendState.NEUTRAL and bool(
            config.get("require_ema_confirmation", False)
        ):
            confirmed = self._check_ema_confluence(db, btc_market, current_trend, config)
            if not confirmed:
                logger.debug(
                    "btc_macro_protection_ema_confirmation_failed",
                    candidate_trend=current_trend.value,
                )
                current_trend = BtcTrendState.NEUTRAL

        # Conferma con flusso reale BTC (kiyotaka): se il CVD reale contraddice il trend
        # candidato, declassa a NEUTRAL. Fail-open: dato assente/errore => non blocca.
        flow_info: dict[str, Any] = {}
        if current_trend != BtcTrendState.NEUTRAL and bool(
            config.get("require_flow_confirmation", False)
        ):
            flow_confirmed, flow_info = self._check_flow_confluence(current_trend, config)
            if not flow_confirmed:
                logger.debug(
                    "btc_macro_protection_flow_confirmation_failed",
                    candidate_trend=current_trend.value,
                    flow=flow_info,
                )
                current_trend = BtcTrendState.NEUTRAL

        # Read last *directional* trend (NEUTRAL non lo sovrascrive). Fallback al
        # vecchio "_last_btc_trend" per backward compatibility con setup esistenti.
        last_directional_str = str(
            config.get("_last_directional_trend")
            or config.get("_last_btc_trend", "NEUTRAL")
        )
        try:
            last_directional = BtcTrendState[last_directional_str]
        except KeyError:
            last_directional = BtcTrendState.NEUTRAL

        is_reversal = (
            last_directional != BtcTrendState.NEUTRAL
            and current_trend != BtcTrendState.NEUTRAL
            and last_directional != current_trend
        )

        # Save observed trend in a dedicated commit. Anche se un rollback successivo
        # dovesse cancellare le modifiche alle posizioni, l'osservazione resta persistente.
        # Il directional viene aggiornato solo quando current_trend non è NEUTRAL.
        next_directional = (
            current_trend if current_trend != BtcTrendState.NEUTRAL else last_directional
        )
        self._save_state(
            db,
            observed=current_trend,
            directional=next_directional,
            delta_pct=delta_pct,
            vol_ratio=vol_ratio,
        )
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("btc_macro_protection_state_persist_failed", error=str(exc))

        if not is_reversal:
            return 0

        db.add(
            DecisionLog(
                event_type="btc_macro_reversal_detected",
                message=f"BTC trend reversed: {last_directional.value} → {current_trend.value}",
                data={
                    "from_trend": last_directional.value,
                    "to_trend": current_trend.value,
                    "delta_pct": round(delta_pct, 4),
                    "vol_ratio": round(vol_ratio, 4),
                    "timeframe": timeframe,
                    "lookback_candles": lookback,
                    "flow": flow_info or None,
                },
            )
        )
        db.commit()

        # In reverse_mode l'engine agisce anche sulle posizioni paper, così la
        # simulazione resta confrontabile con il percorso live. Quando reverse_mode
        # è OFF rimane live-only (comportamento storico close/breakeven invariato).
        reverse_mode_enabled = bool(config.get("reverse_mode_enabled", False))
        if reverse_mode_enabled:
            allowed_modes = [OrderMode.LIVE.value, OrderMode.PAPER.value]
        else:
            allowed_modes = [OrderMode.LIVE.value]
        open_positions = (
            db.query(Position)
            .filter(
                Position.mode.in_(allowed_modes),
                Position.status == "open",
            )
            .all()
        )

        updated = 0
        for position in open_positions:
            try:
                if self._apply_to_position(
                    db, executor, position, current_trend, delta_pct, vol_ratio, config, trading_client
                ):
                    db.commit()
                    updated += 1
            except Exception as exc:
                db.rollback()
                logger.exception(
                    "btc_macro_protection_position_failed",
                    position_id=position.id,
                    error=str(exc),
                )

        logger.info(
            "btc_macro_protection_applied",
            reversal=f"{last_directional.value}→{current_trend.value}",
            positions_updated=updated,
        )
        return updated

    def _load_config(self, db: Session) -> dict[str, Any]:
        setting = db.get(AppSetting, _CONFIG_KEY)
        if setting is None or not isinstance(setting.value, dict):
            return {"enabled": False}
        return dict(setting.value)

    def _save_state(
        self,
        db: Session,
        *,
        observed: BtcTrendState,
        directional: BtcTrendState,
        delta_pct: float,
        vol_ratio: float,
    ) -> None:
        setting = db.get(AppSetting, _CONFIG_KEY)
        if setting is None:
            return
        updated = dict(setting.value or {})
        updated["_last_btc_trend"] = observed.value
        updated["_last_directional_trend"] = directional.value
        updated["_last_delta_pct"] = round(delta_pct, 4)
        updated["_last_vol_ratio"] = round(vol_ratio, 4)
        updated["_last_checked_at"] = datetime.now(UTC).isoformat()
        setting.value = updated
        db.add(setting)

    def _compute_trend(
        self,
        candles: list[Candle],
        config: dict[str, Any],
    ) -> tuple[BtcTrendState, float, float]:
        lookback = int(config.get("lookback_candles", 6))
        pct_threshold = float(config.get("reversal_pct_threshold", 1.5))
        vol_multiplier = float(config.get("volume_multiplier", 1.2))

        window = candles[-(lookback + 1):]
        if len(window) < lookback + 1:
            return BtcTrendState.NEUTRAL, 0.0, 0.0

        old_close = float(window[0].close)
        new_close = float(window[-1].close)
        if old_close <= 0:
            return BtcTrendState.NEUTRAL, 0.0, 0.0

        delta_pct = (new_close - old_close) / old_close * 100.0

        volumes = [float(c.volume) for c in window[:-1]]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0.0
        current_vol = float(window[-1].volume)
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0.0

        if vol_ratio < vol_multiplier:
            return BtcTrendState.NEUTRAL, delta_pct, vol_ratio

        if delta_pct >= pct_threshold:
            return BtcTrendState.BULLISH, delta_pct, vol_ratio
        if delta_pct <= -pct_threshold:
            return BtcTrendState.BEARISH, delta_pct, vol_ratio

        return BtcTrendState.NEUTRAL, delta_pct, vol_ratio

    def _check_ema_confluence(
        self,
        db: Session,
        btc_market: Market,
        candidate: BtcTrendState,
        config: dict[str, Any],
    ) -> bool:
        confirmation_tf = str(config.get("confirmation_timeframe", "1h"))
        ema_period = int(config.get("confirmation_ema_period", 200))
        candles = (
            db.query(Candle)
            .filter(Candle.market_id == btc_market.id, Candle.timeframe == confirmation_tf)
            .order_by(Candle.open_time.desc())
            .limit(ema_period * 3)
            .all()
        )[::-1]
        if len(candles) < ema_period:
            return False
        closes = [float(c.close) for c in candles]
        ema_value = ema(closes, ema_period)[-1]
        if ema_value is None:
            return False
        last_close = closes[-1]
        if candidate == BtcTrendState.BULLISH and last_close > ema_value:
            return True
        return bool(candidate == BtcTrendState.BEARISH and last_close < ema_value)

    def _check_flow_confluence(
        self,
        candidate: BtcTrendState,
        config: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """Conferma il trend candidato col flusso reale BTC (CVD da kiyotaka).

        Blocca (ritorna False) solo quando il CVD reale contraddice nettamente la
        direzione: BULLISH con CVD netto < 0, o BEARISH con CVD netto > 0. Fail-open:
        se il dato non è disponibile (free tier giù, no key, errore) il reversal passa.
        """
        lookback_hours = int(config.get("flow_lookback_hours", 6))
        exchange = str(config.get("flow_exchange", "BINANCE_FUTURES"))
        symbol = str(config.get("flow_symbol", "BTCUSDT"))
        ttl = int(config.get("flow_cache_ttl_seconds", 600))

        snapshot = self._kiotaka.btc_flow(
            lookback_hours=lookback_hours, exchange=exchange, symbol=symbol, ttl_seconds=ttl
        )
        if snapshot is None or snapshot.points < 2:
            # Dato assente o insufficiente: non blocchiamo (degradazione sicura).
            return True, {"flow_available": False}

        info = {
            "flow_available": True,
            "cvd_net": round(snapshot.cvd_net, 2),
            "cvd_direction": snapshot.cvd_direction,
            "oi_change_pct": round(snapshot.oi_change_pct, 3),
            "liq_long": round(snapshot.liq_long, 3),
            "liq_short": round(snapshot.liq_short, 3),
            "lookback_hours": lookback_hours,
        }

        contradicts = (
            (candidate == BtcTrendState.BULLISH and snapshot.cvd_net < 0)
            or (candidate == BtcTrendState.BEARISH and snapshot.cvd_net > 0)
        )
        info["confirmed"] = not contradicts
        return (not contradicts), info

    def _latest_close(self, db: Session, market_id: int) -> Decimal | None:
        row = (
            db.query(Candle)
            .filter(Candle.market_id == market_id)
            .order_by(Candle.open_time.desc())
            .first()
        )
        return row.close if row is not None else None

    def _is_position_in_profit(self, db: Session, position: Position) -> bool:
        """Ricalcola in-line se la posizione è in profitto, evitando di affidarsi al
        `unrealized_pnl` che può essere stale o nullo."""
        last_close = self._latest_close(db, position.market_id)
        if last_close is None or last_close <= 0:
            # Fallback: usa il valore stored (può essere stale ma è l'unica info disponibile)
            return float(position.unrealized_pnl or 0) > 0
        side = position.side.lower()
        if side == "long":
            return last_close > position.entry_price
        return last_close < position.entry_price

    def _apply_to_position(
        self,
        db: Session,
        executor: Any,
        position: Position,
        reversal: BtcTrendState,
        delta_pct: float,
        vol_ratio: float,
        config: dict[str, Any],
        trading_client: Any,
    ) -> bool:
        # Reverse-mode è mutualmente esclusivo con close/breakeven: se attivo,
        # gestisce direttamente la posizione (chiusura + apertura opposta) e non
        # cade nei rami legacy. Le posizioni che non superano i filtri del reverse
        # (no signal flip, cooldown, max/day) restano semplicemente intoccate.
        if bool(config.get("reverse_mode_enabled", False)):
            return self._try_reverse(
                db, executor, position, reversal, delta_pct, vol_ratio, config, trading_client
            )

        side = position.side.lower()
        in_profit = self._is_position_in_profit(db, position)
        loss_exit_enabled = bool(config.get("loss_exit_enabled", True))
        buffer_pct = Decimal(str(config.get("breakeven_buffer_pct", 0.3)))

        # BULLISH reversal → intervieni sulle SHORT (BTC sale = shorts a rischio)
        if reversal == BtcTrendState.BULLISH and side == "short":
            if in_profit:
                return self._set_breakeven_stop(
                    db, position, buffer_pct, reversal, delta_pct, vol_ratio, trading_client, direction="up"
                )
            elif loss_exit_enabled:
                return self._close_for_protection(db, executor, position, trading_client)

        # BEARISH reversal → intervieni sulle LONG (BTC scende = longs a rischio)
        elif reversal == BtcTrendState.BEARISH and side == "long":
            if in_profit:
                return self._set_breakeven_stop(
                    db, position, buffer_pct, reversal, delta_pct, vol_ratio, trading_client, direction="down"
                )
            elif loss_exit_enabled:
                return self._close_for_protection(db, executor, position, trading_client)

        return False

    def _set_breakeven_stop(
        self,
        db: Session,
        position: Position,
        buffer_pct: Decimal,
        reversal: BtcTrendState,
        delta_pct: float,
        vol_ratio: float,
        trading_client: Any,
        direction: str,  # "up" = entry + buffer (short), "down" = entry - buffer (long)
    ) -> bool:
        market = db.get(Market, position.market_id)
        if market is None:
            return False

        entry = position.entry_price
        buffer = entry * buffer_pct / Decimal("100")

        if direction == "up":
            new_sl = entry + buffer
            current_sl = position.stop_loss
            # Per short: SL più basso = più protettivo. Applica solo se migliora.
            if current_sl is not None and new_sl >= current_sl:
                logger.debug(
                    "btc_macro_protection_breakeven_skipped",
                    position_id=position.id,
                    reason="trailing_already_better",
                    current_sl=str(current_sl),
                    new_sl=str(new_sl),
                )
                return False
        else:
            new_sl = entry - buffer
            current_sl = position.stop_loss
            # Per long: SL più alto = più protettivo. Applica solo se migliora.
            if current_sl is not None and new_sl <= current_sl:
                logger.debug(
                    "btc_macro_protection_breakeven_skipped",
                    position_id=position.id,
                    reason="trailing_already_better",
                    current_sl=str(current_sl),
                    new_sl=str(new_sl),
                )
                return False

        candidate = StopCandidate(
            price=new_sl,
            reason="btc_macro_protection_breakeven",
            indicators={
                "btc_reversal": reversal.value,
                "btc_delta_pct": round(delta_pct, 4),
                "vol_ratio": round(vol_ratio, 4),
                "buffer_pct": str(buffer_pct),
                "entry_price": str(entry),
            },
            r_multiple=Decimal("0"),
        )

        return self._position_manager.replace_live_stop_order(
            db, market, position, candidate, trading_client
        )

    def _close_for_protection(
        self,
        db: Session,
        executor: Any,
        position: Position,
        trading_client: Any,
    ) -> bool:
        try:
            executor.close_live_position(
                db,
                position,
                reason="btc_macro_protection_loss_exit",
                trading_client=trading_client,
            )
            return True
        except Exception as exc:
            db.add(
                DecisionLog(
                    event_type="btc_macro_protection_close_failed",
                    market_id=position.market_id,
                    position_id=position.id,
                    message="BTC macro protection: close failed",
                    data={"error": str(exc)},
                )
            )
            db.flush()
            return False

    # ------------------------------------------------------------------
    # Auto-reverse mode
    # ------------------------------------------------------------------

    _LONG_DIRECTIONS = {Direction.LONG.value, Direction.STRONG_LONG.value}
    _SHORT_DIRECTIONS = {Direction.SHORT.value, Direction.STRONG_SHORT.value}

    def _try_reverse(
        self,
        db: Session,
        executor: Any,
        position: Position,
        reversal: BtcTrendState,
        delta_pct: float,
        vol_ratio: float,
        config: dict[str, Any],
        trading_client: Any,
    ) -> bool:
        side = position.side.lower()
        # 1. Direzione target = direzione del nuovo regime BTC. Deve essere
        # opposta alla posizione corrente, altrimenti la posizione è già allineata
        # al nuovo regime e non va invertita.
        if reversal == BtcTrendState.BULLISH and side == "short":
            target_side = "long"
            target_directions = self._LONG_DIRECTIONS
        elif reversal == BtcTrendState.BEARISH and side == "long":
            target_side = "short"
            target_directions = self._SHORT_DIRECTIONS
        else:
            return False

        # 2. Cooldown e max/day per market.
        history = self._reverse_history_for_market(config, position.market_id)
        now = datetime.now(UTC)
        cooldown_minutes = int(config.get("reverse_cooldown_minutes", 240))
        max_per_day = int(config.get("reverse_max_per_day", 3))
        if history:
            last_at = self._parse_iso(history[-1].get("reversed_at"))
            if last_at is not None and (now - last_at) < timedelta(minutes=cooldown_minutes):
                db.add(
                    DecisionLog(
                        event_type="btc_auto_reverse_skipped_cooldown",
                        market_id=position.market_id,
                        position_id=position.id,
                        message="Auto-reverse skipped: cooldown active",
                        data={
                            "cooldown_minutes": cooldown_minutes,
                            "last_reversed_at": history[-1].get("reversed_at"),
                        },
                    )
                )
                db.flush()
                return False
        last_24h = [
            entry for entry in history
            if (parsed := self._parse_iso(entry.get("reversed_at"))) is not None
            and (now - parsed) < timedelta(hours=24)
        ]
        if len(last_24h) >= max_per_day:
            db.add(
                DecisionLog(
                    event_type="btc_auto_reverse_skipped_max_per_day",
                    market_id=position.market_id,
                    position_id=position.id,
                    message="Auto-reverse skipped: daily cap reached",
                    data={"max_per_day": max_per_day, "count_24h": len(last_24h)},
                )
            )
            db.flush()
            return False

        # 3. Cerca un signal flip recente sullo stesso market nella direzione target.
        max_age_minutes = int(config.get("reverse_signal_flip_max_age_minutes", 60))
        min_score = int(config.get("reverse_signal_flip_min_score", 50))
        cutoff = now - timedelta(minutes=max_age_minutes)
        flip_signal = (
            db.query(Signal)
            .filter(
                Signal.market_id == position.market_id,
                Signal.direction.in_(list(target_directions)),
                Signal.score >= min_score,
                Signal.created_at >= cutoff,
                Signal.status.notin_([SignalStatus.IGNORED.value, SignalStatus.INVALIDATED.value]),
            )
            .order_by(Signal.score.desc(), Signal.created_at.desc())
            .first()
        )
        if flip_signal is None:
            db.add(
                DecisionLog(
                    event_type="btc_auto_reverse_skipped_no_signal_flip",
                    market_id=position.market_id,
                    position_id=position.id,
                    message="Auto-reverse skipped: no recent opposite signal",
                    data={
                        "target_side": target_side,
                        "min_score": min_score,
                        "max_age_minutes": max_age_minutes,
                    },
                )
            )
            db.flush()
            return False

        # 4. Chiudi la posizione obsoleta (paper o live).
        try:
            if position.mode == OrderMode.PAPER.value:
                self._close_paper_for_reverse(db, executor, position)
            else:
                executor.close_live_position(
                    db,
                    position,
                    reason="btc_auto_reverse",
                    trading_client=trading_client,
                )
        except Exception as exc:
            db.add(
                DecisionLog(
                    event_type="btc_auto_reverse_close_failed",
                    market_id=position.market_id,
                    position_id=position.id,
                    message="Auto-reverse: close failed",
                    data={"error": str(exc), "mode": position.mode},
                )
            )
            db.flush()
            return False

        # 5. Apri la nuova posizione nella direzione opposta via flusso standard
        # (RiskManager ricalcola size/SL/TP dal contesto attuale del signal flip).
        new_position = None
        try:
            if position.mode == OrderMode.PAPER.value:
                new_position = executor.execute_paper(db, flip_signal)
            else:
                new_position = executor.execute_live(
                    db, flip_signal, trading_client=trading_client
                )
        except Exception as exc:
            db.add(
                DecisionLog(
                    event_type="btc_auto_reverse_open_failed",
                    market_id=position.market_id,
                    signal_id=flip_signal.id,
                    message="Auto-reverse: open failed",
                    data={"error": str(exc), "mode": position.mode},
                )
            )
            db.flush()
            return False

        # 6. Registra l'evento e aggiorna l'history per cooldown/max-per-day.
        self._append_reverse_history(
            db,
            market_id=position.market_id,
            reversed_at=now,
            from_side=side,
            to_side=target_side,
        )
        db.add(
            DecisionLog(
                event_type="btc_auto_reverse_triggered",
                market_id=position.market_id,
                signal_id=flip_signal.id,
                position_id=new_position.id if new_position is not None else None,
                message=f"Auto-reverse {side}→{target_side} on BTC {reversal.value}",
                data={
                    "from_side": side,
                    "to_side": target_side,
                    "closed_position_id": position.id,
                    "new_position_id": new_position.id if new_position is not None else None,
                    "flip_signal_id": flip_signal.id,
                    "flip_signal_score": flip_signal.score,
                    "btc_reversal": reversal.value,
                    "btc_delta_pct": round(delta_pct, 4),
                    "vol_ratio": round(vol_ratio, 4),
                    "mode": position.mode,
                },
            )
        )
        db.flush()
        return True

    def _close_paper_for_reverse(
        self,
        db: Session,
        executor: Any,
        position: Position,
    ) -> None:
        """Chiude una posizione paper usando l'ultimo close disponibile come
        exit price, in modo che il P&L realizzato rifletta la situazione di
        mercato al momento del reverse (non l'entry, che azzererebbe il PnL)."""
        last_close = self._latest_close(db, position.market_id)
        exit_price = last_close if last_close is not None and last_close > 0 else position.entry_price
        executor.close_position(
            db,
            position,
            exit_price=exit_price,
            reason="btc_auto_reverse",
            commit=False,
        )

    def _reverse_history_for_market(
        self, config: dict[str, Any], market_id: int
    ) -> list[dict[str, Any]]:
        raw = config.get("_reverse_history")
        if not isinstance(raw, dict):
            return []
        entries = raw.get(str(market_id))
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _append_reverse_history(
        self,
        db: Session,
        *,
        market_id: int,
        reversed_at: datetime,
        from_side: str,
        to_side: str,
    ) -> None:
        setting = db.get(AppSetting, _CONFIG_KEY)
        if setting is None:
            return
        updated = dict(setting.value or {})
        raw = updated.get("_reverse_history")
        history: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        market_key = str(market_id)
        existing = list(history.get(market_key) or [])
        existing.append(
            {
                "reversed_at": reversed_at.isoformat(),
                "from_side": from_side,
                "to_side": to_side,
            }
        )
        # Trim: tieni solo le ultime 50 entry per market per non gonfiare il JSON.
        history[market_key] = existing[-50:]
        updated["_reverse_history"] = history
        setting.value = updated
        db.add(setting)

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed


def _decimal(value: object, default: Decimal | None) -> Decimal | None:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default

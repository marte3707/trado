from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import OrderMode
from app.db.models import AppSetting, Candle, DecisionLog, Market, Position
from app.engine.position_management import PositionManagementEngine, StopCandidate

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
            .limit(lookback + 20)
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

        last_trend_str = str(config.get("_last_btc_trend", "NEUTRAL"))
        try:
            last_trend = BtcTrendState[last_trend_str]
        except KeyError:
            last_trend = BtcTrendState.NEUTRAL

        # Salva sempre lo stato corrente (anche se non è un reversal)
        self._save_state(db, config, current_trend, delta_pct, vol_ratio)

        is_reversal = (
            last_trend != BtcTrendState.NEUTRAL
            and current_trend != BtcTrendState.NEUTRAL
            and last_trend != current_trend
        )

        if not is_reversal:
            return 0

        db.add(
            DecisionLog(
                event_type="btc_macro_reversal_detected",
                message=f"BTC trend reversed: {last_trend.value} → {current_trend.value}",
                data={
                    "from_trend": last_trend.value,
                    "to_trend": current_trend.value,
                    "delta_pct": round(delta_pct, 4),
                    "vol_ratio": round(vol_ratio, 4),
                    "timeframe": timeframe,
                    "lookback_candles": lookback,
                },
            )
        )

        open_positions = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.LIVE.value,
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
                    updated += 1
            except Exception as exc:
                db.rollback()
                logger.exception(
                    "btc_macro_protection_position_failed",
                    position_id=position.id,
                    error=str(exc),
                )

        if updated:
            db.commit()

        logger.info(
            "btc_macro_protection_applied",
            reversal=f"{last_trend.value}→{current_trend.value}",
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
        config: dict[str, Any],
        trend: BtcTrendState,
        delta_pct: float,
        vol_ratio: float,
    ) -> None:
        setting = db.get(AppSetting, _CONFIG_KEY)
        if setting is None:
            return
        updated = dict(setting.value or {})
        updated["_last_btc_trend"] = trend.value
        updated["_last_delta_pct"] = round(delta_pct, 4)
        updated["_last_vol_ratio"] = round(vol_ratio, 4)
        updated["_last_checked_at"] = datetime.now(timezone.utc).isoformat()
        setting.value = updated
        db.add(setting)
        db.flush()

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
        side = position.side.lower()
        unrealized = float(position.unrealized_pnl or 0)
        in_profit = unrealized > 0
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


def _decimal(value: object, default: Decimal | None) -> Decimal | None:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default

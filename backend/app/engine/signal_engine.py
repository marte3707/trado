import asyncio
import hashlib
import time
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import Direction, SignalStatus
from app.core.runtime_settings import (
    effective_min_signal_expected_r,
    effective_strategy_exclusions,
)
from app.db.models import AppSetting, Candle, DecisionLog, Indicator, Market, Signal, StrategyConfig
from app.engine.auto_tuner import active_ab_overlay
from app.engine.edge_profile import signal_threshold_adjustment
from app.engine.macro_direction import btc_macro_direction, suspended_penalty_family
from app.engine.mtf import check_mtf_confluence, higher_timeframe
from app.engine.orderbook import (
    OrderBookAnalysis,
    OrderBookImbalanceStats,
    analyze_order_book,
    ob_imbalance_result,
    update_obi_stats,
)
from app.engine.performance import SignalPerformanceAnalyzer
from app.engine.pre_tp1_validation import (
    cached_pre_tp1_validation_table,
    evaluate_pre_tp1_entry,
)
from app.engine.regime import apply_regime_filter
from app.engine.risk import RiskManager
from app.engine.score_calibration import cached_calibration_table, serialize_metrics
from app.engine.scoring import ScoreResult, SignalScoringEngine
from app.engine.volatility import evaluate_volatility
from app.indicators.core import ema
from app.indicators.volume_profile import VolumeProfileResult
from app.services.coinmarketcap import (
    AltcoinSeasonIndex,
    CoinMarketCapClient,
    FearGreedIndex,
    MarketQuote,
)
from app.services.hyperliquid import fetch_l2_book, fetch_l2_book_async, fetch_predicted_fundings
from app.services.kiotaka import KiotakaClient
from app.services.whale_alert import WhaleAlertClient, WhaleFlow
from app.strategies.base import CandleInput, StrategyContext, StrategyResult
from app.strategies.registry import strategy_registry

logger = structlog.get_logger()


# Stop distance minimo scalato per timeframe. Era hardcoded a 2% per tutti i TF,
# rendendo irraggiungibile il RR target su 1m/5m e gonfiando inutilmente il rischio
# per trade. Override possibile via risk.min_stop_distance_pct in AppSetting.
_DEFAULT_MIN_STOP_BY_TF: dict[str, float] = {
    "1m": 0.004,   # 0.4%
    "5m": 0.007,   # 0.7%
    "15m": 0.010,  # 1.0%
    "30m": 0.013,  # 1.3%
    "1h": 0.016,   # 1.6%
    "2h": 0.018,   # 1.8%
    "4h": 0.022,   # 2.2%
    "1d": 0.030,   # 3.0%
}

# Minuti per timeframe: usato per scalare min_average_quote_volume_usd
# proporzionalmente rispetto al riferimento 1h (60 min).
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}

_VOLATILITY_SHADOW_EVENT = "volatility_filter_shadow_sample"


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def apply_timeframe_min_score_override(
    risk_value: dict[str, Any], timeframe: str, base_min_score: int
) -> int:
    """Soglia min score per-timeframe da `risk.timeframe_min_score_overrides`.

    L'override può solo alzare la soglia rispetto al `min_signal_score` globale,
    mai abbassarla: serve a stringere timeframe con edge negativo documentato
    (es. 5m) senza penalizzare gli altri.
    """
    overrides = risk_value.get("timeframe_min_score_overrides")
    if not isinstance(overrides, dict):
        return base_min_score
    raw = overrides.get(timeframe)
    if raw is None:
        return base_min_score
    try:
        override = int(raw)
    except (TypeError, ValueError):
        return base_min_score
    return max(base_min_score, override)


class SignalEngine:
    def __init__(self) -> None:
        self.registry = strategy_registry()
        self.scoring = SignalScoringEngine()
        self.risk = RiskManager()
        self.performance = SignalPerformanceAnalyzer()
        settings = get_settings()
        self.market_data = CoinMarketCapClient(settings)
        self.whales = WhaleAlertClient(settings)
        self._kiotaka = KiotakaClient(settings)
        # Cache valida per la durata di un worker tick: il worker chiama
        # `begin_cycle()` ad ogni nuovo tick per invalidare. Evita chiamate HTTP
        # ripetute (fundings/orderbook) per market × timeframe nello stesso tick.
        self._cycle_fundings: dict[str, float] | None = None
        self._cycle_l2_cache: dict[str, Any] = {}
        # Aggregated skip-reason counter per cycle. In debug-log mode i singoli
        # "signal_skipped_*" non si vedono in produzione; questo conta dove muoiono
        # i segnali per esporre un breakdown nel `worker_cycle` event.
        self._cycle_skip_counts: dict[str, int] = {}
        # Cache del funding breadth per ciclo: frazione di mercati con funding
        # estremo nella direzione short/long. Calcolata una sola volta per tick.
        self._cycle_funding_breadth: dict[str, float] | None = None
        self._cycle_btc_volume_profile_loaded = False
        self._cycle_btc_volume_profile_context: dict[str, Any] | None = None
        self._cycle_macro_direction: dict[str, Any] | None = None
        # Cull permanente: strategie escluse dalla generazione segnali. Letto una
        # volta per ciclo (lista globale, non per market×timeframe).
        self._cycle_strategy_exclusions: set[str] | None = None
        self._cycle_volatility_shadow_samples = 0

    def begin_cycle(self) -> None:
        """Invalidate per-cycle caches at the start of each worker tick."""
        self._cycle_fundings = None
        self._cycle_l2_cache = {}
        self._cycle_skip_counts = {}
        self._cycle_funding_breadth = None
        self._cycle_btc_volume_profile_loaded = False
        self._cycle_btc_volume_profile_context = None
        self._cycle_macro_direction = None
        self._cycle_strategy_exclusions = None
        self._cycle_volatility_shadow_samples = 0
        # Abilita/invalida la cache per-cycle degli aggregati di performance
        # (global/timeframe stats + conteggi per market) usati da adjusted_weights.
        self.performance.begin_cycle()

    async def prefetch_l2_books(self, markets: list, settings) -> None:
        """Pre-popola _cycle_l2_cache con fetch async paralleli (concorrenza=4) prima
        del loop segnali, eliminando le fetch L2 sequenziali dentro generate()."""
        n_levels = settings.ob_imbalance_n_levels
        coins = {
            str((market.metadata_json or {}).get("hl_coin") or market.symbol)
            for market in markets
        }
        semaphore = asyncio.Semaphore(4)

        async def _fetch(coin: str) -> None:
            cache_key = f"{coin}:{n_levels}"
            if cache_key in self._cycle_l2_cache:
                return
            async with semaphore:
                snapshot = await fetch_l2_book_async(settings.info_url, coin, n_levels)
                self._cycle_l2_cache[cache_key] = snapshot

        await asyncio.gather(*(_fetch(coin) for coin in coins))

    def cycle_skip_counts(self) -> dict[str, int]:
        """Snapshot dei contatori di skip aggregati per il tick in corso."""
        return dict(self._cycle_skip_counts)

    def _record_skip(self, reason: str) -> None:
        self._cycle_skip_counts[reason] = self._cycle_skip_counts.get(reason, 0) + 1

    def _should_sample_volatility_skip(
        self,
        risk_value: dict[str, Any],
        *,
        market_id: int,
        timeframe: str,
        source_candle_open_time: Any,
    ) -> bool:
        if not bool(risk_value.get("volatility_filter_shadow_sample_enabled", True)):
            return False
        max_per_cycle = max(
            0,
            _safe_int(risk_value.get("volatility_filter_shadow_max_per_cycle"), 5),
        )
        if max_per_cycle == 0 or self._cycle_volatility_shadow_samples >= max_per_cycle:
            return False
        sample_rate = _safe_float(risk_value.get("volatility_filter_shadow_sample_rate"), 0.05)
        sample_rate = max(0.0, min(1.0, sample_rate))
        if sample_rate <= 0:
            return False
        if sample_rate >= 1:
            return True
        raw_key = f"{market_id}:{timeframe}:{source_candle_open_time}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        return bucket < sample_rate

    def _record_volatility_shadow_sample(
        self,
        db: Session,
        *,
        market: Market,
        timeframe: str,
        source_candle_open_time: Any,
        volatility_info: dict[str, Any] | None,
        risk_value: dict[str, Any],
        candle_count: int,
    ) -> None:
        if not self._should_sample_volatility_skip(
            risk_value,
            market_id=market.id,
            timeframe=timeframe,
            source_candle_open_time=source_candle_open_time,
        ):
            return
        self._cycle_volatility_shadow_samples += 1
        db.add(
            DecisionLog(
                event_type=_VOLATILITY_SHADOW_EVENT,
                market_id=market.id,
                message="Volatility filter shadow sample",
                data={
                    "market": market.symbol,
                    "timeframe": timeframe,
                    "source_candle_open_time": (
                        source_candle_open_time.isoformat()
                        if hasattr(source_candle_open_time, "isoformat")
                        else str(source_candle_open_time)
                    ),
                    "volatility": volatility_info or {},
                    "candle_count": candle_count,
                    "sample_rate": _safe_float(
                        risk_value.get("volatility_filter_shadow_sample_rate"), 0.05
                    ),
                    "max_per_cycle": _safe_int(
                        risk_value.get("volatility_filter_shadow_max_per_cycle"), 5
                    ),
                },
            )
        )

    @staticmethod
    def _disabled_strategy_names_for_timeframe(
        risk_value: dict[str, Any],
        timeframe: str,
    ) -> set[str]:
        raw = risk_value.get("disabled_strategies_by_timeframe")
        if not isinstance(raw, dict):
            return set()
        disabled = raw.get(timeframe)
        if not isinstance(disabled, list):
            return set()
        return {str(item) for item in disabled if isinstance(item, str) and item}

    @staticmethod
    def _enabled_strategy_names_for_timeframe(
        risk_value: dict[str, Any],
        timeframe: str,
    ) -> set[str]:
        raw = risk_value.get("enabled_strategies_by_timeframe")
        if not isinstance(raw, dict):
            return set()
        enabled = raw.get(timeframe)
        if not isinstance(enabled, list):
            return set()
        return {str(item) for item in enabled if isinstance(item, str) and item}

    def _get_cycle_strategy_exclusions(self, db: Session) -> set[str]:
        if self._cycle_strategy_exclusions is None:
            self._cycle_strategy_exclusions = effective_strategy_exclusions(db)
        return self._cycle_strategy_exclusions

    def _get_cycle_macro_suspension(
        self, db: Session, risk_value: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        """Famiglia direzionale con penalità sospese nel regime macro corrente.

        La direzione macro BTC è calcolata una volta per ciclo (query candele 4h)
        e cachata; fail-safe a (None, {}) su qualsiasi errore — le penalità
        restano attive, mai il contrario.
        """
        try:
            if self._cycle_macro_direction is None:
                self._cycle_macro_direction = btc_macro_direction(db, risk_value)
            macro = self._cycle_macro_direction
            return suspended_penalty_family(macro, risk_value), macro
        except Exception:
            logger.exception("macro_direction_failed")
            return None, {}

    def _get_cycle_funding_breadth(self, threshold_pct: float) -> dict[str, float]:
        """Restituisce la frazione di mercati con funding estremo per direzione.

        Calcolata una sola volta per ciclo (lazy) su tutti i coin in _cycle_fundings.
        `threshold_pct` è espresso in % per 8h (es. 0.03 = 0.03%/8h).
        La cache viene invalidata se il threshold cambia rispetto all'ultimo calcolo.
        """
        cached = self._cycle_funding_breadth
        if cached is not None and cached.get("_threshold") == threshold_pct:
            return cached
        if not self._cycle_fundings:
            result: dict[str, float] = {"short": 0.0, "long": 0.0, "_threshold": threshold_pct}
            self._cycle_funding_breadth = result
            return result
        rates = list(self._cycle_fundings.values())
        total = len(rates)
        if total == 0:
            result = {"short": 0.0, "long": 0.0, "_threshold": threshold_pct}
            self._cycle_funding_breadth = result
            return result
        threshold = threshold_pct / 100  # % → decimale
        crowded_short = sum(1 for r in rates if r < -threshold)
        crowded_long = sum(1 for r in rates if r > threshold)
        result = {
            "short": crowded_short / total,
            "long": crowded_long / total,
            "_threshold": threshold_pct,
        }
        self._cycle_funding_breadth = result
        return result

    @staticmethod
    def _is_ob_imbalance_only_signal(breakdown: dict[str, Any], direction: Direction) -> bool:
        if direction in {Direction.NEUTRAL}:
            return False
        aligned_directions = (
            {Direction.LONG.value, Direction.STRONG_LONG.value}
            if direction in {Direction.LONG, Direction.STRONG_LONG}
            else {Direction.SHORT.value, Direction.STRONG_SHORT.value}
        )
        aligned_sources: set[str] = set()
        for strategy_name, payload in breakdown.items():
            if strategy_name.startswith("_") or not isinstance(payload, dict):
                continue
            if str(payload.get("direction")) not in aligned_directions:
                continue
            try:
                contribution = float(payload.get("contribution", 0))
            except (TypeError, ValueError):
                contribution = 0.0
            if contribution <= 0:
                continue
            aligned_sources.add(strategy_name)
        return aligned_sources == {"ob_imbalance"}

    @staticmethod
    def _average_quote_volume(candles: list[CandleInput], window: int = 20) -> float:
        if not candles:
            return 0.0
        recent = candles[-window:]
        return sum(candle.close * candle.volume for candle in recent) / len(recent)

    @staticmethod
    def _entry_quality_result(
        candles: list[CandleInput],
        direction: Direction,
        risk_value: dict[str, Any],
        ob_analysis: OrderBookAnalysis | None,
    ) -> tuple[bool, dict[str, Any]]:
        if direction == Direction.NEUTRAL or not candles:
            return True, {"enabled": False, "reason": "neutral_or_missing_candles"}

        latest = candles[-1]
        candle_midpoint = (latest.high + latest.low) / 2
        ema_period = int(risk_value.get("entry_quality_ema_period", 21))
        ema_value = ema([candle.close for candle in candles], ema_period)[-1]
        obi = ob_analysis.obi if ob_analysis is not None else None

        long_direction = direction in {Direction.LONG, Direction.STRONG_LONG}
        if long_direction:
            close_location_passed = latest.close >= candle_midpoint
            ema_passed = ema_value is not None and latest.close >= ema_value
            ob_min = float(risk_value.get("entry_quality_long_ob_imbalance_min", 0.05))
            orderbook_passed = obi is not None and obi >= ob_min
        else:
            close_location_passed = latest.close <= candle_midpoint
            ema_passed = ema_value is not None and latest.close <= ema_value
            ob_max = float(risk_value.get("entry_quality_short_ob_imbalance_max", 0.0))
            orderbook_passed = obi is not None and obi <= ob_max

        confirmations = {
            "close_location": close_location_passed,
            "ema": ema_passed,
            "orderbook": orderbook_passed,
        }
        passed = any(confirmations.values())
        return passed, {
            "enabled": True,
            "passed": passed,
            "direction": "LONG" if long_direction else "SHORT",
            "close": latest.close,
            "candle_midpoint": candle_midpoint,
            "ema_period": ema_period,
            "ema": ema_value,
            "orderbook_obi": obi,
            "confirmations": confirmations,
        }

    def _fear_greed_result(
        self,
        context: StrategyContext,
        fear_greed: FearGreedIndex | None,
    ) -> StrategyResult | None:
        if fear_greed is None:
            return None
        distance = fear_greed.value - 50
        if abs(distance) < 10:
            direction = Direction.NEUTRAL
            confidence = 0
            reason = f"Fear & Greed neutral ({fear_greed.value})"
        elif distance > 0:
            direction = Direction.LONG
            confidence = min(55, 20 + abs(distance))
            reason = f"Risk appetite supportive ({fear_greed.value})"
        else:
            direction = Direction.SHORT
            confidence = min(55, 20 + abs(distance))
            reason = f"Market fear defensive ({fear_greed.value})"
        return StrategyResult(
            strategy_name="fear_greed",
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                "value": fear_greed.value,
                "classification": fear_greed.classification,
                "timestamp": fear_greed.timestamp,
                "source": fear_greed.source,
            },
            suggested_entry=context.candles[-1].close if context.candles else None,
        )

    def _whale_flow_result(
        self,
        context: StrategyContext,
        whale_flow: WhaleFlow | None,
    ) -> StrategyResult | None:
        if whale_flow is None:
            return None
        reason = "Whale flow neutral"
        if whale_flow.direction == Direction.LONG:
            reason = f"Whale withdrawals/accumulation dominate (${whale_flow.net_flow_usd:,.0f})"
        elif whale_flow.direction == Direction.SHORT:
            reason = f"Whale exchange inflows dominate (${abs(whale_flow.net_flow_usd):,.0f})"
        return StrategyResult(
            strategy_name="whale_flow",
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=whale_flow.direction,
            confidence=whale_flow.confidence,
            reason=reason,
            indicator_values={
                "net_flow_usd": whale_flow.net_flow_usd,
                "bullish_flow_usd": whale_flow.bullish_flow_usd,
                "bearish_flow_usd": whale_flow.bearish_flow_usd,
                "neutral_flow_usd": whale_flow.neutral_flow_usd,
                "transaction_count": whale_flow.transaction_count,
                "lookback_minutes": whale_flow.lookback_minutes,
                "min_value_usd": whale_flow.min_value_usd,
                "source": whale_flow.source,
            },
            suggested_entry=context.candles[-1].close if context.candles else None,
        )

    def _funding_rate_result(
        self,
        context: StrategyContext,
        funding_rate: float | None,
    ) -> StrategyResult | None:
        if funding_rate is None:
            return None
        rate_pct = funding_rate * 100
        abs_rate = abs(funding_rate)
        if abs_rate < 0.0001:
            return None
        if funding_rate > 0:
            direction = Direction.SHORT
            confidence = min(60, int(20 + abs_rate * 40_000))
            reason = f"Funding positivo ({rate_pct:.4f}%/8h): i long pagano un premio"
        else:
            direction = Direction.LONG
            confidence = min(60, int(20 + abs_rate * 40_000))
            reason = f"Funding negativo ({rate_pct:.4f}%/8h): gli short pagano un premio"
        return StrategyResult(
            strategy_name="funding_rate",
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                "funding_rate": funding_rate,
                "funding_rate_pct": rate_pct,
                "source": "hyperliquid",
            },
            suggested_entry=context.candles[-1].close if context.candles else None,
        )

    def _altcoin_season_result(
        self,
        context: StrategyContext,
        index: AltcoinSeasonIndex | None,
        base_symbol: str,
    ) -> StrategyResult | None:
        if index is None:
            return None
        base = base_symbol.upper()
        value = index.altcoin_index
        direction = Direction.NEUTRAL
        confidence = 0
        reason = f"Altcoin Season neutral ({value})"
        if base == "BTC":
            if value <= 25:
                direction = Direction.LONG
                confidence = min(45, 20 + (25 - value))
                reason = f"Bitcoin season supports BTC relative strength ({value})"
            elif value >= 75:
                direction = Direction.SHORT
                confidence = min(45, 20 + (value - 75))
                reason = f"Altcoin season weakens BTC relative strength ({value})"
        else:
            if value >= 75:
                direction = Direction.LONG
                confidence = min(50, 20 + (value - 75))
                reason = f"Altcoin season supports altcoin risk ({value})"
            elif value <= 25:
                direction = Direction.SHORT
                confidence = min(50, 20 + (25 - value))
                reason = f"Bitcoin season pressures altcoin risk ({value})"
        return StrategyResult(
            strategy_name="altcoin_season",
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                "altcoin_index": index.altcoin_index,
                "altcoin_marketcap": index.altcoin_marketcap,
                "snapshot_time": index.snapshot_time,
                "yearly_high": index.yearly_high,
                "yearly_high_date": index.yearly_high_date,
                "yearly_low": index.yearly_low,
                "yearly_low_date": index.yearly_low_date,
                "source": index.source,
            },
            suggested_entry=context.candles[-1].close if context.candles else None,
        )

    @staticmethod
    def _directions_oppose(left: Direction, right: Direction) -> bool:
        long_directions = {Direction.LONG, Direction.STRONG_LONG}
        short_directions = {Direction.SHORT, Direction.STRONG_SHORT}
        return (left in long_directions and right in short_directions) or (
            left in short_directions and right in long_directions
        )

    def _liquidation_pressure_penalty(
        self,
        market: Market,
        score: ScoreResult,
        risk_value: dict[str, Any],
    ) -> int:
        """Penalità (punti di soglia) quando le liquidazioni recenti vanno *contro*
        la direzione del segnale. Misura l'intensità oraria recente (default 1h)
        rapportata alla media sul baseline (default 24h) per il lato che si oppone
        alla direzione; sopra `liq_pressure_ratio_threshold` la penalità sale
        linearmente fino a `liq_max_penalty` (a 2× la soglia). Fail-open: dato
        assente/errore o asset non coperto => 0.
        """
        max_penalty = int(risk_value.get("liq_max_penalty", 15))
        if max_penalty <= 0:
            return 0
        recent_hours = int(risk_value.get("liq_recent_hours", 1))
        baseline_hours = int(risk_value.get("liq_baseline_hours", 24))
        ratio_threshold = float(risk_value.get("liq_pressure_ratio_threshold", 3.0))

        pressure = self._kiotaka.liquidation_pressure(
            coin=str(market.base),
            recent_hours=recent_hours,
            baseline_hours=baseline_hours,
        )
        if pressure is None or pressure.points < 2:
            return 0

        if score.direction in {Direction.SHORT, Direction.STRONG_SHORT}:
            contra_ratio = pressure.short_pressure_ratio
            contra_side = "short_squeeze"
        elif score.direction in {Direction.LONG, Direction.STRONG_LONG}:
            contra_ratio = pressure.long_pressure_ratio
            contra_side = "long_flush"
        else:
            return 0

        penalty = 0
        if ratio_threshold > 0 and contra_ratio > ratio_threshold:
            scaled = (contra_ratio - ratio_threshold) / ratio_threshold
            penalty = min(max_penalty, round(max_penalty * scaled))

        score.breakdown["_liquidation_pressure"] = {
            "coin": market.base,
            "contra_side": contra_side,
            "contra_ratio": round(contra_ratio, 2),
            "ratio_threshold": ratio_threshold,
            "short_recent_usd": round(pressure.short_recent, 0),
            "long_recent_usd": round(pressure.long_recent, 0),
            "recent_hours": recent_hours,
            "baseline_hours": baseline_hours,
            "penalty_applied": penalty,
        }
        return penalty

    def _btc_volume_profile_penalty(
        self,
        db: Session,
        score: ScoreResult,
        risk_value: dict[str, Any],
    ) -> int:
        """Macro BTC VPVR gate: usa solo il volume profile BTC come contesto
        risk-on/risk-off per i segnali direzionali sulle alt. Non abbassa mai la
        soglia minima: applica solo penalità conservative e fail-open."""
        if not bool(risk_value.get("btc_volume_profile_gate_enabled", True)):
            return 0
        if score.direction not in {
            Direction.LONG,
            Direction.STRONG_LONG,
            Direction.SHORT,
            Direction.STRONG_SHORT,
        }:
            return 0
        context = self._btc_volume_profile_context(db, risk_value)
        if context is None:
            return 0

        profile: VolumeProfileResult = context["profile"]
        current_price = float(context["current_price"])
        previous_price = context.get("previous_price")
        previous_price = float(previous_price) if previous_price is not None else None
        proximity_pct = float(risk_value.get("btc_volume_profile_proximity_pct", 0.004))
        distance_poc = self._distance_pct(current_price, profile.poc_price)
        distance_vah = self._distance_pct(current_price, profile.vah_price)
        distance_val = self._distance_pct(current_price, profile.val_price)
        near_poc = distance_poc <= proximity_pct
        state = "inside_value_area"
        if current_price > profile.vah_price:
            state = "above_vah"
        elif current_price < profile.val_price:
            state = "below_val"
        elif near_poc:
            state = "near_poc"

        is_long = score.direction in {Direction.LONG, Direction.STRONG_LONG}
        is_short = score.direction in {Direction.SHORT, Direction.STRONG_SHORT}
        vah_rejection = (
            previous_price is not None
            and previous_price > profile.vah_price
            and current_price < profile.vah_price
        )
        val_rejection = (
            previous_price is not None
            and previous_price < profile.val_price
            and current_price > profile.val_price
        )

        penalty = 0
        reason = "context_only"
        if is_long and state == "below_val":
            penalty = int(risk_value.get("btc_volume_profile_risk_off_long_penalty", 6))
            reason = "btc_below_val_risk_off"
        elif is_short and state == "above_vah":
            penalty = int(risk_value.get("btc_volume_profile_risk_on_short_penalty", 6))
            reason = "btc_above_vah_risk_on"
        elif is_long and vah_rejection:
            penalty = int(risk_value.get("btc_volume_profile_rejection_penalty", 4))
            reason = "btc_vah_rejection_against_long"
        elif is_short and val_rejection:
            penalty = int(risk_value.get("btc_volume_profile_rejection_penalty", 4))
            reason = "btc_val_rejection_against_short"
        elif near_poc:
            penalty = int(risk_value.get("btc_volume_profile_poc_chop_penalty", 0))
            reason = "btc_near_poc_chop"
        penalty = max(0, penalty)

        score.breakdown["_btc_volume_profile"] = {
            "source": profile.source,
            "exchange": context.get("exchange"),
            "raw_symbol": context.get("raw_symbol"),
            "interval": context.get("interval"),
            "lookback_hours": context.get("lookback_hours"),
            "price_timeframe": context.get("price_timeframe"),
            "current_price": round(current_price, 6),
            "previous_price": round(previous_price, 6) if previous_price is not None else None,
            "state": state,
            "poc": round(profile.poc_price, 6),
            "vah": round(profile.vah_price, 6),
            "val": round(profile.val_price, 6),
            "distance_from_poc_pct": round(distance_poc * 100, 3),
            "distance_from_vah_pct": round(distance_vah * 100, 3),
            "distance_from_val_pct": round(distance_val * 100, 3),
            "vah_rejection": vah_rejection,
            "val_rejection": val_rejection,
            "reason": reason,
            "penalty_applied": penalty,
        }
        return penalty

    def _btc_volume_profile_context(
        self,
        db: Session,
        risk_value: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self._cycle_btc_volume_profile_loaded:
            return self._cycle_btc_volume_profile_context
        self._cycle_btc_volume_profile_loaded = True

        exchange = str(risk_value.get("btc_volume_profile_exchange", "HYPERLIQUID_FUTURES"))
        raw_symbol = str(risk_value.get("btc_volume_profile_raw_symbol", "BTC"))
        interval = str(risk_value.get("btc_volume_profile_interval", "HOUR"))
        lookback_hours = max(1, int(risk_value.get("btc_volume_profile_lookback_hours", 48)))
        price_timeframe = str(risk_value.get("btc_volume_profile_price_timeframe", "1h"))
        period_s = lookback_hours * 3600
        from_s = int(time.time()) - period_s
        profile = self._kiotaka.volume_profile(
            exchange=exchange,
            raw_symbol=raw_symbol,
            interval=interval,
            from_s=from_s,
            period_s=period_s,
            value_area_pct=float(risk_value.get("btc_volume_profile_value_area_pct", 0.70)),
            hvn_percentile=float(risk_value.get("btc_volume_profile_hvn_percentile", 0.80)),
            lvn_percentile=float(risk_value.get("btc_volume_profile_lvn_percentile", 0.20)),
        )
        if profile is None:
            return None

        btc_candles = (
            db.query(Candle)
            .join(Market, Market.id == Candle.market_id)
            .filter(
                Market.base == "BTC",
                Market.market_type == "perp",
                Candle.timeframe == price_timeframe,
            )
            .order_by(Candle.open_time.desc())
            .limit(2)
            .all()
        )
        if not btc_candles:
            return None
        current_price = float(btc_candles[0].close)
        previous_price = float(btc_candles[1].close) if len(btc_candles) > 1 else None
        self._cycle_btc_volume_profile_context = {
            "profile": profile,
            "exchange": exchange,
            "raw_symbol": raw_symbol,
            "interval": interval,
            "lookback_hours": lookback_hours,
            "price_timeframe": price_timeframe,
            "current_price": current_price,
            "previous_price": previous_price,
        }
        return self._cycle_btc_volume_profile_context

    @staticmethod
    def _distance_pct(price: float, level: float) -> float:
        return abs(price - level) / price if price else 0.0

    def _global_volume_quote(self, market: Market) -> MarketQuote | None:
        if not get_settings().external_quote_enrichment_enabled:
            return None
        quote = self.market_data.latest_quote(market.base)
        if quote is None and market.symbol != market.base:
            quote = self.market_data.latest_quote(market.symbol)
        return quote

    def _persist_indicators(
        self,
        db: Session,
        market: Market,
        timeframe: str,
        timestamp,
        results,
    ) -> None:
        merged = {
            result.strategy_name: {
                **result.indicator_values,
                "_direction": result.direction.value,
                "_confidence": result.confidence,
                "_reason": result.reason,
            }
            for result in results
        }
        stmt = pg_insert(Indicator).values(
            market_id=market.id,
            timeframe=timeframe,
            timestamp=timestamp,
            values=merged,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_indicators_market_tf_time",
            set_={"values": merged},
        )
        db.execute(stmt)

    @staticmethod
    def _regime_filter_kwargs(risk_value: dict[str, Any]) -> dict[str, Any]:
        """Parametri del regime filter estratti da AppSetting "risk".

        Restituisce un dict pronto per `apply_regime_filter(**kwargs)`. Isolato da
        generate() per essere testabile e ridurne la dimensione; `method` invalido
        viene normalizzato ad "adx"."""
        method = str(risk_value.get("regime_method", "adx")).lower()
        if method not in ("adx", "hurst", "garch", "hybrid"):
            method = "adx"
        return {
            "method": method,
            "adx_trending": float(risk_value.get("regime_adx_trending", 25.0)),
            "adx_ranging": float(risk_value.get("regime_adx_ranging", 18.0)),
            "confirm_candles": int(risk_value.get("regime_confirm_candles", 2)),
            "off_regime_weight_multiplier": float(
                risk_value.get("regime_off_weight_multiplier", 0.5)
            ),
            "hurst_trending": float(risk_value.get("regime_hurst_trending", 0.55)),
            "hurst_ranging": float(risk_value.get("regime_hurst_ranging", 0.45)),
            "hurst_min_lag": int(risk_value.get("regime_hurst_min_lag", 2)),
            "hurst_max_lag": int(risk_value.get("regime_hurst_max_lag", 20)),
            "garch_window": int(risk_value.get("regime_garch_window", 50)),
            "garch_lag": int(risk_value.get("regime_garch_lag", 1)),
            "garch_cluster_threshold": float(
                risk_value.get("regime_garch_cluster_threshold", 0.2)
            ),
        }

    def generate(self, db: Session, market: Market, timeframe: str, limit: int = 250) -> Signal | None:
        settings = get_settings()
        rows = (
            db.query(Candle)
            .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(limit)
            .all()
        )
        candles = [
            CandleInput(
                open_time=row.open_time,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                open_interest=float(row.open_interest) if row.open_interest is not None else None,
            )
            for row in reversed(rows)
        ]
        if len(candles) < 60:
            logger.debug(
                "signal_skipped_insufficient_candles",
                market=market.symbol,
                timeframe=timeframe,
                candle_count=len(candles),
            )
            self._record_skip("insufficient_candles")
            return None
        latest_candle_open_time = rows[0].open_time

        risk_settings = db.get(AppSetting, "risk")
        min_signal_score = settings.min_signal_score
        min_risk_reward = settings.min_risk_reward
        min_average_quote_volume_usd = settings.min_average_quote_volume_usd
        min_atr_multiplier = 1.5
        # Stop minimum scalato per timeframe: lo stop "duro" di 2% è sensato su 4h ma
        # eccessivo su 5m (= 4-6 ATR di noise) e penalizza il RR raggiungibile. Le
        # scale derivano dalla volatilità tipica per candela su BTC/ETH perpetual.
        min_stop_distance_pct = _DEFAULT_MIN_STOP_BY_TF.get(timeframe, 0.020)
        # AB testing auto-tuner (§3.10): in mode="ab_test" l'auto-tuner non modifica
        # `risk` globalmente — ogni segnale è assegnato a control o test e l'overlay
        # del relativo arm viene mergiato in `risk_value` prima di derivare i filtri.
        ab_overlay_payload = active_ab_overlay(
            db,
            market_id=market.id,
            timeframe=timeframe,
            source_candle_open_time=latest_candle_open_time,
        )
        ab_overlay: dict[str, Any] = (
            dict(ab_overlay_payload.get("overlay", {})) if ab_overlay_payload else {}
        )
        risk_value: dict[str, Any] = (
            dict(risk_settings.value) if risk_settings and isinstance(risk_settings.value, dict) else {}
        )
        if ab_overlay:
            risk_value.update(ab_overlay)
        min_signal_score = int(risk_value.get("min_signal_score", min_signal_score))
        min_signal_score = apply_timeframe_min_score_override(
            risk_value, timeframe, min_signal_score
        )
        # Direzione macro BTC (cache per ciclo): determina se le penalità
        # direzionali apprese nel regime opposto vanno sospese per questo segnale.
        macro_suspended_family, macro_direction_info = self._get_cycle_macro_suspension(
            db, risk_value
        )
        min_risk_reward = float(risk_value.get("min_risk_reward", min_risk_reward))
        min_average_quote_volume_usd = float(
            risk_value.get("min_average_quote_volume_usd", min_average_quote_volume_usd)
        )
        # Scala la soglia proporzionalmente al timeframe: il valore in DB è il
        # riferimento per 1h (60 min). Candle più corte hanno meno volume per
        # definizione, quindi la soglia si riduce linearmente.
        tf_minutes = _TF_MINUTES.get(timeframe, 60)
        min_average_quote_volume_usd *= tf_minutes / 60
        min_atr_multiplier = float(risk_value.get("min_atr_multiplier", min_atr_multiplier))
        min_stop_distance_pct = float(
            risk_value.get("min_stop_distance_pct", min_stop_distance_pct)
        )
        scoring_settings = db.get(AppSetting, "scoring")
        scoring_value = (
            scoring_settings.value
            if scoring_settings and isinstance(scoring_settings.value, dict)
            else {}
        )
        strong_threshold = int(scoring_value.get("strong_score_threshold", 75))
        label_map = scoring_value.get("label_thresholds")
        label_tuple: tuple[tuple[int, str], ...] | None = None
        if isinstance(label_map, dict):
            try:
                label_tuple = tuple(
                    sorted(
                        ((int(v), str(k)) for k, v in label_map.items()),
                        key=lambda item: item[0],
                    )
                )
            except (TypeError, ValueError):
                label_tuple = None
        if label_tuple:
            scoring_engine = SignalScoringEngine(
                strong_score_threshold=strong_threshold,
                label_thresholds=label_tuple,
            )
        else:
            scoring_engine = SignalScoringEngine(
                strong_score_threshold=strong_threshold,
            )
        regime_filter_enabled = bool(risk_value.get("regime_filter_enabled", False))
        participation_ratio = float(risk_value.get("participation_ratio", 0.35))
        volatility_filter_enabled = bool(risk_value.get("volatility_filter_enabled", False))
        # Default 25-90: 10-95 era 85% della distribuzione — filtro decorativo. 25-90
        # taglia effettivamente mercati morti (sotto p25) e panic moves (sopra p90).
        atr_pct_min = float(risk_value.get("atr_pct_min", 0.25))
        atr_pct_max = float(risk_value.get("atr_pct_max", 0.90))
        # Durante starvation override la maggior parte dei mercati può trovarsi in una
        # fase di compressione di volatilità relativa: il filtro ATR percentile blocca
        # più candidati del previsto (>60% osservato) aggravando la starvation stessa.
        # Il bypass è automatico e si disattiva non appena la starvation rientra.
        if volatility_filter_enabled:
            cal_state_setting = db.get(AppSetting, "auto_calibration_state")
            if cal_state_setting and isinstance(cal_state_setting.value, dict):
                if cal_state_setting.value.get("starvation_override_active"):
                    volatility_filter_enabled = False
                    logger.debug(
                        "volatility_filter_bypassed_starvation",
                        market=market.symbol,
                        timeframe=timeframe,
                    )
        mtf_confluence_enabled = bool(risk_value.get("mtf_confluence_enabled", False))
        mtf_ema_period = int(risk_value.get("mtf_ema_period", 50))
        volatility_passed, volatility_info = evaluate_volatility(
            candles,
            enabled=volatility_filter_enabled,
            pct_min=atr_pct_min,
            pct_max=atr_pct_max,
        )
        if not volatility_passed:
            logger.debug(
                "signal_skipped_volatility_filter",
                market=market.symbol,
                timeframe=timeframe,
                volatility=volatility_info,
            )
            self._record_volatility_shadow_sample(
                db,
                market=market,
                timeframe=timeframe,
                source_candle_open_time=latest_candle_open_time,
                volatility_info=volatility_info,
                risk_value=risk_value,
                candle_count=len(candles),
            )
            self._record_skip("volatility_filter")
            db.commit()
            return None
        global_quote = self._global_volume_quote(market)
        hyperliquid_average_quote_volume = self._average_quote_volume(candles)
        if min_average_quote_volume_usd > 0:
            if hyperliquid_average_quote_volume < min_average_quote_volume_usd:
                logger.debug(
                    "signal_skipped_low_liquidity",
                    market=market.symbol,
                    timeframe=timeframe,
                    global_quote_volume_24h_usd=global_quote.volume_24h_usd if global_quote else None,
                    hyperliquid_average_quote_volume=hyperliquid_average_quote_volume,
                    min_average_quote_volume_usd=min_average_quote_volume_usd,
                )
                self._record_skip("low_liquidity")
                return None

        # Early-exit duplicate check: evita di pagare HTTP esterne (fundings, L2 book,
        # whale/altcoin/fear-greed) e l'esecuzione di tutte le strategie quando un
        # segnale per questa candela è già stato emesso. Era a metà funzione, dopo i
        # costi pesanti — adesso è il primo controllo dopo aver identificato la candela.
        existing_signal = (
            db.query(Signal)
            .filter(
                Signal.market_id == market.id,
                Signal.timeframe == timeframe,
                Signal.source_candle_open_time == latest_candle_open_time,
            )
            .first()
        )
        if existing_signal is not None:
            logger.debug(
                "signal_skipped_duplicate",
                market=market.symbol,
                timeframe=timeframe,
                existing_signal_id=existing_signal.id,
            )
            self._record_skip("duplicate")
            return None
        configs = (
            db.query(StrategyConfig)
            .filter(StrategyConfig.timeframe == timeframe)
            .all()
        )
        force_enabled_strategy_names = self._enabled_strategy_names_for_timeframe(
            risk_value, timeframe
        )
        configs = [
            config
            for config in configs
            if config.enabled or config.strategy_name in force_enabled_strategy_names
        ]
        # Unione di: disable tattico per-timeframe (risk setting) + cull permanente
        # globale (strategy_exclusions). Entrambi escludono la strategia dallo scoring.
        skip_strategy_names = self._disabled_strategy_names_for_timeframe(
            risk_value, timeframe
        ) | self._get_cycle_strategy_exclusions(db)
        if skip_strategy_names:
            configs = [
                config
                for config in configs
                if config.strategy_name not in skip_strategy_names
            ]
        context = StrategyContext(
            market="hyperliquid",
            symbol=market.symbol,
            timeframe=timeframe,
            market_type=market.market_type,
            candles=candles,
        )
        results = []
        weights: dict[str, int] = {}
        # Snapshot del peso "calibrated" (post-AutoCalibrationEngine, prima del
        # performance adjustment e del regime filter). Serve per esporre la pipeline
        # completa nel breakdown del segnale.
        calibrated_weights: dict[str, int] = {}
        raw_direction_strategy_overrides = risk_value.get("direction_strategy_weight_overrides")
        direction_strategy_overrides: dict[str, Any] = (
            raw_direction_strategy_overrides
            if isinstance(raw_direction_strategy_overrides, dict)
            else {}
        )
        for config in configs:
            strategy = self.registry.get(config.strategy_name)
            if strategy is None:
                continue
            try:
                result = strategy.evaluate(context, config.params)
            except Exception:
                # Una strategia che solleva non deve uccidere l'intero segnale:
                # la saltiamo e lasciamo che le altre contribuiscano allo scoring.
                logger.exception(
                    "strategy_evaluate_failed",
                    strategy=config.strategy_name,
                    timeframe=timeframe,
                )
                self._record_skip("strategy_error")
                continue
            results.append(result)
            weight = config.weight
            direction_key = (
                "LONG"
                if result.direction in {Direction.LONG, Direction.STRONG_LONG}
                else "SHORT"
                if result.direction in {Direction.SHORT, Direction.STRONG_SHORT}
                else None
            )
            override_key = f"{timeframe}:{direction_key}:{config.strategy_name}" if direction_key else None
            # Gli override per la famiglia sospesa (appresi nel regime macro opposto)
            # non vengono applicati finché il macro resta favorevole a quella direzione.
            if direction_key is not None and direction_key == macro_suspended_family:
                override_key = None
            if override_key and override_key in direction_strategy_overrides:
                try:
                    weight = max(0, int(direction_strategy_overrides[override_key]))
                except (TypeError, ValueError):
                    weight = config.weight
            weights[config.strategy_name] = weight
            calibrated_weights[config.strategy_name] = weight
        disabled_sources = set(risk_value.get("disabled_signal_sources", []))
        fear_greed = self.market_data.latest_fear_greed()
        if "fear_greed" not in disabled_sources:
            fear_greed_result = self._fear_greed_result(context, fear_greed)
            if fear_greed_result is not None:
                results.append(fear_greed_result)
                weights[fear_greed_result.strategy_name] = max(0, settings.fear_greed_weight)
        altcoin_season = (
            self.market_data.latest_altcoin_season()
            if "altcoin_season" not in disabled_sources
            else None
        )
        altcoin_season_result = self._altcoin_season_result(context, altcoin_season, market.base)
        if altcoin_season_result is not None:
            results.append(altcoin_season_result)
            weights[altcoin_season_result.strategy_name] = max(0, settings.altcoin_season_weight)
        whale_flow = self.whales.recent_flow(market.base) if "whale_flow" not in disabled_sources else None
        if "whale_flow" not in disabled_sources:
            whale_flow_result = self._whale_flow_result(context, whale_flow)
            if whale_flow_result is not None:
                results.append(whale_flow_result)
                weights[whale_flow_result.strategy_name] = max(0, settings.whale_flow_weight)
        coin = (market.metadata_json or {}).get("hl_coin") or market.symbol
        funding_rate_value: float | None = None
        if "funding_rate" not in disabled_sources:
            if self._cycle_fundings is None:
                # fetch_predicted_fundings ritorna la mappa di TUTTI i coin con un'unica
                # richiesta HTTP. Cachata per cycle evita di chiamarla 1 volta per ogni
                # (market × timeframe) durante un tick.
                self._cycle_fundings = fetch_predicted_fundings(settings.info_url)
            funding_rate_value = self._cycle_fundings.get(str(coin))
        funding_result = self._funding_rate_result(context, funding_rate_value)
        if funding_result is not None:
            results.append(funding_result)
            weights[funding_result.strategy_name] = max(0, settings.funding_rate_weight)
        ob_analysis: OrderBookAnalysis | None = None
        ob_stats: OrderBookImbalanceStats | None = None
        ob_snapshot = None
        if "ob_imbalance" not in disabled_sources:
            # L2 book cachato per (coin, n_levels) per la durata del tick: lo stesso market
            # è chiamato 1 volta per timeframe (1m/5m/15m...) e l'orderbook non cambia
            # significativamente in pochi secondi rispetto al costo della call HTTP.
            ob_cache_key = f"{coin}:{settings.ob_imbalance_n_levels}"
            if ob_cache_key in self._cycle_l2_cache:
                ob_snapshot = self._cycle_l2_cache[ob_cache_key]
            else:
                ob_snapshot = fetch_l2_book(
                    settings.info_url, str(coin), n_levels=settings.ob_imbalance_n_levels
                )
                self._cycle_l2_cache[ob_cache_key] = ob_snapshot
        if ob_snapshot is not None:
            ob_analysis = analyze_order_book(
                ob_snapshot,
                entry=candles[-1].close,
                n_levels=settings.ob_imbalance_n_levels,
            )
            ob_stats = update_obi_stats(ob_cache_key, ob_analysis.obi)
            ob_result = ob_imbalance_result(context, ob_analysis)
            if ob_result is not None:
                results.append(ob_result)
                weights[ob_result.strategy_name] = max(0, settings.ob_imbalance_weight)
        # Nota: la persistenza degli indicatori (`_persist_indicators`) è rimandata
        # a valle di tutti i filtri, subito prima di creare il Signal: scriviamo solo
        # per i segnali effettivamente emessi invece che per ogni valutazione (la
        # maggior parte viene scartata). L'endpoint /indicators ricade su un calcolo
        # on-demand quando non trova righe, quindi la UI resta coperta.

        weights, performance_notes = self.performance.adjusted_weights(
            db,
            weights,
            timeframe=timeframe,
            market_id=market.id,
        )
        performance_adjusted_weights = dict(weights)
        weights, regime_info = apply_regime_filter(
            weights,
            candles,
            enabled=regime_filter_enabled,
            **self._regime_filter_kwargs(risk_value),  # type: ignore[arg-type]
        )
        if regime_info is not None and regime_info["neutralized"]:
            logger.debug(
                "signal_strategy_neutralized_by_regime",
                market=market.symbol,
                timeframe=timeframe,
                regime=regime_info["regime"],
                method=regime_info.get("method"),
                adx=regime_info.get("adx"),
                hurst=regime_info.get("hurst"),
                garch=regime_info.get("garch"),
                neutralized=regime_info["neutralized"],
            )
        score = scoring_engine.score(results, weights, participation_ratio=participation_ratio)
        if performance_notes:
            score.breakdown["_performance_adjustments"] = performance_notes
        if regime_info is not None:
            score.breakdown["_regime"] = regime_info
        # Pipeline esplicita dei pesi: utile per debug quando un segnale "evapora"
        # tra strategia, calibrazione, performance adjustment e regime filter.
        score.breakdown["_weight_pipeline"] = {
            name: {
                "calibrated": calibrated_weights.get(name),
                "performance_adjusted": performance_adjusted_weights.get(name),
                "regime_filtered": weights.get(name),
            }
            for name in calibrated_weights
        }
        if volatility_info is not None:
            score.breakdown["_volatility"] = volatility_info
        score.breakdown["_funding_rate"] = {
            "coin": coin,
            "rate": funding_rate_value,
            "rate_pct": funding_rate_value * 100 if funding_rate_value is not None else None,
        }
        if ob_analysis is not None:
            score.breakdown["_order_book"] = {
                "obi": round(ob_analysis.obi, 4),
                "spread_pct": round(ob_analysis.spread_pct * 100, 4),
                "bid_volume": round(ob_analysis.bid_volume, 4),
                "ask_volume": round(ob_analysis.ask_volume, 4),
                "bid_wall_count": len(ob_analysis.bid_walls),
                "ask_wall_count": len(ob_analysis.ask_walls),
                "bid_walls": [{"price": w.price, "size": w.size} for w in ob_analysis.bid_walls],
                "ask_walls": [{"price": w.price, "size": w.size} for w in ob_analysis.ask_walls],
                "obi_delta": round(ob_stats.obi_delta, 4)
                if ob_stats is not None and ob_stats.obi_delta is not None
                else None,
                "obi_zscore": round(ob_stats.obi_zscore, 4)
                if ob_stats is not None and ob_stats.obi_zscore is not None
                else None,
                "baseline_mean": round(ob_stats.baseline_mean, 4)
                if ob_stats is not None and ob_stats.baseline_mean is not None
                else None,
                "baseline_std": round(ob_stats.baseline_std, 4)
                if ob_stats is not None and ob_stats.baseline_std is not None
                else None,
                "baseline_samples": ob_stats.baseline_samples if ob_stats is not None else 0,
            }
        score.breakdown["_market_context"] = {
            "hyperliquid_average_quote_volume_usd": hyperliquid_average_quote_volume,
            "global_quote": {
                "symbol": global_quote.symbol,
                "price": global_quote.price,
                "volume_24h_usd": global_quote.volume_24h_usd,
                "volume_change_24h": global_quote.volume_change_24h,
                "percent_change_1h": global_quote.percent_change_1h,
                "percent_change_24h": global_quote.percent_change_24h,
                "percent_change_7d": global_quote.percent_change_7d,
                "market_cap": global_quote.market_cap,
                "last_updated": global_quote.last_updated,
                "source": global_quote.source,
            }
            if global_quote
            else None,
            "altcoin_season": {
                "altcoin_index": altcoin_season.altcoin_index,
                "altcoin_marketcap": altcoin_season.altcoin_marketcap,
                "snapshot_time": altcoin_season.snapshot_time,
                "yearly_high": altcoin_season.yearly_high,
                "yearly_high_date": altcoin_season.yearly_high_date,
                "yearly_low": altcoin_season.yearly_low,
                "yearly_low_date": altcoin_season.yearly_low_date,
                "source": altcoin_season.source,
            }
            if altcoin_season
            else None,
        }
        funding_block_threshold = risk_value.get("funding_block_threshold_pct")
        if (
            funding_block_threshold is not None
            and funding_rate_value is not None
            and score.direction != Direction.NEUTRAL
        ):
            try:
                threshold_pct = float(funding_block_threshold)
            except (TypeError, ValueError):
                threshold_pct = None
            else:
                # threshold espresso in % per 8h (es. 0.05 = 0.05%/8h)
                assert threshold_pct is not None
                funding_pct = funding_rate_value * 100
                if (
                    funding_pct > threshold_pct
                    and score.direction in {Direction.LONG, Direction.STRONG_LONG}
                ) or (
                    funding_pct < -threshold_pct
                    and score.direction in {Direction.SHORT, Direction.STRONG_SHORT}
                ):
                    logger.debug(
                        "signal_skipped_funding_block",
                        market=market.symbol,
                        timeframe=timeframe,
                        funding_pct=round(funding_pct, 4),
                        threshold_pct=threshold_pct,
                        direction=score.direction.value,
                    )
                    self._record_skip("funding_block")
                    db.commit()
                    return None
        if (
            whale_flow is not None
            and whale_flow.confidence >= settings.whale_contra_block_confidence
            and self._directions_oppose(score.direction, whale_flow.direction)
        ):
            logger.debug(
                "signal_skipped_whale_contra_flow",
                market=market.symbol,
                timeframe=timeframe,
                direction=score.direction.value,
                whale_direction=whale_flow.direction.value,
                whale_confidence=whale_flow.confidence,
                whale_net_flow_usd=whale_flow.net_flow_usd,
            )
            self._record_skip("whale_contra_flow")
            db.commit()
            return None
        edge_adjustment = signal_threshold_adjustment(
            risk_value,
            market_id=market.id,
            timeframe=timeframe,
            direction=score.direction,
            score=score.score,
            suspended_family=macro_suspended_family,
        )
        effective_min_signal_score = min_signal_score + int(edge_adjustment["total"])
        direction_family = edge_adjustment.get("direction")
        directional_key = f"{direction_family}:{timeframe}" if direction_family else None
        if macro_suspended_family is not None and direction_family == macro_suspended_family:
            score.breakdown["_macro_direction_suspension"] = {
                "suspended_family": macro_suspended_family,
                "macro": macro_direction_info,
            }
        directional_blocklist = risk_value.get("directional_blocklist")
        if (
            bool(risk_value.get("directional_blocklist_enabled", True))
            and directional_key
            and isinstance(directional_blocklist, dict)
            and directional_key in directional_blocklist
            # La blocklist LONG appresa in regime bearish è sospesa quando il
            # macro BTC torna bullish (non cancellata: riprende se il regime gira).
            and direction_family != macro_suspended_family
        ):
            score.breakdown["_directional_blocklist"] = {
                "key": directional_key,
                "payload": directional_blocklist.get(directional_key),
                "effective_min_signal_score": effective_min_signal_score,
            }
            logger.debug(
                "signal_skipped_directional_blocklist",
                market=market.symbol,
                timeframe=timeframe,
                direction=score.direction.value,
                score=score.score,
                directional_key=directional_key,
            )
            self._record_skip("directional_blocklist")
            db.commit()
            return None
        extra_directional_min_score = 0
        directional_min_score_adjustments = (
            None
            if direction_family == macro_suspended_family and macro_suspended_family is not None
            else risk_value.get("directional_min_score_adjustments")
        )
        if directional_key and isinstance(directional_min_score_adjustments, dict):
            raw_extra = directional_min_score_adjustments.get(directional_key)
            if isinstance(raw_extra, dict):
                raw_extra = raw_extra.get("adjustment", 0)
            try:
                extra_directional_min_score = max(0, int(raw_extra or 0))
            except (TypeError, ValueError):
                extra_directional_min_score = 0
        if extra_directional_min_score > 0:
            effective_min_signal_score += extra_directional_min_score
            score.breakdown["_directional_min_score_adjustment"] = {
                "key": directional_key,
                "adjustment": extra_directional_min_score,
                "payload": directional_min_score_adjustments.get(directional_key)
                if isinstance(directional_min_score_adjustments, dict) and directional_key
                else None,
                "effective_min_signal_score": effective_min_signal_score,
            }
        # Penalità direzione applicata dopo il cap max_combined_score_adjustment.
        # Permette di alzare ulteriormente la soglia per LONG/STRONG_LONG senza
        # interferire con la logica di calibrazione automatica. Sospesa quando il
        # macro BTC è bullish (penalità appresa in regime opposto).
        if (
            score.direction in {Direction.LONG, Direction.STRONG_LONG}
            and macro_suspended_family != "LONG"
        ):
            long_score_penalty = int(risk_value.get("long_score_penalty", 0))
            if long_score_penalty > 0:
                effective_min_signal_score += long_score_penalty
                score.breakdown["_long_score_penalty"] = {
                    "penalty": long_score_penalty,
                    "effective_min_signal_score": effective_min_signal_score,
                }
        # Funding breadth penalty: se il mercato è sovra-affollato nella direzione
        # del segnale (es. 60%+ dei perp con funding molto negativo durante un bear
        # move), il trade è fragile — tipico segnale di false breakout soggetto a
        # short squeeze. Alziamo la soglia min invece di bloccare: i segnali deboli
        # vengono filtrati ma quelli forti passano, e i dati paper rimangono intatti.
        if self._cycle_fundings is not None and score.direction != Direction.NEUTRAL:
            fb_threshold_pct = float(risk_value.get("funding_breadth_threshold_pct", 0.03))
            fb_trigger = float(risk_value.get("funding_breadth_trigger_pct", 0.60))
            fb_penalty = int(risk_value.get("funding_breadth_score_penalty", 5))
            if fb_penalty > 0:
                breadth = self._get_cycle_funding_breadth(fb_threshold_pct)
                is_short = score.direction in {Direction.SHORT, Direction.STRONG_SHORT}
                is_long = score.direction in {Direction.LONG, Direction.STRONG_LONG}
                direction_breadth = (
                    breadth["short"] if is_short else breadth["long"] if is_long else 0.0
                )
                penalty_applied = fb_penalty if direction_breadth >= fb_trigger else 0
                if penalty_applied:
                    effective_min_signal_score += penalty_applied
                score.breakdown["_funding_breadth"] = {
                    "short_breadth": round(breadth["short"], 3),
                    "long_breadth": round(breadth["long"], 3),
                    "direction_breadth": round(direction_breadth, 3),
                    "threshold_pct": fb_threshold_pct,
                    "trigger_pct": fb_trigger,
                    "penalty_applied": penalty_applied,
                    "effective_min_signal_score": effective_min_signal_score,
                }
        btc_volume_profile_penalty = 0
        if score.direction != Direction.NEUTRAL and score.score >= effective_min_signal_score:
            btc_volume_profile_penalty = self._btc_volume_profile_penalty(db, score, risk_value)
            if btc_volume_profile_penalty > 0:
                effective_min_signal_score += btc_volume_profile_penalty
                score.breakdown["_btc_volume_profile"][
                    "effective_min_signal_score"
                ] = effective_min_signal_score
        if int(edge_adjustment["total"]) > 0:
            score_bucket = edge_adjustment.get("score_bucket")
            score_bucket_key = f"{direction_family}:{score_bucket}"
            components = edge_adjustment.get("components", {})
            score.breakdown["_edge_calibration"] = {
                **edge_adjustment,
                "base_min_signal_score": min_signal_score,
                "effective_min_signal_score": effective_min_signal_score,
            }
            score.breakdown["_directional_calibration"] = {
                "key": directional_key,
                "adjustment": components.get("directional", 0) if isinstance(components, dict) else 0,
                "payload": risk_value.get("directional_score_adjustments", {}).get(directional_key)
                if isinstance(risk_value.get("directional_score_adjustments"), dict)
                else None,
                "effective_min_signal_score": effective_min_signal_score,
            }
            score.breakdown["_score_reliability"] = {
                "key": score_bucket_key,
                "adjustment": components.get("score_bucket", 0) if isinstance(components, dict) else 0,
                "payload": risk_value.get("score_bucket_adjustments", {}).get(score_bucket_key)
                if isinstance(risk_value.get("score_bucket_adjustments"), dict)
                else None,
                "effective_min_signal_score": effective_min_signal_score,
            }
            score.breakdown["_market_timeframe_probation"] = {
                "market_id": market.id,
                "timeframe": timeframe,
                "market_adjustment": components.get("market", 0) if isinstance(components, dict) else 0,
                "timeframe_adjustment": components.get("timeframe", 0) if isinstance(components, dict) else 0,
            }
        # Liquidation pressure penalty (kiyotaka): se le liquidazioni recenti vanno
        # *contro* la direzione del segnale (short squeeze in corso vs un SHORT,
        # long-flush vs un LONG) alziamo la soglia invece di bloccare. La fetch
        # avviene SOLO per candidati che passerebbero la soglia corrente, così le
        # chiamate restano nel budget free-tier (≈1 per candidato reale). Fail-open.
        if (
            bool(risk_value.get("liquidation_gate_enabled", False))
            and score.direction != Direction.NEUTRAL
            and score.score >= effective_min_signal_score
        ):
            liq_penalty = self._liquidation_pressure_penalty(market, score, risk_value)
            if liq_penalty > 0:
                effective_min_signal_score += liq_penalty
        if score.direction == Direction.NEUTRAL or score.score < effective_min_signal_score:
            components = edge_adjustment.get("components", {})
            skip_reason = "low_score"
            if isinstance(components, dict) and int(components.get("directional", 0)) > 0:
                skip_reason = "directional_score_filter"
            elif isinstance(components, dict) and int(components.get("score_bucket", 0)) > 0:
                skip_reason = "score_reliability_filter"
            elif int(edge_adjustment["total"]) > 0:
                skip_reason = "edge_probation_filter"
            elif btc_volume_profile_penalty > 0:
                skip_reason = "btc_volume_profile_filter"
            logger.debug(
                "signal_skipped_low_score",
                market=market.symbol,
                timeframe=timeframe,
                direction=score.direction.value,
                score=score.score,
                min_score=min_signal_score,
                effective_min_score=effective_min_signal_score,
                edge_calibration=edge_adjustment,
                reason=score.reason,
            )
            self._record_skip(skip_reason)
            db.commit()
            return None
        min_expected_r = effective_min_signal_expected_r(db)
        if min_expected_r is not None:
            calibration_table = cached_calibration_table(db)
            calibrated = calibration_table.lookup(
                direction=score.direction,
                timeframe=timeframe,
                score=score.score,
            )
            if calibrated is not None:
                score.breakdown["_expected_r_calibration"] = {
                    **serialize_metrics(calibrated),
                    "min_required": min_expected_r,
                    "computed_at": calibration_table.computed_at.isoformat(),
                }
                if (
                    calibrated.confidence != "low"
                    and calibrated.expected_r < min_expected_r
                ):
                    logger.debug(
                        "signal_skipped_low_expected_r",
                        market=market.symbol,
                        timeframe=timeframe,
                        direction=score.direction.value,
                        score=score.score,
                        expected_r=calibrated.expected_r,
                        min_expected_r=min_expected_r,
                        confidence=calibrated.confidence,
                        n_samples=calibrated.n_samples,
                    )
                    self._record_skip("low_expected_r")
                    db.commit()
                    return None
        if (
            not bool(risk_value.get("allow_ob_imbalance_only_signals", False))
            and self._is_ob_imbalance_only_signal(score.breakdown, score.direction)
        ):
            score.breakdown["_single_source_guardrail"] = {
                "reason": "ob_imbalance_only_signal_blocked",
                "allow_ob_imbalance_only_signals": False,
            }
            logger.debug(
                "signal_skipped_ob_imbalance_only",
                market=market.symbol,
                timeframe=timeframe,
                direction=score.direction.value,
                score=score.score,
                reason=score.reason,
            )
            self._record_skip("ob_imbalance_only")
            db.commit()
            return None
        if bool(risk_value.get("pre_tp1_validation_enabled", True)):
            pre_tp1_min_samples = max(
                1,
                _safe_int(risk_value.get("pre_tp1_validation_min_samples"), 30),
            )
            pre_tp1_lookback = max(
                50,
                min(2000, _safe_int(risk_value.get("pre_tp1_validation_lookback"), 500)),
            )
            pre_tp1_table = cached_pre_tp1_validation_table(
                db,
                lookback=pre_tp1_lookback,
                min_samples_for_use=pre_tp1_min_samples,
            )
            pre_tp1_passed, pre_tp1_info = evaluate_pre_tp1_entry(
                pre_tp1_table,
                direction=score.direction,
                timeframe=timeframe,
                score=score.score,
                market_id=market.id,
                strategy_breakdown=score.breakdown,
                risk_value=risk_value,
            )
            score.breakdown["_pre_tp1_validation"] = pre_tp1_info
            if not pre_tp1_passed:
                logger.debug(
                    "signal_skipped_pre_tp1_validation",
                    market=market.symbol,
                    timeframe=timeframe,
                    direction=score.direction.value,
                    score=score.score,
                    pre_tp1_validation=pre_tp1_info,
                )
                self._record_skip("pre_tp1_validation_filter")
                db.commit()
                return None
        if bool(risk_value.get("entry_quality_filter_enabled", True)):
            entry_quality_passed, entry_quality_info = self._entry_quality_result(
                candles,
                score.direction,
                risk_value,
                ob_analysis,
            )
            score.breakdown["_entry_quality"] = entry_quality_info
            if not entry_quality_passed:
                logger.debug(
                    "signal_skipped_entry_quality_filter",
                    market=market.symbol,
                    timeframe=timeframe,
                    direction=score.direction.value,
                    score=score.score,
                    entry_quality=entry_quality_info,
                )
                self._record_skip("entry_quality_filter")
                db.commit()
                return None
        if mtf_confluence_enabled:
            higher_tf = higher_timeframe(timeframe)
            if higher_tf is not None:
                higher_rows = (
                    db.query(Candle)
                    .filter(Candle.market_id == market.id, Candle.timeframe == higher_tf)
                    .order_by(Candle.open_time.desc())
                    .limit(max(mtf_ema_period * 3, 100))
                    .all()
                )
                higher_candles = [
                    CandleInput(
                        open_time=row.open_time,
                        open=float(row.open),
                        high=float(row.high),
                        low=float(row.low),
                        close=float(row.close),
                        volume=float(row.volume),
                        open_interest=float(row.open_interest) if row.open_interest is not None else None,
                    )
                    for row in reversed(higher_rows)
                ]
                mtf_passed, mtf_info = check_mtf_confluence(
                    higher_candles,
                    score.direction,
                    enabled=True,
                    ema_period=mtf_ema_period,
                )
                if mtf_info is not None:
                    score.breakdown["_mtf_confluence"] = mtf_info
                if not mtf_passed:
                    logger.debug(
                        "signal_skipped_mtf_confluence",
                        market=market.symbol,
                        timeframe=timeframe,
                        higher_timeframe=higher_tf,
                        mtf=mtf_info,
                    )
                    self._record_skip("mtf_confluence")
                    db.commit()
                    return None
        risk_plan = self.risk.build_plan(
            candles,
            score.direction,
            score.score,
            min_risk_reward,
            min_atr_multiplier=min_atr_multiplier,
            min_stop_distance_pct=min_stop_distance_pct,
            ob_analysis=ob_analysis,
        )
        if risk_plan is None or risk_plan.risk_reward < min_risk_reward:
            logger.debug(
                "signal_skipped_risk_reward",
                market=market.symbol,
                timeframe=timeframe,
                risk_reward=risk_plan.risk_reward if risk_plan else None,
                min_risk_reward=min_risk_reward,
            )
            self._record_skip("risk_reward")
            db.commit()
            return None
        if ab_overlay_payload is not None:
            score.breakdown["_auto_tuner"] = {
                "step_key": ab_overlay_payload["step_key"],
                "arm": ab_overlay_payload["arm"],
                "assignment_method": ab_overlay_payload.get("assignment_method"),
            }
        # Segnale confermato: ora persistiamo lo snapshot degli indicatori delle
        # strategie (un'unica riga per la candela sorgente), committato insieme al
        # Signal nello stesso commit sottostante.
        self._persist_indicators(db, market, timeframe, latest_candle_open_time, results)
        signal = Signal(
            market_id=market.id,
            timeframe=timeframe,
            direction=score.direction.value,
            score=score.score,
            confidence_label=score.confidence_label,
            entry_price=Decimal(str(risk_plan.entry)),
            stop_loss=Decimal(str(risk_plan.stop_loss)),
            take_profit_1=Decimal(str(risk_plan.take_profit_1)),
            take_profit_2=Decimal(str(risk_plan.take_profit_2)),
            take_profit_3=Decimal(str(risk_plan.take_profit_3)) if risk_plan.take_profit_3 else None,
            risk_reward=Decimal(str(risk_plan.risk_reward)),
            reason=score.reason,
            strategy_breakdown=score.breakdown,
            source_candle_open_time=latest_candle_open_time,
            status=SignalStatus.NEW.value,
        )
        db.add(signal)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info(
                "signal_creation_race_lost",
                market=market.symbol,
                timeframe=timeframe,
            )
            return (
                db.query(Signal)
                .filter(
                    Signal.market_id == market.id,
                    Signal.timeframe == timeframe,
                    Signal.source_candle_open_time == latest_candle_open_time,
                )
                .first()
            )
        db.refresh(signal)
        return signal

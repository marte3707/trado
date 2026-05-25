from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import Direction, SignalStatus
from app.db.models import AppSetting, Candle, Indicator, Market, Signal, StrategyConfig
from app.engine.auto_tuner import active_ab_overlay
from app.engine.mtf import check_mtf_confluence, higher_timeframe
from app.engine.orderbook import OrderBookAnalysis, analyze_order_book, ob_imbalance_result
from app.engine.performance import SignalPerformanceAnalyzer
from app.engine.regime import apply_regime_filter
from app.engine.risk import RiskManager
from app.engine.scoring import SignalScoringEngine
from app.engine.volatility import evaluate_volatility
from app.services.coinmarketcap import (
    AltcoinSeasonIndex,
    CoinMarketCapClient,
    FearGreedIndex,
    MarketQuote,
)
from app.services.hyperliquid import fetch_l2_book, fetch_predicted_fundings
from app.services.whale_alert import WhaleAlertClient, WhaleFlow
from app.strategies.base import CandleInput, StrategyContext
from app.strategies.base import StrategyResult
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


class SignalEngine:
    def __init__(self) -> None:
        self.registry = strategy_registry()
        self.scoring = SignalScoringEngine()
        self.risk = RiskManager()
        self.performance = SignalPerformanceAnalyzer()
        settings = get_settings()
        self.market_data = CoinMarketCapClient(settings)
        self.whales = WhaleAlertClient(settings)
        # Cache valida per la durata di un worker tick: il worker chiama
        # `begin_cycle()` ad ogni nuovo tick per invalidare. Evita chiamate HTTP
        # ripetute (fundings/orderbook) per market × timeframe nello stesso tick.
        self._cycle_fundings: dict[str, float] | None = None
        self._cycle_l2_cache: dict[str, Any] = {}
        # Aggregated skip-reason counter per cycle. In debug-log mode i singoli
        # "signal_skipped_*" non si vedono in produzione; questo conta dove muoiono
        # i segnali per esporre un breakdown nel `worker_cycle` event.
        self._cycle_skip_counts: dict[str, int] = {}

    def begin_cycle(self) -> None:
        """Invalidate per-cycle caches at the start of each worker tick."""
        self._cycle_fundings = None
        self._cycle_l2_cache = {}
        self._cycle_skip_counts = {}

    def cycle_skip_counts(self) -> dict[str, int]:
        """Snapshot dei contatori di skip aggregati per il tick in corso."""
        return dict(self._cycle_skip_counts)

    def _record_skip(self, reason: str) -> None:
        self._cycle_skip_counts[reason] = self._cycle_skip_counts.get(reason, 0) + 1

    @staticmethod
    def _average_quote_volume(candles: list[CandleInput], window: int = 20) -> float:
        if not candles:
            return 0.0
        recent = candles[-window:]
        return sum(candle.close * candle.volume for candle in recent) / len(recent)

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
        db.query(Indicator).filter(
            Indicator.market_id == market.id,
            Indicator.timeframe == timeframe,
            Indicator.timestamp == timestamp,
        ).delete(synchronize_session=False)
        for result in results:
            db.add(
                Indicator(
                    market_id=market.id,
                    timeframe=timeframe,
                    timestamp=timestamp,
                    indicator_name=result.strategy_name,
                    values={
                        **result.indicator_values,
                        "_direction": result.direction.value,
                        "_confidence": result.confidence,
                        "_reason": result.reason,
                    },
                )
            )

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
        ab_overlay_payload = active_ab_overlay(db)
        ab_overlay: dict[str, Any] = (
            dict(ab_overlay_payload.get("overlay", {})) if ab_overlay_payload else {}
        )
        risk_value: dict[str, Any] = (
            dict(risk_settings.value) if risk_settings and isinstance(risk_settings.value, dict) else {}
        )
        if ab_overlay:
            risk_value.update(ab_overlay)
        min_signal_score = int(risk_value.get("min_signal_score", min_signal_score))
        min_risk_reward = float(risk_value.get("min_risk_reward", min_risk_reward))
        min_average_quote_volume_usd = float(
            risk_value.get("min_average_quote_volume_usd", min_average_quote_volume_usd)
        )
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
        regime_method_raw = str(risk_value.get("regime_method", "adx")).lower()
        if regime_method_raw not in ("adx", "hurst", "garch", "hybrid"):
            regime_method_raw = "adx"
        regime_adx_trending = float(risk_value.get("regime_adx_trending", 25.0))
        regime_adx_ranging = float(risk_value.get("regime_adx_ranging", 18.0))
        regime_confirm_candles = int(risk_value.get("regime_confirm_candles", 2))
        regime_off_multiplier = float(risk_value.get("regime_off_weight_multiplier", 0.5))
        regime_hurst_trending = float(risk_value.get("regime_hurst_trending", 0.55))
        regime_hurst_ranging = float(risk_value.get("regime_hurst_ranging", 0.45))
        regime_hurst_min_lag = int(risk_value.get("regime_hurst_min_lag", 2))
        regime_hurst_max_lag = int(risk_value.get("regime_hurst_max_lag", 20))
        regime_garch_window = int(risk_value.get("regime_garch_window", 50))
        regime_garch_lag = int(risk_value.get("regime_garch_lag", 1))
        regime_garch_cluster_threshold = float(
            risk_value.get("regime_garch_cluster_threshold", 0.2)
        )
        participation_ratio = float(risk_value.get("participation_ratio", 0.35))
        volatility_filter_enabled = bool(risk_value.get("volatility_filter_enabled", False))
        # Default 25-90: 10-95 era 85% della distribuzione — filtro decorativo. 25-90
        # taglia effettivamente mercati morti (sotto p25) e panic moves (sopra p90).
        atr_pct_min = float(risk_value.get("atr_pct_min", 0.25))
        atr_pct_max = float(risk_value.get("atr_pct_max", 0.90))
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

        latest_candle_open_time = rows[0].open_time
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
            .filter(StrategyConfig.timeframe == timeframe, StrategyConfig.enabled.is_(True))
            .all()
        )
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
        for config in configs:
            strategy = self.registry.get(config.strategy_name)
            if strategy is None:
                continue
            results.append(strategy.evaluate(context, config.params))
            weights[config.strategy_name] = config.weight
            calibrated_weights[config.strategy_name] = config.weight
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
            ob_result = ob_imbalance_result(context, ob_analysis)
            if ob_result is not None:
                results.append(ob_result)
                weights[ob_result.strategy_name] = max(0, settings.ob_imbalance_weight)
        self._persist_indicators(db, market, timeframe, latest_candle_open_time, results)

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
            method=regime_method_raw,  # type: ignore[arg-type]
            adx_trending=regime_adx_trending,
            adx_ranging=regime_adx_ranging,
            confirm_candles=regime_confirm_candles,
            off_regime_weight_multiplier=regime_off_multiplier,
            hurst_trending=regime_hurst_trending,
            hurst_ranging=regime_hurst_ranging,
            hurst_min_lag=regime_hurst_min_lag,
            hurst_max_lag=regime_hurst_max_lag,
            garch_window=regime_garch_window,
            garch_lag=regime_garch_lag,
            garch_cluster_threshold=regime_garch_cluster_threshold,
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
        if score.direction == Direction.NEUTRAL or score.score < min_signal_score:
            logger.debug(
                "signal_skipped_low_score",
                market=market.symbol,
                timeframe=timeframe,
                direction=score.direction.value,
                score=score.score,
                min_score=min_signal_score,
                reason=score.reason,
            )
            self._record_skip("low_score")
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
            }
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

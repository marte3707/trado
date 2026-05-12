from decimal import Decimal

import structlog
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import Direction, SignalStatus
from app.db.models import AppSetting, Candle, Indicator, Market, Signal, StrategyConfig
from app.engine.performance import SignalPerformanceAnalyzer
from app.engine.risk import RiskManager
from app.engine.scoring import SignalScoringEngine
from app.services.coinmarketcap import (
    AltcoinSeasonIndex,
    CoinMarketCapClient,
    FearGreedIndex,
    MarketQuote,
)
from app.services.whale_alert import WhaleAlertClient, WhaleFlow
from app.strategies.base import CandleInput, StrategyContext
from app.strategies.base import StrategyResult
from app.strategies.registry import strategy_registry

logger = structlog.get_logger()


class SignalEngine:
    def __init__(self) -> None:
        self.registry = strategy_registry()
        self.scoring = SignalScoringEngine()
        self.risk = RiskManager()
        self.performance = SignalPerformanceAnalyzer()
        settings = get_settings()
        self.market_data = CoinMarketCapClient(settings)
        self.whales = WhaleAlertClient(settings)

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
            return None

        risk_settings = db.get(AppSetting, "risk")
        min_signal_score = settings.min_signal_score
        min_risk_reward = settings.min_risk_reward
        min_average_quote_volume_usd = settings.min_average_quote_volume_usd
        if risk_settings:
            min_signal_score = int(
                risk_settings.value.get(
                    "min_signal_score",
                    min_signal_score,
                )
            )
            min_risk_reward = float(
                risk_settings.value.get(
                    "min_risk_reward",
                    min_risk_reward,
                )
            )
            min_average_quote_volume_usd = float(
                risk_settings.value.get(
                    "min_average_quote_volume_usd",
                    min_average_quote_volume_usd,
                )
            )
        global_quote = self._global_volume_quote(market)
        if min_average_quote_volume_usd > 0:
            liquidity_window = candles[-20:]
            hyperliquid_average_quote_volume = sum(candle.close * candle.volume for candle in liquidity_window) / len(
                liquidity_window
            )
            liquidity_quote_volume = (
                global_quote.volume_24h_usd
                if global_quote and global_quote.volume_24h_usd is not None
                else hyperliquid_average_quote_volume
            )
            if liquidity_quote_volume < min_average_quote_volume_usd:
                logger.debug(
                    "signal_skipped_low_liquidity",
                    market=market.symbol,
                    timeframe=timeframe,
                    global_quote_volume_24h_usd=global_quote.volume_24h_usd if global_quote else None,
                    hyperliquid_average_quote_volume=hyperliquid_average_quote_volume,
                    min_average_quote_volume_usd=min_average_quote_volume_usd,
                )
                return None

        latest_candle_open_time = rows[0].open_time
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
        for config in configs:
            strategy = self.registry.get(config.strategy_name)
            if strategy is None:
                continue
            results.append(strategy.evaluate(context, config.params))
            weights[config.strategy_name] = config.weight
        fear_greed = self.market_data.latest_fear_greed()
        fear_greed_result = self._fear_greed_result(context, fear_greed)
        if fear_greed_result is not None:
            results.append(fear_greed_result)
            weights[fear_greed_result.strategy_name] = max(0, settings.fear_greed_weight)
        altcoin_season = self.market_data.latest_altcoin_season()
        altcoin_season_result = self._altcoin_season_result(context, altcoin_season, market.base)
        if altcoin_season_result is not None:
            results.append(altcoin_season_result)
            weights[altcoin_season_result.strategy_name] = max(0, settings.altcoin_season_weight)
        whale_flow = self.whales.recent_flow(market.base)
        whale_flow_result = self._whale_flow_result(context, whale_flow)
        if whale_flow_result is not None:
            results.append(whale_flow_result)
            weights[whale_flow_result.strategy_name] = max(0, settings.whale_flow_weight)
        self._persist_indicators(db, market, timeframe, latest_candle_open_time, results)
        existing_signal = (
            db.query(Signal)
            .filter(
                Signal.market_id == market.id,
                Signal.timeframe == timeframe,
                Signal.created_at >= latest_candle_open_time,
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
            db.commit()
            return None

        weights, performance_notes = self.performance.adjusted_weights(
            db,
            weights,
            timeframe=timeframe,
            market_id=market.id,
        )
        score = self.scoring.score(results, weights)
        if performance_notes:
            score.breakdown["_performance_adjustments"] = performance_notes
        score.breakdown["_market_context"] = {
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
            db.commit()
            return None
        min_risk_reward = max(min_risk_reward, 2.0)
        risk_plan = self.risk.build_plan(candles, score.direction, score.score, min_risk_reward)
        if risk_plan is None or risk_plan.risk_reward < min_risk_reward:
            logger.debug(
                "signal_skipped_risk_reward",
                market=market.symbol,
                timeframe=timeframe,
                risk_reward=risk_plan.risk_reward if risk_plan else None,
                min_risk_reward=min_risk_reward,
            )
            db.commit()
            return None
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
            status=SignalStatus.NEW.value,
        )
        db.add(signal)
        db.commit()
        db.refresh(signal)
        return signal

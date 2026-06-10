from app.strategies.base import Strategy
from app.strategies.bollinger_strategy import BollingerBandsStrategy
from app.strategies.candlestick_strategy import CandlestickPatternStrategy
from app.strategies.cvd_divergence_strategy import CvdDivergenceStrategy
from app.strategies.ema_crossover import EmaCrossoverStrategy
from app.strategies.fibonacci_strategy import FibonacciStrategy
from app.strategies.liquidation_cascade_strategy import LiquidationCascadeStrategy
from app.strategies.macd_strategy import MacdStrategy
from app.strategies.oi_trend_strategy import OITrendStrategy
from app.strategies.rsi_strategy import RsiStrategy
from app.strategies.supertrend_strategy import SupertrendStrategy
from app.strategies.support_resistance_strategy import SupportResistanceStrategy
from app.strategies.volume_profile_strategy import VolumeProfileStrategy
from app.strategies.volume_spike import VolumeSpikeStrategy
from app.strategies.vwap_strategy import VwapStrategy
from app.strategies.wyckoff_strategy import WyckoffSpringUpthrustStrategy


def strategy_registry() -> dict[str, Strategy]:
    strategies: list[Strategy] = [
        EmaCrossoverStrategy(),
        RsiStrategy(),
        MacdStrategy(),
        VolumeSpikeStrategy(),
        FibonacciStrategy(),
        SupportResistanceStrategy(),
        SupertrendStrategy(),
        BollingerBandsStrategy(),
        VwapStrategy(),
        CandlestickPatternStrategy(),
        WyckoffSpringUpthrustStrategy(),
        OITrendStrategy(),
        CvdDivergenceStrategy(),
        LiquidationCascadeStrategy(),
        VolumeProfileStrategy(),
    ]
    return {strategy.name: strategy for strategy in strategies}

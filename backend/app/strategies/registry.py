from app.strategies.base import Strategy
from app.strategies.bollinger_strategy import BollingerBandsStrategy
from app.strategies.candlestick_strategy import CandlestickPatternStrategy
from app.strategies.ema_crossover import EmaCrossoverStrategy
from app.strategies.fibonacci_strategy import FibonacciStrategy
from app.strategies.macd_strategy import MacdStrategy
from app.strategies.rsi_strategy import RsiStrategy
from app.strategies.supertrend_strategy import SupertrendStrategy
from app.strategies.support_resistance_strategy import SupportResistanceStrategy
from app.strategies.vwap_strategy import VwapStrategy
from app.strategies.volume_spike import VolumeSpikeStrategy
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
    ]
    return {strategy.name: strategy for strategy in strategies}

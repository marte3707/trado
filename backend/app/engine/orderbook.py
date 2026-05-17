from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import Direction
from app.services.hyperliquid import L2BookSnapshot
from app.strategies.base import StrategyContext, StrategyResult

# OBI: al di sotto di questa soglia il segnale è troppo rumoroso per essere utile
_OBI_NEUTRAL_THRESHOLD = 0.10
# Un livello è considerato "muro" se la sua dimensione è >= N volte la media
_WALL_MULTIPLIER = 3.0
# Ricerca muri entro questa distanza percentuale dal prezzo di entry
_WALL_SEARCH_PCT = 0.05


@dataclass(frozen=True)
class WallLevel:
    price: float
    size: float
    side: str  # "bid" | "ask"


@dataclass(frozen=True)
class OrderBookAnalysis:
    obi: float          # Order Book Imbalance [-1, 1]: >0 = pressione long, <0 = short
    spread_pct: float   # spread bid-ask come frazione del mid price
    bid_volume: float   # volume cumulato top-N livelli bid
    ask_volume: float   # volume cumulato top-N livelli ask
    bid_walls: list[WallLevel]  # muri bid significativi sotto l'entry
    ask_walls: list[WallLevel]  # muri ask significativi sopra l'entry


def analyze_order_book(
    snapshot: L2BookSnapshot,
    entry: float,
    n_levels: int = 10,
    wall_multiplier: float = _WALL_MULTIPLIER,
    wall_search_pct: float = _WALL_SEARCH_PCT,
) -> OrderBookAnalysis:
    bids = snapshot.bids[:n_levels]
    asks = snapshot.asks[:n_levels]

    bid_volume = sum(level.size for level in bids)
    ask_volume = sum(level.size for level in asks)
    total = bid_volume + ask_volume
    obi = (bid_volume - ask_volume) / total if total > 0 else 0.0

    spread = asks[0].price - bids[0].price if bids and asks else 0.0
    mid = (asks[0].price + bids[0].price) / 2 if bids and asks else entry
    spread_pct = spread / mid if mid > 0 else 0.0

    search_range = entry * wall_search_pct
    lower_bound = entry - search_range
    upper_bound = entry + search_range

    all_bids = snapshot.bids
    all_asks = snapshot.asks

    avg_bid = sum(level.size for level in all_bids) / len(all_bids) if all_bids else 0.0
    avg_ask = sum(level.size for level in all_asks) / len(all_asks) if all_asks else 0.0

    bid_walls = [
        WallLevel(price=level.price, size=level.size, side="bid")
        for level in all_bids
        if level.size >= avg_bid * wall_multiplier and lower_bound <= level.price < entry
    ]
    ask_walls = [
        WallLevel(price=level.price, size=level.size, side="ask")
        for level in all_asks
        if level.size >= avg_ask * wall_multiplier and entry < level.price <= upper_bound
    ]

    return OrderBookAnalysis(
        obi=obi,
        spread_pct=spread_pct,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        bid_walls=bid_walls,
        ask_walls=ask_walls,
    )


def ob_imbalance_result(
    context: StrategyContext,
    analysis: OrderBookAnalysis,
    obi_threshold: float = _OBI_NEUTRAL_THRESHOLD,
) -> StrategyResult | None:
    obi = analysis.obi
    if abs(obi) < obi_threshold:
        return None

    if obi > 0:
        direction = Direction.LONG
        reason = (
            f"OB pressure LONG: OBI={obi:.2f} "
            f"(bid {analysis.bid_volume:.2f} vs ask {analysis.ask_volume:.2f})"
        )
    else:
        direction = Direction.SHORT
        reason = (
            f"OB pressure SHORT: OBI={obi:.2f} "
            f"(bid {analysis.bid_volume:.2f} vs ask {analysis.ask_volume:.2f})"
        )

    confidence = min(70, int(30 + abs(obi) * 100))

    return StrategyResult(
        strategy_name="ob_imbalance",
        market=context.market_type,
        symbol=context.symbol,
        timeframe=context.timeframe,
        direction=direction,
        confidence=confidence,
        reason=reason,
        indicator_values={
            "obi": round(obi, 4),
            "spread_pct": round(analysis.spread_pct * 100, 4),
            "bid_volume": round(analysis.bid_volume, 4),
            "ask_volume": round(analysis.ask_volume, 4),
            "bid_wall_count": len(analysis.bid_walls),
            "ask_wall_count": len(analysis.ask_walls),
            "source": "hyperliquid_l2",
        },
        suggested_entry=context.candles[-1].close if context.candles else None,
    )

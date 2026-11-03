"""Donchian-ensemble trend following, written as a vibetrading strategy.

Reference: Zarattini, Pagani & Barbon (2025), "Catching Crypto Trends: A
Tactical Approach for Bitcoin and Altcoins", Swiss Finance Institute RP 25-80.

Rules (long-only, daily bars, evaluated on closed candles only):
- For every lookback N in LOOKBACKS a sub-model goes long when the close breaks
  the highest close of the previous N days.
- Each sub-model exits on a trailing stop placed at the midpoint of the N-day
  channel; the stop only ratchets up.
- Signal per asset = fraction of sub-models that are long (0..1).
- Target weight per asset = signal * min(MAX_LEV, TARGET_VOL / realized_vol) / n_assets.
- Rebalance only when the weight drift exceeds REBAL_BAND (limits fees).
"""

STRATEGY_TEMPLATE = '''
import math
from vibetrading import (
    vibe, get_futures_ohlcv, get_perp_summary,
    get_perp_position, set_leverage, long, reduce_position,
)

ASSETS = {assets}
LOOKBACKS = {lookbacks}
TARGET_VOL = {target_vol}
VOL_WINDOW = {vol_window}
MAX_LEV = {max_lev}
REBAL_BAND = {rebal_band}

STATE = {{a: {{n: {{"on": False, "stop": 0.0}} for n in LOOKBACKS}} for a in ASSETS}}


def _target_weight(asset, close):
    price = float(close.iloc[-1])
    prior = close.iloc[:-1]
    active = 0
    for n in LOOKBACKS:
        window = prior.iloc[-n:]
        upper = float(window.max())
        mid = (upper + float(window.min())) / 2.0
        st = STATE[asset][n]
        if st["on"]:
            st["stop"] = max(st["stop"], mid)
            if price < st["stop"]:
                st["on"] = False
        elif price > upper:
            st["on"] = True
            st["stop"] = mid
        active += 1 if st["on"] else 0
    signal = active / len(LOOKBACKS)
    vol = float(close.pct_change().iloc[-VOL_WINDOW:].std()) * math.sqrt(365)
    lev = min(MAX_LEV, TARGET_VOL / vol) if vol > 0 else 0.0
    return signal * lev / len(ASSETS)


@vibe(interval="1d")
def donchian_ensemble():
    summary = get_perp_summary()
    equity = float(summary.get("account_value", 0.0))
    if equity <= 0:
        return
    for asset in ASSETS:
        hist = get_futures_ohlcv(asset, "1d", max(LOOKBACKS) + 2)
        if hist is None or len(hist) < max(LOOKBACKS) + 2 or hist["close"].isna().any():
            continue
        close = hist["close"]
        price = float(close.iloc[-1])
        target_w = _target_weight(asset, close)
        pos = get_perp_position(asset)
        size = float(pos.get("size", 0.0)) if pos else 0.0
        cur_w = size * price / equity
        if target_w == 0.0 and size > 0:
            reduce_position(asset, size)
            continue
        if abs(target_w - cur_w) < REBAL_BAND / len(ASSETS):
            continue
        set_leverage(asset, 3)
        delta_qty = (target_w - cur_w) * equity / price
        if delta_qty > 0:
            long(asset, delta_qty, price, order_type="market")
        elif size > 0:
            reduce_position(asset, min(size, -delta_qty))
'''

DEFAULTS: dict = {
    "assets": ["BTC"],
    "lookbacks": [5, 10, 20, 30, 60, 90, 150, 250, 360],
    "target_vol": 0.25,
    "vol_window": 90,
    "max_lev": 2.0,
    "rebal_band": 0.10,
}


def generate(**overrides) -> str:
    return STRATEGY_TEMPLATE.format(**{**DEFAULTS, **overrides})

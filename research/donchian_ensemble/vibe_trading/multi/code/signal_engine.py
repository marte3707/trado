"""Donchian-ensemble trend following (Zarattini, Pagani & Barbon 2025)."""
import math
from typing import Dict

import numpy as np
import pandas as pd

LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
TARGET_VOL = 0.25
VOL_WINDOW = 90
MAX_WEIGHT = 1.0


class SignalEngine:
    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        out = {}
        n_assets = len(data_map)
        for code, df in data_map.items():
            close = df["close"].astype(float).to_numpy()
            signal = np.zeros(len(close))
            on = {n: False for n in LOOKBACKS}
            stop = {n: 0.0 for n in LOOKBACKS}
            for i in range(max(LOOKBACKS), len(close)):
                active = 0
                for n in LOOKBACKS:
                    window = close[i - n:i]
                    upper = window.max()
                    mid = (upper + window.min()) / 2.0
                    if on[n]:
                        stop[n] = max(stop[n], mid)
                        if close[i] < stop[n]:
                            on[n] = False
                    elif close[i] > upper:
                        on[n] = True
                        stop[n] = mid
                    active += on[n]
                signal[i] = active / len(LOOKBACKS)
            rets = pd.Series(close).pct_change()
            vol = rets.rolling(VOL_WINDOW).std().to_numpy() * math.sqrt(365)
            with np.errstate(divide="ignore", invalid="ignore"):
                lev = np.where(vol > 0, np.minimum(MAX_WEIGHT, TARGET_VOL / vol), 0.0)
            weight = np.nan_to_num(signal * lev) / n_assets
            out[code] = pd.Series(weight, index=df.index)
        return out

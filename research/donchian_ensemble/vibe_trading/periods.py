"""Per-period and per-year stats from a Vibe-Trading artifacts/equity.csv."""
import math
import sys

import pandas as pd

eq = pd.read_csv(sys.argv[1], parse_dates=["timestamp"]).set_index("timestamp")


def st(s: pd.Series) -> str:
    r = s.pct_change().dropna()
    years = (s.index[-1] - s.index[0]).days / 365.25
    cagr = ((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1) * 100
    sharpe = r.mean() / r.std() * math.sqrt(365) if r.std() > 0 else 0.0
    dd = (s / s.cummax() - 1).min() * 100
    return f"CAGR {cagr:6.1f}%  Sharpe {sharpe:5.2f}  MaxDD {dd:6.1f}%"


first, last = eq.index[0].year, eq.index[-1].year
periods = [(str(first), str(last)), ("2016", "2020"), ("2021", str(last)), ("2024", str(last))]
periods += [(str(y), str(y)) for y in range(first, last + 1)]
for a, b in periods:
    s = eq.loc[a:b]
    if len(s) < 30:
        continue
    label = a if a == b else f"{a}-{b}"
    print(f"{label:9}  strat {st(s['equity'])} | B&H {st(s['benchmark_equity'])}")

"""Cross-sectional momentum on Hyperliquid perps (after Liu, Tsyvinski & Wu 2022, JF).

Every REBAL_DAYS (Monday 00:00 UTC by default) rank coins by trailing LOOKBACK-day
return, skipping the most recent SKIP days. Go long the top K and (optionally) short
the bottom K, equal weight, gross exposure GROSS. Positions are perps, so real
hourly funding is applied (longs pay, shorts receive). Decisions use only closed
daily candles: weights chosen at the close of day t earn the return of day t+1.

    python xs_momentum_backtest.py --data ../funding_carry/data
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, replace
from pathlib import Path

import warnings

import numpy as np
import pandas as pd


warnings.filterwarnings("ignore", category=DeprecationWarning)


@dataclass(frozen=True)
class Config:
    name: str
    lookback: int = 21
    skip: int = 0
    k: int = 5
    long_short: bool = True
    rebal_days: int = 7
    gross: float = 1.0
    min_history: int = 60          # a coin must have this many days of data to be ranked
    universe: int = 40             # rank only the N most traded coins (trailing 30d $ volume) at each rebalance
    fee: float = 0.00045           # Hyperliquid perp taker
    slip: float = 0.0010           # per side, alts
    vol_target: float | None = None


BASELINE_DAILY_FUNDING = 0.0000125 * 24      # Hyperliquid baseline (~11% APR) when no data was exported


def load(data: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    c = pd.read_csv(data / "candles_daily.csv", parse_dates=["date"])
    close = c.pivot_table(index="date", columns="coin", values="close").sort_index()
    dollar_vol = (c.assign(dv=c["close"] * c["volume"])
                  .pivot_table(index="date", columns="coin", values="dv").reindex_like(close))
    funding = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    fpath = data / "funding_hourly.csv"
    if fpath.exists() and fpath.stat().st_size > 100:
        f = pd.read_csv(fpath, parse_dates=["time_utc"])
        f["date"] = (f["time_utc"] - pd.Timedelta(hours=1)).dt.floor("D")   # settlement at T pays hour T-1
        funding = (f.pivot_table(index="date", columns="coin", values="funding_rate", aggfunc="sum")
                   .reindex(index=close.index, columns=close.columns))
    has_data = funding.notna().any()
    funding.loc[:, ~has_data] = BASELINE_DAILY_FUNDING
    return close, funding.fillna(0.0), dollar_vol


def weights(cfg: Config, close: pd.DataFrame, dollar_vol: pd.DataFrame) -> pd.DataFrame:
    ret = close.shift(cfg.skip) / close.shift(cfg.skip + cfg.lookback) - 1
    history = close.notna().cumsum()
    liquidity = dollar_vol.rolling(30, min_periods=20).mean()
    w = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    rebal_days = close.index[close.index.dayofweek == 0] if cfg.rebal_days == 7 else close.index[:: cfg.rebal_days]
    for day in rebal_days:
        eligible = (history.loc[day] >= cfg.min_history) & close.loc[day].notna()
        liquid = liquidity.loc[day][eligible].dropna().nlargest(cfg.universe).index
        score = ret.loc[day][liquid].dropna()
        row = pd.Series(0.0, index=close.columns)
        if cfg.k <= 0 and len(score):                 # benchmark: equal weight in the whole liquid universe
            row[score.index] = cfg.gross / len(score)
        elif cfg.k > 0 and len(score) >= 2 * cfg.k or cfg.k > 0 and (not cfg.long_short and len(score) >= cfg.k):
            ranked = score.sort_values()
            if cfg.long_short:
                row[ranked.index[-cfg.k:]] = cfg.gross / (2 * cfg.k)
                row[ranked.index[: cfg.k]] = -cfg.gross / (2 * cfg.k)
            else:
                row[ranked.index[-cfg.k:]] = cfg.gross / cfg.k
        w.loc[day] = row
    return w.ffill().fillna(0.0)


def run(cfg: Config, close: pd.DataFrame, funding: pd.DataFrame, dollar_vol: pd.DataFrame) -> pd.DataFrame:
    w = weights(cfg, close, dollar_vol)
    rets = close.pct_change().fillna(0.0)
    held = w.shift(1).fillna(0.0)                       # decided at close t, earns day t+1
    if cfg.vol_target:
        gross_ret = (held * rets).sum(axis=1)
        vol = gross_ret.rolling(60, min_periods=20).std().shift(1) * math.sqrt(365)
        scale = (cfg.vol_target / vol).clip(upper=2.0).fillna(1.0)
        held = held.mul(scale, axis=0)
    pnl = (held * rets).sum(axis=1)
    fund = -(held * funding).sum(axis=1)                # long pays positive funding
    turnover = held.diff().abs().sum(axis=1).fillna(held.abs().sum(axis=1))
    cost = turnover * (cfg.fee + cfg.slip)
    out = pd.DataFrame({"price": pnl, "funding": fund, "cost": -cost})
    out["net"] = out.sum(axis=1)
    out["equity"] = (1 + out["net"]).cumprod()
    out["gross_exposure"] = held.abs().sum(axis=1)
    return out


def perf(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    years = len(r) / 365
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else 0.0
    sharpe = r.mean() / r.std() * math.sqrt(365) if r.std() > 0 else 0.0
    return {"cagr": cagr, "sharpe": sharpe, "max_dd": float((eq / eq.cummax() - 1).min())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="../funding_carry/data")
    ap.add_argument("--start", default="2023-09-01", help="first evaluated day (earlier data is warm-up)")
    ap.add_argument("--out")
    args = ap.parse_args()
    close, funding, dollar_vol = load(Path(args.data))

    base = Config(name="")
    configs = [
        replace(base, name="L/S top-bottom 5, 21d"),
        replace(base, name="L/S 5, 7d"),
        replace(base, name="L/S 5, 14d"),
        replace(base, name="L/S 5, 28d"),
        replace(base, name="L/S 5, 56d"),
        replace(base, name="L/S 3, 21d", k=3),
        replace(base, name="L/S 5, 21d, skip 1d", skip=1),
        replace(base, name="Long-only top 5, 21d", long_short=False),
        replace(base, name="L/S 5, 21d, 2x slippage", slip=0.0020),
        replace(base, name="L/S 5, 21d, top-20 liquid only", universe=20),
    ]
    configs[1] = replace(configs[1], lookback=7)
    configs[2] = replace(configs[2], lookback=14)
    configs[3] = replace(configs[3], lookback=28)
    configs[4] = replace(configs[4], lookback=56)

    # benchmark: equal-weight long of the same liquid universe, weekly
    bench_cfg = replace(base, long_short=False, k=0, lookback=1)
    ew = run(bench_cfg, close, funding * 0, dollar_vol).loc[args.start:, "price"]
    lines = ["| Config | CAGR | Sharpe | Max DD | Price P&L/yr | Funding/yr | Costs/yr | " +
             " | ".join(str(y) for y in sorted(set(ew.index.year))) + " |",
             "|---|---|---|---|---|---|---|" + "---|" * len(set(ew.index.year))]
    rows = [(f"Equal-weight long, top-{base.universe} liquid (benchmark)", pd.DataFrame({"net": ew, "price": ew, "funding": 0.0, "cost": 0.0}))]
    rows += [(c.name, run(c, close, funding, dollar_vol).loc[args.start:]) for c in configs]
    for name, df in rows:
        p = perf(df["net"])
        yrs = len(df) / 365
        per_year = []
        for y in sorted(set(ew.index.year)):
            r = df["net"][df.index.year == y]
            per_year.append(f"{((1 + r).prod() - 1) * 100:+.1f}%" if len(r) > 20 else "")
        lines.append(
            f"| {name} | {p['cagr']*100:.1f}% | {p['sharpe']:.2f} | {p['max_dd']*100:.1f}% | "
            f"{df['price'].sum()/yrs*100:+.1f}% | {df['funding'].sum()/yrs*100:+.1f}% | {df['cost'].sum()/yrs*100:+.1f}% | "
            + " | ".join(per_year) + " |")
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    main()

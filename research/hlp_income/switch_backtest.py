"""HLP by default, BTC+ETH funding carry when trailing 7-day funding is high.

Uses HLP interval returns (hlp_analysis.interval_returns, conservative denominator)
and the carry backtest from research/funding_carry. At the start of each HLP interval
the regime is read (no lookahead); if both BTC and ETH 7-day funding exceed ENTRY_APR
the interval earns the carry return instead, minus a switch cost on each change.

    python switch_backtest.py --hlp ./data --funding ../funding_carry/data
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "funding_carry"))
sys.path.insert(0, str(HERE))
import carry_backtest as cb  # noqa: E402
import hlp_analysis as ha  # noqa: E402

SWITCH_COST = 0.0045      # carry round trip on both legs (fees + slippage), charged per switch


def hlp_intervals(path: Path) -> pd.DataFrame:
    h = pd.read_csv(path / "hlp_history.csv", parse_dates=["time_utc"])
    a = h[h["period"] == "allTime"].sort_values("time_utc").reset_index(drop=True)
    a = a[pd.to_numeric(a["account_value"]) > 0].reset_index(drop=True)
    av = pd.to_numeric(a["account_value"])
    r = pd.DataFrame({
        "start": a["time_utc"].shift(1), "end": a["time_utc"],
        "ret": pd.to_numeric(a["pnl"]).diff() / pd.concat([av, av.shift(1)], axis=1).max(axis=1),
        "av0": av.shift(1),
    }).dropna()
    return r[r["av0"] > 1e6].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hlp", default="./data")
    ap.add_argument("--funding", default="../funding_carry/data")
    ap.add_argument("--entry-apr", type=float, default=0.15)
    args = ap.parse_args()

    rate, prem, px = cb.load(Path(args.funding))
    carry = cb.run(replace(cb.Config(name="carry"), coins=("BTC", "ETH"), max_positions=2, always_on=True),
                   rate, prem, px)["curve"]["equity"]
    signal = (rate[["BTC", "ETH"]].resample("D").mean() * 24 * 365).rolling(7).mean()

    r = hlp_intervals(Path(args.hlp))
    r = r[r["start"] >= carry.index[0]].reset_index(drop=True)
    rows, prev_on = [], False
    for _, iv in r.iterrows():
        day = iv["start"].normalize()
        sig = signal.loc[:day - pd.Timedelta(days=1)].iloc[-1]          # known before the interval
        on = bool((sig >= args.entry_apr).all())
        c0 = carry.loc[:iv["start"]].iloc[-1]
        c1 = carry.loc[:iv["end"]].iloc[-1]
        ret = (c1 / c0 - 1) if on else iv["ret"]
        if on != prev_on:
            ret -= SWITCH_COST / 2
        rows.append({"end": iv["end"], "hlp": iv["ret"], "carry": c1 / c0 - 1, "switch": ret, "on": on})
        prev_on = on
    df = pd.DataFrame(rows).set_index("end")

    print(f"{df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}, carry active in {df['on'].mean()*100:.0f}% of intervals")
    print("| Year | HLP | Carry BTC+ETH | Switcher |\n|---|---|---|---|")
    for y, g in df.groupby(df.index.year):
        f = lambda c: f"{((1 + g[c]).prod() - 1) * 100:+.1f}%"  # noqa: E731
        print(f"| {y} | {f('hlp')} | {f('carry')} | {f('switch')} |")
    for c in ("hlp", "carry", "switch"):
        eq = (1 + df[c]).cumprod()
        print(f"{c:7} total {(eq.iloc[-1]-1)*100:+.1f}%  max DD {(eq/eq.cummax()-1).min()*100:.1f}%")


if __name__ == "__main__":
    main()

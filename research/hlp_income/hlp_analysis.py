"""Depositor returns of the HLP vault, from export_hlp_history.py output.

The API gives, per sample, the vault's account value (which moves with deposits
and withdrawals) and cumulative PnL (which does not). The return of a depositor
between two samples is therefore approximated by delta PnL / account value at
the start of the interval (a time-weighted, flow-neutral estimate). The allTime
series has ~1-2 week spacing, so intra-period drawdowns are underestimated;
the month/week series are used to check the most recent period at finer grain.

    python hlp_analysis.py --data ./data
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pandas as pd


def interval_returns(h: pd.DataFrame, period: str) -> pd.DataFrame:
    s = h[h["period"] == period].sort_values("time_utc").reset_index(drop=True)
    s = s[s["account_value"] > 0].reset_index(drop=True)
    out = pd.DataFrame({
        "start": s["time_utc"].shift(1),
        "end": s["time_utc"],
        "av_start": s["account_value"].shift(1),
        "dpnl": s["pnl"].diff(),
    }).dropna()
    out = out[out["av_start"] > 1_000_000]          # skip the first weeks with a tiny vault
    out["ret"] = out["dpnl"] / out["av_start"]
    out["days"] = (out["end"] - out["start"]).dt.total_seconds() / 86400
    return out.set_index("end")


def summarise(r: pd.DataFrame) -> dict:
    eq = (1 + r["ret"]).cumprod()
    years = r["days"].sum() / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    per_year_vol = (r["ret"] / r["days"].pow(0.5)).std() * math.sqrt(365.25)
    return {
        "cagr": cagr,
        "vol": per_year_vol,
        "sharpe": cagr / per_year_vol if per_year_vol else float("nan"),
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "worst_interval": float(r["ret"].min()),
        "worst_interval_end": r["ret"].idxmin(),
        "best_interval": float(r["ret"].max()),
        "best_interval_end": r["ret"].idxmax(),
        "negative_share": float((r["ret"] < 0).mean()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data")
    args = ap.parse_args()
    h = pd.read_csv(Path(args.data) / "hlp_history.csv", parse_dates=["time_utc"])
    h["account_value"] = pd.to_numeric(h["account_value"], errors="coerce")
    h["pnl"] = pd.to_numeric(h["pnl"], errors="coerce")

    r = interval_returns(h, "allTime")
    s = summarise(r)
    print(f"allTime: {len(r)} intervals, {r.index[0]:%Y-%m-%d} -> {r.index[-1]:%Y-%m-%d}")
    print(f"  CAGR {s['cagr']*100:.1f}%  vol {s['vol']*100:.1f}%  return/vol {s['sharpe']:.2f}  "
          f"max DD {s['max_dd']*100:.1f}%  negative intervals {s['negative_share']*100:.0f}%")
    print(f"  worst interval {s['worst_interval']*100:+.2f}% (to {s['worst_interval_end']:%Y-%m-%d}), "
          f"best {s['best_interval']*100:+.2f}% (to {s['best_interval_end']:%Y-%m-%d})")

    print("\nBy calendar year (compounded interval returns, attributed to interval end):")
    for year, g in r.groupby(r.index.year):
        print(f"  {year}: {((1 + g['ret']).prod() - 1) * 100:+6.1f}%  ({g['days'].sum():.0f} days, "
              f"worst interval {g['ret'].min()*100:+.2f}%)")

    print("\nBy half-year:")
    for (year, half), g in r.groupby([r.index.year, (r.index.month > 6) + 1]):
        days = g["days"].sum()
        tot = (1 + g["ret"]).prod() - 1
        print(f"  {year} H{half}: {tot*100:+6.1f}%  annualised {((1 + tot) ** (365.25 / days) - 1)*100:+6.1f}%")

    for period in ("month", "week"):
        m = interval_returns(h, period)
        if len(m):
            tot = (1 + m["ret"]).prod() - 1
            days = m["days"].sum()
            print(f"\nLast {period}: {tot*100:+.2f}% over {days:.1f} days "
                  f"(annualised {((1 + tot) ** (365.25 / days) - 1)*100:+.1f}%), "
                  f"worst step {m['ret'].min()*100:+.2f}% at {m['ret'].idxmin():%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    main()

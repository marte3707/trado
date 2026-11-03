"""Delta-neutral funding carry backtest on Hyperliquid data.

Position per coin: long spot + short perp of equal notional. P&L sources:
  + hourly funding received by the short perp (negative funding is paid)
  + basis P&L: short perp entered at premium p_in, closed at p_out -> (p_in - p_out) * notional
  - fees + slippage on both legs at every entry, exit and resize
  - margin top-ups: when price rallies, part of the spot is sold to refill perp margin

Selection (daily, 00:00 UTC, no lookahead): trailing LOOKBACK_DAYS mean funding,
annualised. Enter when above ENTRY_APR, exit when below EXIT_APR; at most
MAX_POSITIONS coins, highest signal first. Capital per unit of notional is
1 (spot) + 1/PERP_LEV (perp margin); unused capital earns 0%.

    python carry_backtest.py --data ./data
"""

from __future__ import annotations

import argparse
import math
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=DeprecationWarning)

MAJORS = ("BTC", "ETH", "SOL")


@dataclass(frozen=True)
class Config:
    name: str
    coins: tuple[str, ...] | None = None      # None = every coin in the data
    max_positions: int = 3
    entry_apr: float = 0.15
    exit_apr: float = 0.05
    lookback_days: int = 7
    perp_lev: float = 3.0
    perp_fee: float = 0.00045                 # Hyperliquid perp taker, base tier
    spot_fee: float = 0.0007                  # Hyperliquid spot taker, base tier
    slip_major: float = 0.0003
    slip_alt: float = 0.0010
    resize_band: float = 0.25                 # resize when notional drifts >25% from target
    always_on: bool = False                   # ignore thresholds, hold every coin
    extra: dict = field(default_factory=dict)


def load(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    f = pd.read_csv(data_dir / "funding_hourly.csv", parse_dates=["time_utc"])
    f["hour"] = f["time_utc"].dt.floor("h")
    rate = f.pivot_table(index="hour", columns="coin", values="funding_rate", aggfunc="last").sort_index()
    prem = f.pivot_table(index="hour", columns="coin", values="premium", aggfunc="last").sort_index()
    c = pd.read_csv(data_dir / "candles_daily.csv", parse_dates=["date"])
    close = c.pivot_table(index="date", columns="coin", values="close").sort_index()
    high = c.pivot_table(index="date", columns="coin", values="high").sort_index()
    return rate, prem, pd.concat({"close": close, "high": high}, axis=1)


def leg_cost(cfg: Config, coin: str) -> float:
    """One-way cost of trading both legs, as a fraction of notional."""
    slip = cfg.slip_major if coin in MAJORS else cfg.slip_alt
    return cfg.perp_fee + cfg.spot_fee + 2 * slip


def run(cfg: Config, rate: pd.DataFrame, prem: pd.DataFrame, px: pd.DataFrame) -> dict:
    coins = [c for c in (cfg.coins or rate.columns) if c in rate.columns]
    rate, prem = rate[coins], prem[coins]
    days = pd.date_range(rate.index[0].normalize() + pd.Timedelta(days=cfg.lookback_days),
                         rate.index[-1].normalize(), freq="D")
    daily_apr = rate.resample("D").mean() * 24 * 365
    signal = daily_apr.rolling(cfg.lookback_days, min_periods=cfg.lookback_days).mean()
    close, high = px["close"].reindex(columns=coins), px["high"].reindex(columns=coins)
    cap_per_notional = 1 + 1 / cfg.perp_lev

    equity = 1.0
    held: dict[str, dict] = {}   # coin -> {notional, ref_px, prem_in}
    rows = []
    stats = {"entries": 0, "exits": 0, "fees": 0.0, "funding": 0.0, "basis": 0.0,
             "margin_topups": 0, "liq_risk_days": 0}

    for day in days:
        sig = signal.shift(1).loc[day] if day in signal.index else pd.Series(dtype=float)  # funding up to day-1 only
        hour0 = day
        p_now = prem.loc[:hour0].iloc[-1] if len(prem.loc[:hour0]) else pd.Series(dtype=float)

        # exits
        for coin in list(held):
            s = sig.get(coin, np.nan)
            if not cfg.always_on and (np.isnan(s) or s < cfg.exit_apr):
                h = held.pop(coin)
                p_out = float(p_now.get(coin, 0.0) or 0.0)
                basis = (h["prem_in"] - p_out) * h["notional"]
                cost = leg_cost(cfg, coin) * h["notional"]
                equity += basis - cost
                stats["basis"] += basis
                stats["fees"] += cost
                stats["exits"] += 1

        # entries
        slot_notional = equity / (cfg.max_positions * cap_per_notional)
        ranked = sig.dropna().sort_values(ascending=False)
        for coin, s in ranked.items():
            if len(held) >= cfg.max_positions:
                break
            if coin in held or (not cfg.always_on and s < cfg.entry_apr):
                continue
            ref = close.loc[:day - pd.Timedelta(days=1), coin].dropna()
            if ref.empty:
                continue
            cost = leg_cost(cfg, coin) * slot_notional
            equity -= cost
            stats["fees"] += cost
            stats["entries"] += 1
            held[coin] = {"notional": slot_notional, "ref_px": float(ref.iloc[-1]),
                          "prem_in": float(p_now.get(coin, 0.0) or 0.0)}

        # resize to target when drift is large
        for coin, h in held.items():
            drift = h["notional"] / slot_notional - 1
            if abs(drift) > cfg.resize_band:
                cost = leg_cost(cfg, coin) * abs(h["notional"] - slot_notional)
                equity -= cost
                stats["fees"] += cost
                h["notional"] = slot_notional

        # accrue the day's hourly funding (a settlement at T pays for the hour before T)
        hours = rate.loc[day + pd.Timedelta(hours=1): day + pd.Timedelta(hours=24)]
        day_funding = 0.0
        for coin, h in held.items():
            if coin in hours:
                day_funding += float(hours[coin].fillna(0).sum()) * h["notional"]
        equity += day_funding
        stats["funding"] += day_funding

        # price moves: notional follows price (fixed quantity); rallies drain perp margin
        for coin, h in held.items():
            c = close[coin].get(day, np.nan)
            hi = high[coin].get(day, np.nan)
            if np.isnan(c):
                continue
            if not np.isnan(hi) and hi / h["ref_px"] - 1 > 0.8 / cfg.perp_lev:
                stats["liq_risk_days"] += 1
            move = c / h["ref_px"] - 1
            h["notional"] *= c / h["ref_px"]
            if move > 0.5 / cfg.perp_lev:
                # sell spot to refill margin: trade ~move * notional on the spot leg and re-short
                slip = cfg.slip_major if coin in MAJORS else cfg.slip_alt
                cost = (cfg.spot_fee + slip) * move * h["notional"]
                equity -= cost
                stats["fees"] += cost
                stats["margin_topups"] += 1
            h["ref_px"] = c if move > 0.5 / cfg.perp_lev or move < -0.5 / cfg.perp_lev else h["ref_px"]

        # mark-to-market of the open basis (realised only at exit)
        p_eod = prem.loc[:day + pd.Timedelta(hours=24)].iloc[-1]
        unrealised = sum((h["prem_in"] - float(p_eod.get(c, 0.0) or 0.0)) * h["notional"] for c, h in held.items())
        invested = sum(h["notional"] for h in held.values()) * cap_per_notional / equity if equity > 0 else 0
        rows.append({"day": day, "equity": equity + unrealised, "n": len(held), "invested": invested})

    curve = pd.DataFrame(rows).set_index("day")
    return {"cfg": cfg, "curve": curve, "stats": stats}


def perf(eq: pd.Series) -> dict:
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    sharpe = r.mean() / r.std() * math.sqrt(365) if r.std() > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min())
    return {"cagr": cagr, "sharpe": sharpe, "max_dd": dd, "worst_day": float(r.min()) if len(r) else 0.0}


def report(results: list[dict]) -> str:
    lines = ["| Config | CAGR | Sharpe | Max DD | Worst day | Avg invested | Entries | Funding | Basis | Costs | Liq-risk days |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for res in results:
        p, s, c = perf(res["curve"]["equity"]), res["stats"], res["curve"]
        lines.append(
            f"| {res['cfg'].name} | {p['cagr']*100:.1f}% | {p['sharpe']:.2f} | {p['max_dd']*100:.1f}% | "
            f"{p['worst_day']*100:.2f}% | {c['invested'].mean()*100:.0f}% | {s['entries']} | "
            f"{s['funding']*100:+.1f}% | {s['basis']*100:+.1f}% | {-s['fees']*100:.1f}% | {s['liq_risk_days']} |")
    lines += ["", "Funding / Basis / Costs are cumulative, as % of starting equity.", "",
              "Per calendar year (CAGR within the year):", ""]
    years = sorted({d.year for res in results for d in res["curve"].index})
    lines.append("| Config | " + " | ".join(map(str, years)) + " |")
    lines.append("|---|" + "---|" * len(years))
    for res in results:
        eq = res["curve"]["equity"]
        cells = []
        for y in years:
            s = eq[eq.index.year == y]
            cells.append(f"{(s.iloc[-1] / s.iloc[0] - 1) * 100:+.1f}%" if len(s) > 20 else "")
        lines.append(f"| {res['cfg'].name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data")
    ap.add_argument("--out", default=None, help="write the markdown report here")
    args = ap.parse_args()
    rate, prem, px = load(Path(args.data))

    base = Config(name="")
    configs = [
        replace(base, name="BTC+ETH always on", coins=("BTC", "ETH"), max_positions=2, always_on=True),
        replace(base, name="Majors (BTC/ETH/SOL), 15%/5%", coins=MAJORS, max_positions=3),
        replace(base, name="HL-spot set (+HYPE), 15%/5%", coins=MAJORS + ("HYPE",), max_positions=4),
        replace(base, name="All 30, top 5, 15%/5%", max_positions=5),
        replace(base, name="All 30, top 5, 25%/10%", max_positions=5, entry_apr=0.25, exit_apr=0.10),
        replace(base, name="All 30, top 10, 15%/5%", max_positions=10),
        replace(base, name="All 30, top 5, 2x slippage", max_positions=5, slip_major=0.0006, slip_alt=0.0020),
    ]
    results = [run(c, rate, prem, px) for c in configs]
    text = report(results)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    main()

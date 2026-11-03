"""Backtest the Donchian ensemble with vibetrading (pip install vibetrading==0.4.0).

Examples:
    # BTC only, Bitstamp daily CSV (date,open,high,low,close,volume), constant funding stress
    python backtest.py --csv btc-usd-daily.csv --start 2016-01-01 --end 2026-11-02

    # Multi-asset portfolio with Binance perp data and real funding history
    python backtest.py --binance BTC,ETH,SOL,XRP,DOGE,ADA,AVAX,LINK,DOT,BNB \
        --start 2021-01-01 --end 2026-11-02

Candles are re-indexed to their CLOSE time so the engine never trades on an
unfinished day (vibetrading indexes by open time and reads `close` as-of now).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import vibetrading.backtest as vb

from strategy import generate

HL_TAKER_FEE = 0.00045


def _close_time_index(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(UTC) if idx.tz is None else idx.tz_convert(UTC)
    df.index = idx + timedelta(days=1)
    return df


def load_csv(path: str, funding_8h: float) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["fundingRate"] = funding_8h
    df["openInterest"] = np.nan
    return {"BTC/1d": _close_time_index(df)}


def load_binance(assets: list[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    from vibetrading.tools import download_data

    warmup = start - timedelta(days=400)
    raw = download_data(assets, exchange="binance", interval="1d", start_time=warmup, end_time=end)
    out = {}
    for key, df in raw.items():
        asset = key.split("/")[0].upper()
        out[f"{asset}/1d"] = _close_time_index(df)
    return out


def perf(eq: pd.Series) -> dict:
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    sharpe = r.mean() / r.std() * math.sqrt(365) if r.std() > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min())
    return {
        "cagr_pct": round(cagr * 100, 1),
        "sharpe": round(float(sharpe), 2),
        "max_dd_pct": round(dd * 100, 1),
        "calmar": round(cagr / abs(dd), 2) if dd else None,
        "multiple": round(float(eq.iloc[-1] / eq.iloc[0]), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--binance", help="comma separated assets")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--funding-8h", type=float, default=0.0001, help="CSV mode only")
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--params", default="{}", help="JSON overrides for strategy.DEFAULTS")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    params = json.loads(args.params)
    if args.csv:
        data = load_csv(args.csv, args.funding_8h)
        assets = ["BTC"]
    else:
        assets = [a.strip().upper() for a in args.binance.split(",") if a.strip()]
        data = load_binance(assets, start, end)
    params.setdefault("assets", assets)

    engine = vb.BacktestEngine(
        interval="1d", initial_balances={"USDC": 100_000}, start_time=start, end_time=end,
        exchange="hyperliquid", data=data, mute_strategy_prints=True,
        slippage_bps=args.slippage_bps,
    )
    engine.sandbox.fee_rate = HL_TAKER_FEE
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(generate(**params)) or {}
    metrics = result.get("metrics", {})

    closes = pd.concat(
        {k.split("/")[0]: v["close"] for k, v in data.items()}, axis=1
    ).loc[start:end].dropna(how="all")
    bench = (1 + closes.pct_change().fillna(0).mean(axis=1)).cumprod()  # equal-weight daily rebal

    print(json.dumps({
        "period": f"{args.start}..{args.end}",
        "assets": params["assets"],
        "strategy": perf(engine.get_equity_curve()["total_value"]),
        "buy_hold_equal_weight": perf(bench),
        "fees_usd": round(metrics.get("total_tx_fees", 0.0)),
        "funding_usd": round(metrics.get("funding_revenue", 0.0)),
    }, indent=2))


if __name__ == "__main__":
    main()

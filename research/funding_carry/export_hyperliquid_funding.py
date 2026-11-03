"""Export Hyperliquid funding history and daily perp candles to CSV.

Standard library only: runs with any Python 3.10+, no API key needed.

    python3 export_hyperliquid_funding.py                     # top 30 perps by open interest
    python3 export_hyperliquid_funding.py --coins BTC,ETH,SOL --start 2023-06-01
    python3 export_hyperliquid_funding.py --top 50 --out ./data

Writes:
    <out>/funding_hourly.csv  coin,time_utc,funding_rate,premium      (one row per hourly settlement)
    <out>/candles_daily.csv   coin,date,open,high,low,close,volume     (perp daily bars)
    <out>/coins/<COIN>_*.csv  per-coin parts; a re-run skips coins already exported
    <out>/universe.csv        coin,open_interest_usd,day_volume_usd,current_funding,max_leverage
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

INFO_URL = "https://api.hyperliquid.xyz/info"
FUNDING_PAGE = 500          # Hyperliquid returns at most 500 funding rows per call
CANDLE_LIMIT = 5000         # and at most 5000 candles per call
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
# Hyperliquid allows ~1200 weight/min per IP; a full funding page costs ~45
# (20 base + 1 per 20 rows), so ~26 pages/min max. Trado's worker shares the
# same IP budget, hence the conservative pacing.
FUNDING_PAUSE_S = 3.0
CANDLE_PAUSE_S = 1.5


def post(payload: dict, retries: int = 10) -> object:
    body = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(INFO_URL, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                wait = min(90, 10 * 2 ** attempt)
                print(f"  HTTP {exc.code}, retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as exc:
            wait = min(90, 5 * 2 ** attempt)
            print(f"  network error {exc.reason}, retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Hyperliquid request failed after {retries} attempts: {payload}")


def universe() -> list[dict]:
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    rows = []
    for asset, ctx in zip(meta["universe"], ctxs, strict=False):
        if asset.get("isDelisted"):
            continue
        mark = float(ctx.get("markPx") or 0)
        rows.append({
            "coin": asset["name"],
            "open_interest_usd": round(float(ctx.get("openInterest") or 0) * mark, 2),
            "day_volume_usd": round(float(ctx.get("dayNtlVlm") or 0), 2),
            "current_funding": ctx.get("funding"),
            "max_leverage": asset.get("maxLeverage"),
        })
    rows.sort(key=lambda r: r["open_interest_usd"], reverse=True)
    return rows


def funding_history(coin: str, start_ms: int, end_ms: int) -> list[dict]:
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        page = post({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms})
        time.sleep(FUNDING_PAUSE_S)
        if not page:
            break
        for item in page:
            rows.append({
                "coin": coin,
                "time_ms": int(item["time"]),
                "funding_rate": item["fundingRate"],
                "premium": item.get("premium"),
            })
        last = int(page[-1]["time"])
        if len(page) < FUNDING_PAGE or last <= cursor:
            break
        cursor = last + 1
    dedup = {r["time_ms"]: r for r in rows}
    return [dedup[k] for k in sorted(dedup)]


def daily_candles(coin: str, start_ms: int, end_ms: int) -> list[dict]:
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        page = post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1d", "startTime": cursor, "endTime": end_ms}})
        time.sleep(CANDLE_PAUSE_S)
        if not page:
            break
        rows.extend(page)
        last = int(page[-1]["t"])
        if len(page) < CANDLE_LIMIT or last <= cursor:
            break
        cursor = last + DAY_MS
    out = {}
    for c in rows:
        if int(c["T"]) > end_ms:      # skip the still-open day
            continue
        out[int(c["t"])] = {
            "coin": coin,
            "date": datetime.fromtimestamp(int(c["t"]) / 1000, UTC).strftime("%Y-%m-%d"),
            "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"], "volume": c["v"],
        }
    return [out[k] for k in sorted(out)]


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", help="comma separated, e.g. BTC,ETH,SOL (default: top N by open interest)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--start", default="2023-05-01", help="YYYY-MM-DD (Hyperliquid hourly funding starts ~May 2023)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD, default now")
    ap.add_argument("--out", default="./data")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start_ms = int(datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp() * 1000)
    end_ms = (
        int(datetime.fromisoformat(args.end).replace(tzinfo=UTC).timestamp() * 1000)
        if args.end else int(time.time() * 1000)
    )

    uni = universe()
    write_csv(out / "universe.csv", uni, ["coin", "open_interest_usd", "day_volume_usd", "current_funding", "max_leverage"])
    coins = [c.strip() for c in args.coins.split(",") if c.strip()] if args.coins else [r["coin"] for r in uni[: args.top]]
    print(f"Exporting {len(coins)} coins from {args.start}: {', '.join(coins)}")

    per_coin = out / "coins"
    per_coin.mkdir(exist_ok=True)
    funding_fields = ["coin", "time_utc", "funding_rate", "premium"]
    candle_fields = ["coin", "date", "open", "high", "low", "close", "volume"]
    for i, coin in enumerate(coins, 1):
        f_path, c_path = per_coin / f"{coin}_funding.csv", per_coin / f"{coin}_candles.csv"
        if f_path.exists() and c_path.exists():
            print(f"[{i}/{len(coins)}] {coin}: already exported, skipping")
            continue
        f = funding_history(coin, start_ms, end_ms)
        for r in f:
            r["time_utc"] = datetime.fromtimestamp(r["time_ms"] / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
        c = daily_candles(coin, start_ms, end_ms)
        write_csv(c_path, c, candle_fields)
        write_csv(f_path, f, funding_fields)   # written last: marks the coin complete
        print(f"[{i}/{len(coins)}] {coin}: {len(f)} funding rows, {len(c)} daily candles")

    for name, suffix, fields in (("funding_hourly.csv", "_funding.csv", funding_fields),
                                 ("candles_daily.csv", "_candles.csv", candle_fields)):
        with (out / name).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for coin in coins:
                part = per_coin / f"{coin}{suffix}"
                if part.exists():
                    with part.open(newline="") as src:
                        writer.writerows(csv.DictReader(src))
    print(f"Done. Files in {out.resolve()}")


if __name__ == "__main__":
    main()

"""Export the Hyperliquidity Provider (HLP) vault history from the public info API.

Standard library only, no API key:

    python3 export_hlp_history.py --out ./data

Writes:
    <out>/hlp_vault_details.json   raw vaultDetails response (all periods, kept for audit)
    <out>/hlp_history.csv          period,time_utc,account_value,pnl   (one row per API sample)
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

INFO_URL = "https://api.hyperliquid.xyz/info"
HLP_VAULT = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"


def post(payload: dict, retries: int = 6) -> object:
    body = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(INFO_URL, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            wait = min(60, 5 * 2 ** attempt)
            print(f"  request failed ({exc}), retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Hyperliquid request failed after {retries} attempts: {payload}")


def flatten(details: dict) -> list[dict]:
    """portfolio = [[period, {accountValueHistory: [[ms, v], ...], pnlHistory: [[ms, v], ...]}], ...]"""
    rows = []
    for period, data in details.get("portfolio") or []:
        values = {int(t): v for t, v in data.get("accountValueHistory") or []}
        pnls = {int(t): v for t, v in data.get("pnlHistory") or []}
        for t in sorted(set(values) | set(pnls)):
            rows.append({
                "period": period,
                "time_utc": datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "account_value": values.get(t, ""),
                "pnl": pnls.get(t, ""),
            })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", default=HLP_VAULT)
    ap.add_argument("--out", default="./data")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    details = post({"type": "vaultDetails", "vaultAddress": args.vault})
    if not isinstance(details, dict):
        raise SystemExit(f"Unexpected response: {str(details)[:300]}")
    (out / "hlp_vault_details.json").write_text(json.dumps(details, indent=1))
    rows = flatten(details)
    with (out / "hlp_history.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["period", "time_utc", "account_value", "pnl"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Vault: {details.get('name')}  current APR field: {details.get('apr')}")
    for period in sorted({r["period"] for r in rows}):
        sample = [r for r in rows if r["period"] == period]
        print(f"  {period:12} {len(sample):5} samples  {sample[0]['time_utc']} -> {sample[-1]['time_utc']}")
    print(f"Done. Files in {out.resolve()}")


if __name__ == "__main__":
    main()

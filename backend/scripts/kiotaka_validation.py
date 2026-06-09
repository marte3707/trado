"""Validazione Fase 1: dati di flusso REALI (kiyotaka) vs nostre approssimazioni OHLCV.

Confronta, su BTC e su una finestra storica, i dati reali esposti da kiyotaka con
le approssimazioni che il nostro sistema deduce dalle sole candele OHLCV:

  - CVD reale (TRADE_AGG, volume BUY vs SELL)   vs   approx `sign(close-open) x volume`
  - Open Interest reale (OPEN_INTEREST_AGG OHLC) vs   nostro storico OI (di fatto assente)
  - Liquidazioni reali (LIQUIDATION_AGG long/short) vs nostro proxy "barra ad alto volume"

Obiettivo: decidere DATA-DRIVEN se i dati reali divergono abbastanza dalle nostre
approssimazioni da giustificare l'integrazione (e, in seconda battuta, la spesa per
un tier a pagamento). Costo: 3 richieste REST (~3 weight), dentro i limiti del free.

Uso:
    cd backend && ../venv/bin/python -m scripts.kiotaka_validation [--days 7] [--exchange BINANCE_FUTURES]

Il dato kiyotaka di default e' BTC su BINANCE_FUTURES (flusso "globale"); le nostre
candele sono Hyperliquid. Il confronto e' sulla DIREZIONE/forma del flusso, non sui
valori assoluti — coerente con l'idea di usare il flusso aggregato globale come gate macro.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from app.db.session import SessionLocal

KIYOTAKA_BASE = "https://api.kiyotaka.ai/v1"
OUT_DIR = Path(__file__).resolve().parent / "out"


# --------------------------------------------------------------------------- #
# API key & client
# --------------------------------------------------------------------------- #
def load_api_key() -> str:
    key = os.getenv("KIOTAKA_API_KEY")
    if key:
        return key.strip()
    # Fallback: parse della .env nella root del progetto.
    root_env = Path(__file__).resolve().parents[2] / ".env"
    if root_env.exists():
        for line in root_env.read_text().splitlines():
            if line.startswith("KIOTAKA_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("KIOTAKA_API_KEY non trovata (ne' in env ne' in .env root)")


def fetch_points(
    client: httpx.Client, key: str, point_type: str, exchange: str, raw_symbol: str,
    from_s: int, period_s: int, interval: str = "HOUR",
) -> list[dict[str, Any]]:
    """Ritorna la lista `series` grezza per un tipo. Ogni serie: {id: {...}, points: [...]}."""
    params = {
        "type": point_type,
        "exchange": exchange,
        "rawSymbol": raw_symbol,
        "interval": interval,
        "from": from_s,
        "period": period_s,
    }
    resp = client.get(
        f"{KIYOTAKA_BASE}/points", params=params, headers={"X-Kiyotaka-Key": key}, timeout=30.0
    )
    if resp.status_code != 200:
        raise SystemExit(f"{point_type}: HTTP {resp.status_code} -> {resp.text[:200]}")
    return resp.json().get("series", [])


def _points_to_map(series: list[dict], field: str, side: str | None = None) -> dict[int, float]:
    """Estrae {hour_epoch: valore} dalla prima serie che matcha `side` (o l'unica)."""
    for s in series:
        if side is not None and s.get("id", {}).get("side") != side:
            continue
        out: dict[int, float] = {}
        for p in s.get("points", []):
            point = p.get("Point", p)
            ts = point.get("timestamp", {}).get("s")
            if ts is None:
                continue
            out[int(ts)] = float(point.get(field, 0.0))
        return out
    return {}


# --------------------------------------------------------------------------- #
# Stat helpers (no numpy)
# --------------------------------------------------------------------------- #
def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


# --------------------------------------------------------------------------- #
# Nostre candele BTC perp 1h
# --------------------------------------------------------------------------- #
def load_our_btc_candles(since_s: int) -> list[dict[str, Any]]:
    sql = text(
        """
        SELECT EXTRACT(EPOCH FROM c.open_time)::bigint AS ts,
               c.open, c.high, c.low, c.close, c.volume, c.open_interest
        FROM candles c
        JOIN markets m ON m.id = c.market_id
        WHERE m.base = 'BTC' AND m.market_type = 'perp'
          AND c.timeframe = '1h'
          AND EXTRACT(EPOCH FROM c.open_time)::bigint >= :since
        ORDER BY c.open_time ASC
        """
    )
    with SessionLocal() as db:
        rows = db.execute(sql, {"since": since_s}).fetchall()
    return [
        {
            "ts": int(r.ts), "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close), "volume": float(r.volume),
            "open_interest": float(r.open_interest) if r.open_interest is not None else None,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser()
    # Il free tier limita la finestra storica per richiesta (~96h / 4 giorni: 168h da' "time range exceeded").
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--exchange", default="BINANCE_FUTURES")
    parser.add_argument("--symbol", default="BTCUSDT")
    args = parser.parse_args()

    key = load_api_key()
    now = int(time.time())
    period = args.days * 86400
    from_s = now - period

    print(f"Finestra: ultimi {args.days}g  | exchange={args.exchange} symbol={args.symbol}")
    print(f"from={from_s} ({datetime.fromtimestamp(from_s, timezone.utc):%Y-%m-%d %H:%M} UTC) period={period}s\n")

    with httpx.Client() as client:
        oi_series = fetch_points(client, key, "OPEN_INTEREST_AGG", args.exchange, args.symbol, from_s, period)
        liq_series = fetch_points(client, key, "LIQUIDATION_AGG", args.exchange, args.symbol, from_s, period)
        trade_series = fetch_points(client, key, "TRADE_AGG", args.exchange, args.symbol, from_s, period)

    oi_real = _points_to_map(oi_series, "close")                       # OI close per ora
    liq_short = _points_to_map(liq_series, "liquidations", side="BUY")  # short liquidati = ordini BUY
    liq_long = _points_to_map(liq_series, "liquidations", side="SELL")  # long liquidati = ordini SELL
    buy_vol = _points_to_map(trade_series, "volume", side="BUY")
    sell_vol = _points_to_map(trade_series, "volume", side="SELL")

    our = load_our_btc_candles(from_s)
    our_by_hour = {c["ts"]: c for c in our}

    print(f"Punti kiyotaka: OI={len(oi_real)} liq_buy={len(liq_short)} liq_sell={len(liq_long)} "
          f"trade_buy={len(buy_vol)} trade_sell={len(sell_vol)}")
    print(f"Nostre candele BTC perp 1h nella finestra: {len(our)}")
    oi_in_db = sum(1 for c in our if c["open_interest"] is not None)
    print(f"  di cui con open_interest storico valorizzato: {oi_in_db}\n")

    # Ore comuni a kiyotaka (trade) e nostre candele, ordinate.
    hours = sorted(set(buy_vol) & set(sell_vol) & set(our_by_hour))
    if len(hours) < 5:
        print("ATTENZIONE: troppe poche ore in comune per un confronto significativo.")
        print("Probabile causa: poche candele BTC 1h nel DB sulla finestra richiesta.")
        return

    # --- CVD: reale vs approssimato -------------------------------------- #
    real_delta, approx_delta = [], []
    real_cvd, approx_cvd = [], []
    rc = ac = 0.0
    rows_csv = []
    dir_agree = 0
    for h in hours:
        rd = buy_vol[h] - sell_vol[h]            # delta CVD reale (volume aggressori)
        c = our_by_hour[h]
        sign = 1.0 if c["close"] > c["open"] else (-1.0 if c["close"] < c["open"] else 0.0)
        ad = sign * c["volume"]                  # delta CVD approssimato (nostra convenzione)
        rc += rd
        ac += ad
        real_delta.append(rd); approx_delta.append(ad)
        real_cvd.append(rc); approx_cvd.append(ac)
        if (rd > 0) == (ad > 0) and rd != 0 and ad != 0:
            dir_agree += 1
        rows_csv.append({
            "hour_utc": datetime.fromtimestamp(h, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "real_cvd_delta": round(rd, 2), "approx_cvd_delta": round(ad, 2),
            "real_cvd_cum": round(rc, 2), "approx_cvd_cum": round(ac, 2),
            "oi_real_close": round(oi_real.get(h, float("nan")), 2),
            "liq_long": round(liq_long.get(h, 0.0), 3), "liq_short": round(liq_short.get(h, 0.0), 3),
            "our_volume": round(c["volume"], 2), "price_close": round(c["close"], 2),
        })

    r_delta = pearson(real_delta, approx_delta)
    r_cum = pearson(real_cvd, approx_cvd)
    print("=== CVD: reale (buy-sell vol) vs approx (sign(close-open) x vol) ===")
    print(f"  ore confrontate: {len(hours)}")
    print(f"  correlazione delta orari   r = {r_delta:+.3f}")
    print(f"  correlazione CVD cumulato  r = {r_cum:+.3f}")
    print(f"  accordo direzionale orario : {dir_agree}/{len(hours)} = {dir_agree/len(hours):.0%}")
    print(f"  -> r alto = la nostra approssimazione regge; r basso/medio = i dati reali aggiungono informazione\n")

    # --- Open Interest: cosa ci perdiamo ---------------------------------- #
    oi_hours = sorted(set(oi_real) & set(our_by_hour))
    genuine = squeeze = 0
    for i in range(1, len(oi_hours)):
        h0, h1 = oi_hours[i - 1], oi_hours[i]
        d_oi = oi_real[h1] - oi_real[h0]
        d_px = our_by_hour[h1]["close"] - our_by_hour[h0]["close"]
        if d_px == 0:
            continue
        if d_oi > 0:
            genuine += 1   # nuovo capitale entra a favore del movimento
        else:
            squeeze += 1   # movimento per chiusura posizioni (squeeze/deleveraging)
    print("=== Open Interest reale (storico OHLC) ===")
    print(f"  storico OI nostro nel DB: {oi_in_db} candele su {len(our)} (di fatto {'ASSENTE' if oi_in_db == 0 else 'sparso'})")
    print(f"  ore con OI reale: {len(oi_hours)}  | trend genuini (OI su): {genuine}  squeeze/deleverage (OI giu): {squeeze}")
    print(f"  -> `oi_trend` oggi NON ha serie storica: non puo' distinguere questi {squeeze} casi dai {genuine} genuini\n")

    # --- Liquidazioni: reale vs proxy "barra alto volume" ----------------- #
    tot_long = sum(liq_long.values())
    tot_short = sum(liq_short.values())
    liq_hours = {h: liq_long.get(h, 0.0) + liq_short.get(h, 0.0) for h in set(liq_long) | set(liq_short)}
    top = sorted(liq_hours.items(), key=lambda kv: kv[1], reverse=True)[:5]
    # Proxy nostro: barra con volume > 2x media mobile a 20 sulle nostre candele.
    vols = [our_by_hour[h]["volume"] for h in hours]
    proxy_spike_hours = set()
    for i, h in enumerate(hours):
        if i < 20:
            continue
        avg = sum(vols[i - 20:i]) / 20
        if avg > 0 and vols[i] >= 2.0 * avg:
            proxy_spike_hours.add(h)
    # Ground truth: ore nel top-quartile di liquidazioni reali.
    if liq_hours:
        sorted_liq = sorted(liq_hours.values(), reverse=True)
        q_idx = max(0, int(len(sorted_liq) * 0.25) - 1)
        liq_threshold = sorted_liq[q_idx]
        real_spike_hours = {h for h, v in liq_hours.items() if v >= liq_threshold and v > 0}
    else:
        real_spike_hours = set()
    overlap = proxy_spike_hours & real_spike_hours
    precision = len(overlap) / len(proxy_spike_hours) if proxy_spike_hours else float("nan")
    recall = len(overlap) / len(real_spike_hours) if real_spike_hours else float("nan")
    print("=== Liquidazioni reali (long vs short) vs proxy 'barra ad alto volume' ===")
    print(f"  totale liquidazioni finestra: long={tot_long:.1f}  short={tot_short:.1f}  (BTC)")
    print("  top 5 ore per liquidazioni:")
    for h, v in top:
        print(f"    {datetime.fromtimestamp(h, timezone.utc):%Y-%m-%d %H:%M} UTC  "
              f"long={liq_long.get(h, 0):.2f} short={liq_short.get(h, 0):.2f} tot={v:.2f}")
    print(f"  nostro proxy (vol>2x avg20): {len(proxy_spike_hours)} ore  |  spike reali (top-quartile): {len(real_spike_hours)} ore")
    print(f"  precision={precision:.0%}  recall={recall:.0%}  (quanto il proxy intercetta le vere liquidazioni)\n")

    # --- CSV ------------------------------------------------------------- #
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "kiotaka_validation.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_csv[0].keys()))
        writer.writeheader()
        writer.writerows(rows_csv)
    print(f"CSV dettagliato scritto in: {out_path}")
    print("\nLettura del verdetto:")
    print("  - CVD r vicino a 1.0  -> l'approssimazione basta, CVD reale di poco valore.")
    print("  - CVD r medio/basso   -> il CVD reale porta segnale che oggi NON catturiamo.")
    print("  - OI: ogni caso 'squeeze' e' un trade che oi_trend oggi legge male (no storico).")
    print("  - Liquidazioni: recall basso -> il nostro proxy MANCA le vere cascate.")


if __name__ == "__main__":
    main()

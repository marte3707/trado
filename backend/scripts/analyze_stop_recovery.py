"""Analisi recupero post-stop-loss (falsi breakout).

Per ogni posizione paper chiusa in stop loss IN PERDITA, guarda come si e' mosso
il prezzo DOPO lo stop e misura quante volte sarebbe tornato a entry / TP1/2/3.
Distingue cosi' i falsi breakout (prezzo torna verso il trade) dagli entry
genuinamente sbagliati (il prezzo prosegue contro).

Uso:
    cd backend && ../venv/bin/python -m scripts.analyze_stop_recovery
"""

from __future__ import annotations

import asyncio
import csv
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.candle_sync import TIMEFRAME_MS
from app.services.hyperliquid import HyperliquidClient

HORIZONS = [12, 24, 48]
MAX_H = max(HORIZONS)
CONCURRENCY = 3  # basso per non sbattere sul rate-limit (429) di Hyperliquid
THROTTLE_S = 0.15  # piccola pausa dopo ogni fetch
OUT_DIR = Path(__file__).parent / "out"


@dataclass
class Trade:
    pos_id: int
    symbol: str
    hl_coin: str
    side: str  # long | short
    tf: str
    entry: Decimal
    stop_px: Decimal
    risk: Decimal  # |entry - initial_stop_loss|
    closed_ms: int
    realized_pnl: Decimal
    score: int | None
    regime: str | None
    strategy: str | None
    tp1: Decimal | None
    tp2: Decimal | None
    tp3: Decimal | None
    # popolati dopo il fetch candele
    no_data: bool = False
    # per orizzonte H -> dict di esiti
    by_h: dict[int, dict[str, Any]] = field(default_factory=dict)


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _dominant_strategy(breakdown: Any) -> str | None:
    """Strategia con contributo maggiore nello strategy_breakdown del signal."""
    if not isinstance(breakdown, dict):
        return None
    best_name = None
    best_val = None
    for name, payload in breakdown.items():
        val = None
        if isinstance(payload, dict):
            # prova chiavi comuni di contributo/peso
            for key in ("contribution", "weighted_score", "weight", "score"):
                if key in payload:
                    val = _dec(payload.get(key))
                    break
        else:
            val = _dec(payload)
        if val is None:
            continue
        if best_val is None or abs(val) > abs(best_val):
            best_val = val
            best_name = name
    return best_name


def load_trades() -> tuple[list[Trade], int]:
    """Ritorna (stop in perdita da analizzare, conteggio stop trailati in profitto)."""
    db = SessionLocal()
    try:
        base = """
            FROM positions p
            LEFT JOIN signals s
                ON s.id = CAST(p."metadata"->>'source_signal_id' AS INTEGER)
            JOIN markets m ON m.id = p.market_id
            WHERE p.mode = 'paper'
              AND p.status = 'closed'
              AND p."metadata"->>'exit_reason' = 'stop_loss'
        """
        profit_count = db.execute(
            text(f"SELECT count(*) {base} AND p.realized_pnl >= 0")
        ).scalar_one()

        rows = db.execute(
            text(
                f"""
                SELECT
                    p.id AS pos_id,
                    m.symbol AS symbol,
                    m."metadata"->>'hl_coin' AS hl_coin,
                    p.side AS side,
                    p."metadata"->>'source_timeframe' AS tf,
                    p.entry_price AS entry,
                    p."metadata"->>'exit_price' AS exit_price,
                    p."metadata"->>'initial_stop_loss' AS initial_sl,
                    p.take_profit AS pos_tp,
                    p.realized_pnl AS realized_pnl,
                    p.closed_at AS closed_at,
                    p."metadata"->>'regime_at_open' AS regime,
                    s.score AS score,
                    s.take_profit_1 AS tp1,
                    s.take_profit_2 AS tp2,
                    s.take_profit_3 AS tp3,
                    s.strategy_breakdown AS breakdown
                {base} AND p.realized_pnl < 0
                ORDER BY p.closed_at
                """
            )
        ).mappings().all()
    finally:
        db.close()

    trades: list[Trade] = []
    skipped = 0
    for r in rows:
        tf = r["tf"]
        entry = _dec(r["entry"])
        stop_px = _dec(r["exit_price"]) or _dec(r["initial_sl"])
        init_sl = _dec(r["initial_sl"])
        # rischio = |entry - stop iniziale|; fallback al prezzo di stop reale per i
        # trade vecchi senza initial_stop_loss nei metadata.
        risk_ref = init_sl if init_sl is not None else stop_px
        if tf not in TIMEFRAME_MS or entry is None or risk_ref is None:
            skipped += 1
            continue
        risk = abs(entry - risk_ref)
        if risk == 0:
            skipped += 1
            continue
        tp1 = _dec(r["tp1"])
        tp2 = _dec(r["tp2"])
        tp3 = _dec(r["tp3"])
        pos_tp = _dec(r["pos_tp"])
        if tp1 is None and pos_tp is not None:  # fallback
            tp1 = pos_tp
        hl_coin = r["hl_coin"] or r["symbol"]
        trades.append(
            Trade(
                pos_id=r["pos_id"],
                symbol=r["symbol"],
                hl_coin=hl_coin,
                side=r["side"],
                tf=tf,
                entry=entry,
                stop_px=stop_px or entry,
                risk=risk,
                closed_ms=int(r["closed_at"].timestamp() * 1000),
                realized_pnl=_dec(r["realized_pnl"]) or Decimal(0),
                score=int(r["score"]) if r["score"] is not None else None,
                regime=r["regime"],
                strategy=_dominant_strategy(r["breakdown"]),
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
            )
        )
    if skipped:
        print(f"[warn] {skipped} stop scartati (tf/entry/risk mancanti)")
    return trades, int(profit_count)


async def fetch_and_eval(client: HyperliquidClient, sem: asyncio.Semaphore, t: Trade) -> None:
    interval_ms = TIMEFRAME_MS[t.tf]
    start = t.closed_ms
    end = t.closed_ms + (MAX_H + 2) * interval_ms
    async with sem:
        try:
            raw = await client.candles(t.hl_coin, t.tf, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] pos {t.pos_id} {t.hl_coin} {t.tf}: fetch fallito ({exc})")
            t.no_data = True
            return
        finally:
            await asyncio.sleep(THROTTLE_S)
    # candele con open_time STRETTAMENTE dopo lo stop, ordinate
    bars: list[tuple[Decimal, Decimal]] = []  # (high, low)
    for row in sorted(raw, key=lambda x: int(x["t"])):
        if int(row["t"]) <= t.closed_ms:
            continue
        bars.append((Decimal(str(row["h"])), Decimal(str(row["l"]))))
    if not bars:
        t.no_data = True
        return

    is_long = t.side == "long"
    for h in HORIZONS:
        window = bars[:h]
        if not window:
            t.by_h[h] = {"insufficient": True}
            continue
        if is_long:
            fav_ext = max(hi for hi, _ in window)  # massimo high
            mfe_R = (fav_ext - t.entry) / t.risk
            reached = lambda tgt: tgt is not None and fav_ext >= tgt  # noqa: E731
            returned_entry = fav_ext >= t.entry
        else:
            fav_ext = min(lo for _, lo in window)  # minimo low
            mfe_R = (t.entry - fav_ext) / t.risk
            reached = lambda tgt: tgt is not None and fav_ext <= tgt  # noqa: E731
            returned_entry = fav_ext <= t.entry

        # bars_to_tp1: prima barra che tocca TP1
        bars_to_tp1 = None
        if t.tp1 is not None:
            for i, (hi, lo) in enumerate(window, start=1):
                hit = hi >= t.tp1 if is_long else lo <= t.tp1
                if hit:
                    bars_to_tp1 = i
                    break

        t.by_h[h] = {
            "returned_entry": returned_entry,
            "tp1": reached(t.tp1),
            "tp2": reached(t.tp2),
            "tp3": reached(t.tp3),
            "mfe_R": float(mfe_R),
            "bars_to_tp1": bars_to_tp1,
        }


async def run() -> None:
    trades, profit_count = load_trades()
    print(f"Stop loss paper IN PERDITA da analizzare: {len(trades)}")
    print(f"Stop loss paper trailati in PROFITTO (esclusi): {profit_count}")

    client = HyperliquidClient(get_settings())
    sem = asyncio.Semaphore(CONCURRENCY)
    await asyncio.gather(*(fetch_and_eval(client, sem, t) for t in trades))

    analyzed = [t for t in trades if not t.no_data]
    no_data = [t for t in trades if t.no_data]
    print(f"Analizzati con candele: {len(analyzed)} | no_data (fetch fallito): {len(no_data)}")
    print(
        f"Totale stop_loss paper = {len(trades)} (candidati) + {profit_count} (profit) = "
        f"{len(trades) + profit_count} ; di cui analizzati {len(analyzed)}, "
        f"no_data {len(no_data)}"
    )
    print()

    _report_overall(analyzed)
    _report_segments(analyzed)
    _write_csv(analyzed)


def _pct(num: int, den: int) -> str:
    return f"{(100.0 * num / den):5.1f}%" if den else "  n/a"


def _agg_for_horizon(trades: list[Trade], h: int) -> dict[str, Any]:
    valid = [t for t in trades if "returned_entry" in t.by_h.get(h, {})]
    n = len(valid)
    if n == 0:
        return {"n": 0}
    ret = sum(1 for t in valid if t.by_h[h]["returned_entry"])
    tp1 = sum(1 for t in valid if t.by_h[h]["tp1"])
    tp2 = sum(1 for t in valid if t.by_h[h]["tp2"])
    tp3 = sum(1 for t in valid if t.by_h[h]["tp3"])
    mfes = [t.by_h[h]["mfe_R"] for t in valid]
    btp1 = [t.by_h[h]["bars_to_tp1"] for t in valid if t.by_h[h]["bars_to_tp1"] is not None]
    return {
        "n": n,
        "ret": ret,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "mfe_med": statistics.median(mfes),
        "mfe_p75": statistics.quantiles(mfes, n=4)[2] if len(mfes) >= 4 else max(mfes),
        "btp1_med": statistics.median(btp1) if btp1 else None,
    }


def _report_overall(trades: list[Trade]) -> None:
    print("=" * 78)
    print("RISULTATO COMPLESSIVO — recupero dopo lo stop (per orizzonte in barre)")
    print("=" * 78)
    print(f"{'H barre':>8} | {'n':>4} | {'->entry':>8} | {'->TP1':>8} | {'->TP2':>8} | {'->TP3':>8} | {'MFE med (R)':>11} | {'MFE p75':>8} | {'barre->TP1':>10}")
    print("-" * 78)
    for h in HORIZONS:
        a = _agg_for_horizon(trades, h)
        if a["n"] == 0:
            print(f"{h:>8} | nessun dato")
            continue
        btp1 = f"{a['btp1_med']:.0f}" if a["btp1_med"] is not None else "n/a"
        print(
            f"{h:>8} | {a['n']:>4} | {_pct(a['ret'], a['n'])} | {_pct(a['tp1'], a['n'])} | "
            f"{_pct(a['tp2'], a['n'])} | {_pct(a['tp3'], a['n'])} | "
            f"{a['mfe_med']:>11.2f} | {a['mfe_p75']:>8.2f} | {btp1:>10}"
        )
    print()
    print("Lettura: alto %->TP1 ⇒ molti falsi breakout (stop stretto/gestione).")
    print("         basso %->TP1 e basso MFE ⇒ entry sbagliati (il trend prosegue contro).")
    print()


def _report_segments(trades: list[Trade]) -> None:
    h = 24  # orizzonte di riferimento per i breakdown
    print("=" * 78)
    print(f"BREAKDOWN a H={h} barre (%->TP1 = quota recuperabile a TP1)")
    print("=" * 78)

    def emit(title: str, keyfn) -> None:
        groups: dict[Any, list[Trade]] = defaultdict(list)
        for t in trades:
            groups[keyfn(t)].append(t)
        print(f"\n— per {title} —")
        print(f"{title:>20} | {'n':>4} | {'->entry':>8} | {'->TP1':>8} | {'->TP3':>8} | {'MFE med':>8}")
        for key, ts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            a = _agg_for_horizon(ts, h)
            if a["n"] == 0:
                continue
            print(
                f"{str(key):>20} | {a['n']:>4} | {_pct(a['ret'], a['n'])} | {_pct(a['tp1'], a['n'])} | "
                f"{_pct(a['tp3'], a['n'])} | {a['mfe_med']:>8.2f}"
            )

    def score_bucket(t: Trade) -> str:
        if t.score is None:
            return "n/a"
        lo = (t.score // 10) * 10
        return f"{lo}-{lo + 9}"

    emit("timeframe", lambda t: t.tf)
    emit("lato", lambda t: t.side)
    emit("score", score_bucket)
    emit("regime", lambda t: t.regime or "n/a")
    emit("strategia", lambda t: t.strategy or "n/a")
    emit("mercato", lambda t: t.symbol)
    print()


def _write_csv(trades: list[Trade]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "stop_recovery.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        header = [
            "pos_id", "symbol", "side", "tf", "entry", "stop_px", "risk",
            "realized_pnl", "score", "regime", "strategy",
        ]
        for h in HORIZONS:
            header += [f"ret_entry_{h}", f"tp1_{h}", f"tp2_{h}", f"tp3_{h}", f"mfe_R_{h}", f"bars_tp1_{h}"]
        w.writerow(header)
        for t in trades:
            row = [
                t.pos_id, t.symbol, t.side, t.tf, t.entry, t.stop_px, t.risk,
                t.realized_pnl, t.score, t.regime, t.strategy,
            ]
            for h in HORIZONS:
                d = t.by_h.get(h, {})
                row += [
                    d.get("returned_entry"), d.get("tp1"), d.get("tp2"),
                    d.get("tp3"), d.get("mfe_R"), d.get("bars_to_tp1"),
                ]
            w.writerow(row)
    print(f"CSV per-trade scritto in: {path}")


if __name__ == "__main__":
    asyncio.run(run())

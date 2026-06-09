"""Simula l'effetto di uno stop loss piu' largo sui trade chiusi in stop in perdita.

Per ogni stop in perdita paper, risimula il trade DALL'ENTRY con stop = m x rischio
originale (m in {1.0, 1.5, 2.0, 3.0}) e guarda cosa viene toccato per primo:
lo stop allargato o il TP1. Calcola l'expected value in R per moltiplicatore,
cosi' si vede se allargare lo stop migliora i risultati complessivi.

Popolazione: solo i trade stoppati in perdita (gli altri non toccavano comunque
lo stop iniziale, quindi allargarlo non li cambia).

Modello di outcome (per trade, per moltiplicatore m):
  - cammina le candele dall'entry per N_MAX barre del timeframe del trade
  - tie-break intrabar CONSERVATIVO: se una barra tocca sia lo stop largo sia il
    TP1, conta lo STOP per primo (pessimista sull'allargamento)
  - stop largo toccato per primo  -> R = -m
  - TP1 toccato per primo          -> R = +r1   (uscita piena a TP1)
  - ne' l'uno ne' l'altro entro N_MAX -> R = (close_ultima - entry)/rischio (mark-to-market)

Uso:
    cd backend && ../venv/bin/python -m scripts.analyze_wider_stop
"""

from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.candle_sync import TIMEFRAME_MS
from app.services.hyperliquid import HyperliquidClient
from scripts.analyze_stop_recovery import _dec, _dominant_strategy

MULTIPLIERS = [Decimal("1.0"), Decimal("1.5"), Decimal("2.0"), Decimal("3.0")]
N_MAX = 96  # barre dall'entry entro cui valutare l'esito
CONCURRENCY = 3
THROTTLE_S = 0.15


@dataclass
class Trade:
    pos_id: int
    symbol: str
    hl_coin: str
    side: str
    tf: str
    entry: Decimal
    risk: Decimal
    risk_stop: Decimal  # stop iniziale (entry -/+ risk)
    tp1: Decimal | None
    opened_ms: int
    score: int | None
    strategy: str | None
    bars: list[tuple[Decimal, Decimal, Decimal]] = field(default_factory=list)  # (high, low, close)
    no_data: bool = False
    # m -> (outcome_label, R)
    sim: dict[str, tuple[str, float]] = field(default_factory=dict)


def load_trades() -> list[Trade]:
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
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
                    p.opened_at AS opened_at,
                    s.score AS score,
                    s.take_profit_1 AS tp1,
                    s.strategy_breakdown AS breakdown
                FROM positions p
                LEFT JOIN signals s
                    ON s.id = CAST(p."metadata"->>'source_signal_id' AS INTEGER)
                JOIN markets m ON m.id = p.market_id
                WHERE p.mode = 'paper'
                  AND p.status = 'closed'
                  AND p."metadata"->>'exit_reason' = 'stop_loss'
                  AND p.realized_pnl < 0
                ORDER BY p.opened_at
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
        init_sl = _dec(r["initial_sl"]) or _dec(r["exit_price"])
        tp1 = _dec(r["tp1"]) or _dec(r["pos_tp"])
        if tf not in TIMEFRAME_MS or entry is None or init_sl is None or tp1 is None:
            skipped += 1
            continue
        risk = abs(entry - init_sl)
        if risk == 0:
            skipped += 1
            continue
        # coerenza direzionale TP1 (long: tp1>entry; short: tp1<entry)
        if (r["side"] == "long" and tp1 <= entry) or (r["side"] == "short" and tp1 >= entry):
            skipped += 1
            continue
        trades.append(
            Trade(
                pos_id=r["pos_id"],
                symbol=r["symbol"],
                hl_coin=r["hl_coin"] or r["symbol"],
                side=r["side"],
                tf=tf,
                entry=entry,
                risk=risk,
                risk_stop=init_sl,
                tp1=tp1,
                opened_ms=int(r["opened_at"].timestamp() * 1000),
                score=int(r["score"]) if r["score"] is not None else None,
                strategy=_dominant_strategy(r["breakdown"]),
            )
        )
    if skipped:
        print(f"[warn] {skipped} stop scartati (tf/entry/risk/tp1 mancanti o incoerenti)")
    return trades


def _simulate(t: Trade) -> None:
    is_long = t.side == "long"
    r1 = float(abs(t.tp1 - t.entry) / t.risk)  # TP1 in multipli di R
    for m in MULTIPLIERS:
        if is_long:
            wide_stop = t.entry - m * t.risk
        else:
            wide_stop = t.entry + m * t.risk
        outcome = None
        for hi, lo, _close in t.bars[:N_MAX]:
            if is_long:
                stop_hit = lo <= wide_stop
                tp_hit = hi >= t.tp1
            else:
                stop_hit = hi >= wide_stop
                tp_hit = lo <= t.tp1
            if stop_hit:  # tie-break conservativo: stop prima del TP
                outcome = ("stop", -float(m))
                break
            if tp_hit:
                outcome = ("tp1", r1)
                break
        if outcome is None:  # ne' stop ne' TP entro N_MAX -> mark-to-market
            last_close = t.bars[min(len(t.bars), N_MAX) - 1][2]
            if is_long:
                r = float((last_close - t.entry) / t.risk)
            else:
                r = float((t.entry - last_close) / t.risk)
            outcome = ("open", r)
        t.sim[str(m)] = outcome


async def fetch(client: HyperliquidClient, sem: asyncio.Semaphore, t: Trade) -> None:
    interval_ms = TIMEFRAME_MS[t.tf]
    start = t.opened_ms
    end = t.opened_ms + (N_MAX + 2) * interval_ms
    async with sem:
        try:
            raw = await client.candles(t.hl_coin, t.tf, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] pos {t.pos_id} {t.hl_coin} {t.tf}: fetch fallito ({exc})")
            t.no_data = True
            return
        finally:
            await asyncio.sleep(THROTTLE_S)
    bars = []
    for row in sorted(raw, key=lambda x: int(x["t"])):
        if int(row["t"]) < t.opened_ms:  # includi la candela di entry e successive
            continue
        bars.append((Decimal(str(row["h"])), Decimal(str(row["l"])), Decimal(str(row["c"]))))
    if not bars:
        t.no_data = True
        return
    t.bars = bars
    _simulate(t)


async def run() -> None:
    trades = load_trades()
    print(f"Stop in perdita candidati: {len(trades)}")
    client = HyperliquidClient(get_settings())
    sem = asyncio.Semaphore(CONCURRENCY)
    await asyncio.gather(*(fetch(client, sem, t) for t in trades))
    ok = [t for t in trades if not t.no_data and t.sim]
    print(f"Simulati: {len(ok)} | no_data: {len(trades) - len(ok)}")
    print()
    _report(ok)


def _report(trades: list[Trade]) -> None:
    print("=" * 80)
    print(f"EFFETTO STOP PIU' LARGO — {len(trades)} stop in perdita, orizzonte {N_MAX} barre")
    print("uscita piena a TP1, tie-break intrabar conservativo (stop prima del TP)")
    print("=" * 80)
    print(f"{'stop =':>8} | {'TP1 rescued':>12} | {'ancora stop':>12} | {'aperto':>8} | {'R medio/trade':>14} | {'R totale':>10}")
    print("-" * 80)
    for m in MULTIPLIERS:
        key = str(m)
        rs = [t.sim[key] for t in trades]
        n = len(rs)
        tp1 = sum(1 for lbl, _ in rs if lbl == "tp1")
        stop = sum(1 for lbl, _ in rs if lbl == "stop")
        opn = sum(1 for lbl, _ in rs if lbl == "open")
        rvals = [r for _, r in rs]
        mean_r = statistics.mean(rvals)
        tot_r = sum(rvals)
        print(
            f"{float(m):>6.1f}xR | {tp1:>4} ({100*tp1/n:4.1f}%) | {stop:>4} ({100*stop/n:4.1f}%) | "
            f"{opn:>3} ({100*opn/n:4.1f}%) | {mean_r:>14.3f} | {tot_r:>10.1f}"
        )
    print()
    print("R medio/trade = expected value in multipli di rischio ORIGINALE (1R).")
    print("Baseline reale ≈ -1.0R (tutti stoppati a 1R). Se R medio sale verso 0/positivo")
    print("allargando, allora lo stop largo aiuta; se scende, peggiora.")
    print()

    # breakdown per timeframe del delta EV tra 1.0x e 2.0x
    print("— delta R medio (2.0xR vs 1.0xR) per timeframe —")
    by_tf: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_tf[t.tf].append(t)
    print(f"{'tf':>6} | {'n':>4} | {'R@1.0x':>8} | {'R@2.0x':>8} | {'delta':>8}")
    for tf, ts in sorted(by_tf.items(), key=lambda kv: -len(kv[1])):
        r1 = statistics.mean([t.sim['1.0'][1] for t in ts])
        r2 = statistics.mean([t.sim['2.0'][1] for t in ts])
        print(f"{tf:>6} | {len(ts):>4} | {r1:>8.3f} | {r2:>8.3f} | {r2 - r1:>+8.3f}")
    print()


if __name__ == "__main__":
    asyncio.run(run())

"""Fase 0 — Diagnosi edge out-of-sample.

Domanda a cui risponde: il sistema (e le singole strategie) hanno un edge che
regge FUORI CAMPIONE, o la performance e' overfitting al rumore in-sample?

Metodo: split temporale walk-forward (stessa macchina usata dalla calibrazione,
`SignalPerformanceAnalyzer.holdout_split_stats`). La frazione piu' vecchia
(train_ratio) e' il "training", la piu' recente e' il "test" (out-of-sample).
Un edge vero ha PF_test > 1 su un campione test non trascurabile.

Riporta tre livelli:
  1. Sistema intero (tutti i trade paper chiusi)
  2. Per cella `timeframe:strategy` (come la calibrazione chiavetta i pesi)
  3. Per strategia aggregata cross-timeframe
Piu' una vista PF time-weighted (half-life 50) per cogliere il regime recente.

Questo e' uno screening storico esplorativo: esamina molte celle e puo'
mescolare configurazioni passate. Anche una cella con train/test concordi e'
soltanto un candidato da pre-registrare in paper; non dimostra un edge e non
puo' autorizzare il live.

Uso:
    cd backend && ../venv/bin/python -m scripts.edge_oos_report
"""

from __future__ import annotations

from app.db.models import StrategyConfig
from app.db.session import SessionLocal
from app.engine.performance import PerformanceStats, SignalPerformanceAnalyzer

# Limite "tutti i trade": ampiamente sopra il totale storico (~926) per non troncare.
ALL = 100_000
TRAIN_RATIO = 0.7
MIN_PER_SIDE = 10  # campione minimo per lato (train/test) per fidarsi dello split
HALF_LIFE = 50


def _pf(stats: PerformanceStats | None) -> str:
    if stats is None or stats.profit_factor is None:
        return "  n/a"
    return f"{stats.profit_factor:5.2f}"


def _fmt(stats: PerformanceStats) -> str:
    pf = "inf" if stats.profit_factor is None else f"{stats.profit_factor:.2f}"
    return (
        f"n={stats.trades:<4} wr={stats.win_rate:.2f} "
        f"pf={pf:>5} pnl={stats.realized_pnl:>10.1f}"
    )


def _concordant(train: PerformanceStats | None, test: PerformanceStats | None) -> bool:
    if train is None or test is None or train.profit_factor is None or test.profit_factor is None:
        return False
    return train.profit_factor >= 0.95 and test.profit_factor >= 1.0


def _verdict(train: PerformanceStats | None, test: PerformanceStats | None) -> str:
    if test is None:
        return "INSUFF"
    if test.profit_factor is None:
        return "no-loss"
    if _concordant(train, test) and test.profit_factor >= 1.2:
        return "CANDIDATO"
    if test.profit_factor >= 1.2:
        return "TEST+"
    if test.profit_factor >= 1.0:
        return "marg+"
    if test.profit_factor >= 0.9:
        return "marg-"
    return "NEG"


def main() -> None:
    db = SessionLocal()
    try:
        an = SignalPerformanceAnalyzer()

        # ---- 1. Sistema intero -------------------------------------------------
        all_pos = an.closed_paper_positions(db, limit=ALL)
        overall = an.summarize_positions(all_pos)
        split = SignalPerformanceAnalyzer.holdout_split_stats(
            all_pos, train_ratio=TRAIN_RATIO, min_samples_per_side=MIN_PER_SIDE
        )
        tw = SignalPerformanceAnalyzer.time_weighted_summarize(all_pos, half_life=HALF_LIFE)
        print("=" * 78)
        print("FASE 0 — EDGE OUT-OF-SAMPLE")
        print("=" * 78)
        print(f"\n[1] SISTEMA INTERO  ({overall.trades} trade paper chiusi)")
        print(f"    full        : {_fmt(overall)}")
        if split is not None:
            tr, te = split
            print(f"    train (70%) : {_fmt(tr)}")
            print(f"    test  (30%) : {_fmt(te)}   <-- OUT-OF-SAMPLE")
        else:
            print("    holdout     : campione insufficiente")
        print(f"    time-weighted (half-life {HALF_LIFE}): {_fmt(tw)}")

        # ---- 2. Per cella timeframe:strategy ----------------------------------
        # Replica esattamente le chiavi che usa la calibrazione.
        strategies = db.query(StrategyConfig).all()
        timeframes = sorted({s.timeframe for s in strategies})
        cells: list[tuple[str, PerformanceStats, PerformanceStats | None, PerformanceStats | None]] = []
        for tf in timeframes:
            by_strat = an.strategy_positions(db, timeframe=tf, limit=ALL)
            for name, positions in by_strat.items():
                full = an.summarize_positions(positions)
                sp = SignalPerformanceAnalyzer.holdout_split_stats(
                    positions, train_ratio=TRAIN_RATIO, min_samples_per_side=MIN_PER_SIDE
                )
                tr = sp[0] if sp else None
                te = sp[1] if sp else None
                cells.append((f"{tf}:{name}", full, tr, te))

        enabled = {f"{s.timeframe}:{s.strategy_name}": s.enabled for s in strategies}
        # ordina per PF test desc (None in fondo)
        def _key(row):
            te = row[3]
            return (te.profit_factor if te and te.profit_factor is not None else -1.0)
        cells.sort(key=_key, reverse=True)

        print("\n[2] PER CELLA timeframe:strategy (ordinate per PF out-of-sample)")
        print(f"    {'cella':<34} {'on':<3} {'full':>6} {'train':>6} {'test':>6} {'n_te':>5}  verdetto")
        print("    " + "-" * 72)
        for key, full, tr, te in cells:
            on = "Y" if enabled.get(key) else "."
            n_te = te.trades if te else 0
            print(
                f"    {key:<34} {on:<3} {_pf(full)} {_pf(tr)} {_pf(te)} {n_te:>5}  {_verdict(tr, te)}"
            )

        # ---- 3. Per strategia aggregata cross-timeframe -----------------------
        agg = an.strategy_positions(db, timeframe=None, limit=ALL)
        agg_rows = []
        for name, positions in agg.items():
            full = an.summarize_positions(positions)
            sp = SignalPerformanceAnalyzer.holdout_split_stats(
                positions, train_ratio=TRAIN_RATIO, min_samples_per_side=MIN_PER_SIDE
            )
            te = sp[1] if sp else None
            agg_rows.append((name, full, sp[0] if sp else None, te))
        agg_rows.sort(key=lambda r: (r[3].profit_factor if r[3] and r[3].profit_factor is not None else -1.0), reverse=True)
        print("\n[3] PER STRATEGIA aggregata cross-timeframe (ordinate per PF out-of-sample)")
        print(f"    {'strategia':<28} {'full':>6} {'train':>6} {'test':>6} {'n_te':>5}  verdetto")
        print("    " + "-" * 66)
        for name, full, tr, te in agg_rows:
            n_te = te.trades if te else 0
            print(f"    {name:<28} {_pf(full)} {_pf(tr)} {_pf(te)} {n_te:>5}  {_verdict(tr, te)}")

        # ---- Riepilogo decisionale --------------------------------------------
        scored = [r for r in cells if r[3] is not None]
        edge_cells = [r for r in scored if r[3].profit_factor is not None and r[3].profit_factor >= 1.2]
        candidates = [r for r in scored if _concordant(r[2], r[3]) and r[3].profit_factor >= 1.2]
        print("\n" + "=" * 78)
        print("RIEPILOGO DECISIONALE")
        print("=" * 78)
        print(f"  Celle con campione test sufficiente (n_test>={MIN_PER_SIDE}): {len(scored)}/{len(cells)}")
        print(f"  Celle con PF_test >= 1.20 (grezzo, include rumore): {len(edge_cells)}")
        print(f"  Candidati storici train/test concordi (PF_test >= 1.20): {len(candidates)}")
        print("  -> Lo screening non e' evidenza: costi, selezione multipla e configurazioni passate")
        print("     richiedono un esperimento paper forward separato prima di qualsiasi conclusione.")
        if candidates:
            print("  -> Candidati da pre-registrare in paper:", ", ".join(r[0] for r in candidates[:10]))
        else:
            print(
                "  -> Nessun candidato storico train/test concorde: i 'PF_test alti' sono discordi"
                " su campioni minimi.\n"
                "     Priorita' = revisione/cull strategie + difesa recency-aware,"
                " NON tuning fine dei pesi."
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()

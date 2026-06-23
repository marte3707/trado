"""Fase 4 — Strumentazione: report di stabilità della calibrazione.

Misura quanto la calibrazione è "irrequieta" nel tempo, per distinguere
adattamento utile da whipsaw (flip-flop guidato dal rumore):

  1. Flip enable/disable per cella `timeframe:strategy` (riabilita/disabilita
     ripetute = isteresi insufficiente).
  2. Churn dei pesi: quante volte e di quanto sono stati mossi.
  3. Churn dei risk setting (min_signal_score / min_risk_reward / risk_pct).
  4. Forward attribution: esito dei trade paper raggruppato per regime di
     rischio attivo all'apertura (`metadata_json.risk_at_open`), per valutare
     se i periodi defensive proteggono davvero.

Sola lettura. Uso:
    cd backend && ../venv/bin/python -m scripts.calibration_stability_report
"""

from __future__ import annotations

from collections import defaultdict

from app.core.enums import OrderMode
from app.db.models import DecisionLog, Position
from app.db.session import SessionLocal


def main() -> None:
    db = SessionLocal()
    try:
        # Eventi di calibrazione applicati, in ordine cronologico.
        events = (
            db.query(DecisionLog)
            .filter(DecisionLog.event_type == "auto_calibration_applied")
            .order_by(DecisionLog.timestamp.asc())
            .all()
        )
        print("=" * 74)
        print("FASE 4 — REPORT STABILITÀ CALIBRAZIONE")
        print("=" * 74)
        print(f"Run 'applied' analizzati: {len(events)}")
        if events:
            print(f"Finestra: {events[0].timestamp:%Y-%m-%d %H:%M} → {events[-1].timestamp:%Y-%m-%d %H:%M}")

        enable_transitions: dict[str, list[bool]] = defaultdict(list)
        weight_moves: dict[str, list[tuple] ] = defaultdict(list)
        risk_moves: dict[str, int] = defaultdict(int)
        for ev in events:
            for ch in (ev.data or {}).get("changes", []):
                target = ch.get("target")
                key = ch.get("key")
                if target == "strategy_enabled":
                    enable_transitions[key].append(bool(ch.get("after")))
                elif target == "strategy_weight":
                    weight_moves[key].append((ch.get("before"), ch.get("after")))
                elif target == "risk_setting":
                    risk_moves[key] += 1

        # ---- 1. Flip enable/disable ------------------------------------------
        print("\n[1] FLIP enable/disable per cella (transizioni di stato)")
        flippers = []
        for key, seq in enable_transitions.items():
            # conta i cambi di stato effettivi nella sequenza
            flips = sum(1 for a, b in zip(seq, seq[1:], strict=False) if a != b)
            total = len(seq)
            disables = sum(1 for v in seq if v is False)
            enables = sum(1 for v in seq if v is True)
            flippers.append((key, total, flips, enables, disables))
        flippers.sort(key=lambda r: (r[2], r[1]), reverse=True)
        if flippers:
            print(f"    {'cella':<34} {'eventi':>6} {'flip':>5} {'on':>4} {'off':>4}")
            print("    " + "-" * 58)
            for key, total, flips, en, dis in flippers[:25]:
                flag = "  <-- WHIPSAW" if flips >= 2 else ""
                print(f"    {key:<34} {total:>6} {flips:>5} {en:>4} {dis:>4}{flag}")
            whipsaw = [f for f in flippers if f[2] >= 2]
            print(f"\n    Celle con >=2 flip di stato (whipsaw): {len(whipsaw)}")
        else:
            print("    nessuna transizione enable/disable registrata")

        # ---- 2. Churn pesi ---------------------------------------------------
        print("\n[2] CHURN pesi (celle più mosse)")
        churn = []
        for key, moves in weight_moves.items():
            n = len(moves)
            try:
                amp = sum(abs(int(b) - int(a)) for a, b in moves if a is not None and b is not None)
            except (TypeError, ValueError):
                amp = 0
            churn.append((key, n, amp))
        churn.sort(key=lambda r: r[1], reverse=True)
        print(f"    {'cella':<34} {'#cambi':>7} {'somma|Δw|':>10}")
        print("    " + "-" * 54)
        for key, n, amp in churn[:15]:
            print(f"    {key:<34} {n:>7} {amp:>10}")
        if not churn:
            print("    nessun cambio di peso registrato")

        # ---- 3. Churn risk setting ------------------------------------------
        print("\n[3] CHURN risk setting (n cambi nel periodo)")
        for key, n in sorted(risk_moves.items(), key=lambda r: r[1], reverse=True)[:12]:
            print(f"    {key:<40} {n:>5}")

        # ---- 4. Forward attribution per regime di rischio -------------------
        print("\n[4] FORWARD ATTRIBUTION — esito trade per regime di rischio all'apertura")
        rows = (
            db.query(Position)
            .filter(
                Position.mode == OrderMode.PAPER.value,
                Position.status == "closed",
                Position.realized_pnl.is_not(None),
            )
            .all()
        )
        buckets: dict[str, list[float]] = defaultdict(list)
        tagged = 0
        for p in rows:
            meta = p.metadata_json if isinstance(p.metadata_json, dict) else {}
            rao = meta.get("risk_at_open")
            if not isinstance(rao, dict):
                buckets["(no tag risk_at_open)"].append(float(p.realized_pnl))
                continue
            tagged += 1
            if rao.get("deep_defensive_active"):
                label = "deep_defensive"
            elif rao.get("recent_defensive_active"):
                label = "recent_defensive"
            elif rao.get("starvation_override_active"):
                label = "starvation_override"
            else:
                label = "normal"
            buckets[label].append(float(p.realized_pnl))
        print(f"    Trade chiusi: {len(rows)} | con risk_at_open: {tagged} "
              f"({100*tagged/len(rows):.0f}%)" if rows else "    nessun trade chiuso")
        print(f"    {'regime':<26} {'n':>5} {'pnl tot':>11} {'wr':>5} {'pf':>6}")
        print("    " + "-" * 56)
        for label, pnls in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True):
            n = len(pnls)
            wins = sum(1 for x in pnls if x > 0)
            gp = sum(x for x in pnls if x > 0)
            gl = abs(sum(x for x in pnls if x <= 0))
            pf = f"{gp/gl:.2f}" if gl > 0 else "inf"
            print(f"    {label:<26} {n:>5} {sum(pnls):>11.1f} {wins/n if n else 0:>5.2f} {pf:>6}")
        if tagged == 0:
            print("\n    NB: il tagging risk_at_open è appena stato introdotto — la sezione [4]"
                  "\n    si popolerà man mano che si aprono nuovi trade.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

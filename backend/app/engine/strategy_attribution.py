"""Attribuzione di performance per strategia dalle posizioni paper chiuse.

Aggrega ciò che il calibratore valuta solo cella per cella: performance per
strategia su tutti i timeframe, spaccata per direzione e timeframe, con trend
recente e metrica di partecipazione (quanti segnali con contributo allineato
diventano trade). Una posizione conta per ogni strategia che ha contribuito
nella direzione del segnale — i PnL per strategia quindi si sovrappongono e
vanno letti come "qualità dei trade a cui la strategia partecipa".
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Position, Signal
from app.engine.edge_profile import direction_family, linked_position_signals
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats

DEFAULT_RECENT_WINDOW = 150
DEFAULT_SIGNAL_LOOKBACK = 2000
# Sotto questa quota di segnali allineati che diventano trade, la strategia è
# considerata "inerte": contribuisce allo scoring ma di fatto non incide mai
# sul flusso tradato (es. soglie contrarian troppo estreme).
INERT_MIN_ALIGNED_SIGNALS = 50
INERT_MAX_TRADED_SHARE = 0.02


def _aligned_contribution(signal: Signal, strategy_name: str) -> float | None:
    breakdown = signal.strategy_breakdown if isinstance(signal.strategy_breakdown, dict) else {}
    payload = breakdown.get(strategy_name)
    if not isinstance(payload, dict):
        return None
    try:
        contribution = float(payload.get("contribution", 0.0))
    except (TypeError, ValueError):
        return None
    if contribution <= 0:
        return None
    if direction_family(payload.get("direction")) != direction_family(signal.direction):
        return None
    return contribution


def _strategy_names(signal: Signal) -> list[str]:
    breakdown = signal.strategy_breakdown if isinstance(signal.strategy_breakdown, dict) else {}
    return [name for name in breakdown if not name.startswith("_")]


def build_strategy_attribution(
    db: Session,
    *,
    lookback: int = 500,
    recent_window: int = DEFAULT_RECENT_WINDOW,
    signal_lookback: int = DEFAULT_SIGNAL_LOOKBACK,
) -> dict[str, Any]:
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=lookback)
    rows = [row for row in linked_position_signals(db, positions) if row.signal is not None]

    by_strategy: dict[str, list[Position]] = {}
    by_direction: dict[str, dict[str, list[Position]]] = {}
    by_timeframe: dict[str, dict[str, list[Position]]] = {}
    recent_by_strategy: dict[str, list[Position]] = {}
    contribution_totals: dict[str, float] = {}

    for index, row in enumerate(rows):
        signal = row.signal
        assert signal is not None
        for name in _strategy_names(signal):
            contribution = _aligned_contribution(signal, name)
            if contribution is None:
                continue
            by_strategy.setdefault(name, []).append(row.position)
            contribution_totals[name] = contribution_totals.get(name, 0.0) + contribution
            family = direction_family(signal.direction)
            if family is not None:
                by_direction.setdefault(name, {}).setdefault(family, []).append(row.position)
            by_timeframe.setdefault(name, {}).setdefault(signal.timeframe, []).append(row.position)
            if index < recent_window:
                recent_by_strategy.setdefault(name, []).append(row.position)

    # Partecipazione: segnali recenti con contributo allineato, anche se mai tradati.
    recent_signals = (
        db.query(Signal).order_by(Signal.id.desc()).limit(signal_lookback).all()
    )
    aligned_signal_counts: dict[str, int] = {}
    for signal in recent_signals:
        for name in _strategy_names(signal):
            if _aligned_contribution(signal, name) is not None:
                aligned_signal_counts[name] = aligned_signal_counts.get(name, 0) + 1

    strategies: dict[str, Any] = {}
    names = set(by_strategy) | set(aligned_signal_counts)
    for name in sorted(names):
        strategy_positions = by_strategy.get(name, [])
        stats = serialize_stats(analyzer.summarize_positions(strategy_positions))
        aligned_signals = aligned_signal_counts.get(name, 0)
        traded_share = stats["trades"] / aligned_signals if aligned_signals > 0 else None
        inert = (
            aligned_signals >= INERT_MIN_ALIGNED_SIGNALS
            and (traded_share is None or traded_share <= INERT_MAX_TRADED_SHARE)
        )
        strategies[name] = {
            "overall": stats,
            "avg_contribution": (
                round(contribution_totals.get(name, 0.0) / stats["trades"], 2)
                if stats["trades"] > 0
                else None
            ),
            "recent": serialize_stats(
                analyzer.summarize_positions(recent_by_strategy.get(name, []))
            ),
            "by_direction": {
                family: serialize_stats(analyzer.summarize_positions(group))
                for family, group in sorted(by_direction.get(name, {}).items())
            },
            "by_timeframe": {
                timeframe: serialize_stats(analyzer.summarize_positions(group))
                for timeframe, group in sorted(by_timeframe.get(name, {}).items())
            },
            "participation": {
                "aligned_signals_recent": aligned_signals,
                "traded_positions": stats["trades"],
                "traded_share": round(traded_share, 4) if traded_share is not None else None,
                "inert": inert,
            },
        }

    return {
        "filters": {
            "lookback": lookback,
            "recent_window": recent_window,
            "signal_lookback": signal_lookback,
        },
        "positions_linked": len(rows),
        "strategies": strategies,
        "note": (
            "L'attribuzione conta una posizione per ogni strategia con contributo "
            "allineato alla direzione del segnale: i PnL si sovrappongono tra strategie."
        ),
    }

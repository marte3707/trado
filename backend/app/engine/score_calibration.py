"""Calibrazione empirica score → Expected R-multiple.

Lo score grezzo del SignalEngine (0-100) è un indice di confluenza, non una
probabilità statistica. Questo modulo costruisce una mappa
`(direction, timeframe, score_bucket) → Expected R-multiple` usando le
posizioni paper chiuse, con shrinkage bayesiano per i bucket sotto-rappresentati.

L'output è consumato sia in UI (DiagnosticsTab) sia come gate opzionale
nel signal_engine (`min_signal_expected_r`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import Direction
from app.db.models import Position, Signal
from app.engine.edge_profile import direction_family, linked_position_signals, score_bucket
from app.engine.performance import SignalPerformanceAnalyzer

DEFAULT_PRIOR_STRENGTH = 20.0
DEFAULT_MIN_SAMPLES_FOR_USE = 10
DEFAULT_LOOKBACK = 500


@dataclass(frozen=True)
class CalibratedMetrics:
    """Metriche calibrate per un singolo bucket."""

    key: str
    direction: str
    timeframe: str
    score_bucket: str
    n_samples: int
    wins: int
    raw_win_rate: float
    shrunk_win_rate: float
    mean_r_win: float
    mean_r_loss: float
    expected_r: float
    confidence: str  # "low" | "medium" | "high"


@dataclass(frozen=True)
class CalibrationTable:
    """Tabella di calibrazione completa: indicizzata per chiave."""

    buckets: dict[str, CalibratedMetrics]
    computed_at: datetime
    lookback_positions: int
    excluded_count: int
    prior_strength: float
    min_samples_for_use: int

    def lookup(
        self, *, direction: Direction | str, timeframe: str, score: int
    ) -> CalibratedMetrics | None:
        family = direction_family(direction)
        if family is None:
            return None
        key = f"{family}:{timeframe}:{score_bucket(score)}"
        return self.buckets.get(key)


def _position_r_multiple(position: Position) -> float | None:
    """R-multiple per una posizione chiusa.

    Preferenze (in ordine):
      1. realized_pnl / metadata.initial_position_risk (rischio USD pre-calcolato)
      2. realized_pnl / (|entry - initial_stop_loss| × initial_size)
      3. realized_pnl / (entry_atr × initial_size)

    Nota: `Position.size` viene azzerato dopo la chiusura paper. Si usano
    `initial_size`, `initial_position_risk` e `initial_stop_loss` dai metadata,
    con fallback ai campi della Position solo se i metadata sono assenti.
    """
    pnl = position.realized_pnl
    if pnl is None:
        return None
    try:
        pnl_f = float(pnl)
    except (TypeError, ValueError):
        return None
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}

    raw_initial_risk = metadata.get("initial_position_risk")
    if raw_initial_risk is not None:
        try:
            risk_usd = float(raw_initial_risk)
        except (TypeError, ValueError):
            risk_usd = None
        if risk_usd is not None and risk_usd > 0:
            return pnl_f / risk_usd

    raw_initial_size = metadata.get("initial_size")
    initial_size: float | None = None
    if raw_initial_size is not None:
        try:
            initial_size = float(raw_initial_size)
        except (TypeError, ValueError):
            initial_size = None
    if initial_size is None or initial_size <= 0:
        try:
            fallback_size = float(position.size) if position.size is not None else 0.0
        except (TypeError, ValueError):
            fallback_size = 0.0
        if fallback_size > 0:
            initial_size = fallback_size
    if initial_size is None or initial_size <= 0:
        return None

    try:
        entry_f = float(position.entry_price) if position.entry_price is not None else None
    except (TypeError, ValueError):
        entry_f = None

    raw_initial_sl = metadata.get("initial_stop_loss")
    sl_candidate: float | None = None
    if raw_initial_sl is not None:
        try:
            sl_candidate = float(raw_initial_sl)
        except (TypeError, ValueError):
            sl_candidate = None
    if sl_candidate is None and position.stop_loss is not None:
        try:
            sl_candidate = float(position.stop_loss)
        except (TypeError, ValueError):
            sl_candidate = None
    if entry_f is not None and sl_candidate is not None:
        risk_per_unit = abs(entry_f - sl_candidate)
        initial_risk = risk_per_unit * initial_size
        if initial_risk > 0:
            return pnl_f / initial_risk

    raw_atr = metadata.get("entry_atr")
    if raw_atr is None:
        return None
    try:
        atr_f = float(raw_atr)
    except (TypeError, ValueError):
        return None
    if atr_f <= 0:
        return None
    initial_risk = atr_f * initial_size
    if initial_risk <= 0:
        return None
    return pnl_f / initial_risk


def _confidence_label(n_samples: int, min_samples_for_use: int) -> str:
    if n_samples < min_samples_for_use:
        return "low"
    if n_samples < min_samples_for_use * 3:
        return "medium"
    return "high"


def compute_calibration_table(
    positions: list[Position],
    signals_by_id: dict[int, Signal],
    *,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
    min_samples_for_use: int = DEFAULT_MIN_SAMPLES_FOR_USE,
    enforce_monotonic: bool = False,
    lookback_positions: int = DEFAULT_LOOKBACK,
) -> CalibrationTable:
    """Costruisce la calibration table dai dati grezzi.

    Aggrega per `(direction_family, timeframe, score_bucket)`. Per ogni bucket
    calcola win rate con Bayesian shrinkage (prior Beta(α₀, β₀) centrato su 0.5),
    mean R-multiple di vittorie e perdite separatamente, e l'expected R.
    """
    if prior_strength < 0:
        prior_strength = 0.0
    alpha_prior = prior_strength / 2.0

    grouped: dict[tuple[str, str, str], list[float]] = {}
    excluded = 0
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        signal_id_raw = metadata.get("source_signal_id")
        if signal_id_raw is None:
            excluded += 1
            continue
        try:
            signal_id = int(signal_id_raw)
        except (TypeError, ValueError):
            excluded += 1
            continue
        signal = signals_by_id.get(signal_id)
        if signal is None:
            excluded += 1
            continue
        family = direction_family(signal.direction)
        if family is None:
            excluded += 1
            continue
        r = _position_r_multiple(position)
        if r is None:
            excluded += 1
            continue
        bucket = score_bucket(signal.score)
        grouped.setdefault((family, signal.timeframe, bucket), []).append(r)

    buckets: dict[str, CalibratedMetrics] = {}
    for (family, timeframe, bucket), r_values in grouped.items():
        n = len(r_values)
        wins_r = [r for r in r_values if r > 0]
        losses_r = [r for r in r_values if r <= 0]
        wins = len(wins_r)
        raw_win_rate = wins / n if n > 0 else 0.0
        shrunk_win_rate = (alpha_prior + wins) / (prior_strength + n) if (prior_strength + n) > 0 else 0.5
        mean_r_win = sum(wins_r) / len(wins_r) if wins_r else 0.0
        mean_r_loss = sum(losses_r) / len(losses_r) if losses_r else 0.0
        expected_r = shrunk_win_rate * mean_r_win + (1.0 - shrunk_win_rate) * mean_r_loss
        key = f"{family}:{timeframe}:{bucket}"
        buckets[key] = CalibratedMetrics(
            key=key,
            direction=family,
            timeframe=timeframe,
            score_bucket=bucket,
            n_samples=n,
            wins=wins,
            raw_win_rate=raw_win_rate,
            shrunk_win_rate=shrunk_win_rate,
            mean_r_win=mean_r_win,
            mean_r_loss=mean_r_loss,
            expected_r=expected_r,
            confidence=_confidence_label(n, min_samples_for_use),
        )

    if enforce_monotonic:
        buckets = _enforce_monotonic(buckets)

    return CalibrationTable(
        buckets=buckets,
        computed_at=datetime.now(UTC),
        lookback_positions=lookback_positions,
        excluded_count=excluded,
        prior_strength=prior_strength,
        min_samples_for_use=min_samples_for_use,
    )


_BUCKET_ORDER = ("50-59", "60-69", "70-79", "80+")


def _enforce_monotonic(buckets: dict[str, CalibratedMetrics]) -> dict[str, CalibratedMetrics]:
    """Pool-adjacent-violators per `expected_r` su score_bucket fissa (direction, timeframe).

    Garantisce che `expected_r` sia non-decrescente al crescere dello score, smussando
    le inversioni dovute al rumore campionario. Non altera `n_samples`, `raw_win_rate`,
    `mean_r_win/loss` — modifica solo `expected_r` e `shrunk_win_rate` di conseguenza.
    """
    by_dir_tf: dict[tuple[str, str], list[CalibratedMetrics]] = {}
    for metrics in buckets.values():
        by_dir_tf.setdefault((metrics.direction, metrics.timeframe), []).append(metrics)

    adjusted: dict[str, CalibratedMetrics] = {}
    for (_direction, _timeframe), entries in by_dir_tf.items():
        ordered = sorted(
            entries,
            key=lambda m: _BUCKET_ORDER.index(m.score_bucket) if m.score_bucket in _BUCKET_ORDER else -1,
        )
        values = [m.expected_r for m in ordered]
        weights = [float(m.n_samples) for m in ordered]
        smoothed = _pool_adjacent_violators(values, weights)
        for original, new_er in zip(ordered, smoothed, strict=False):
            adjusted[original.key] = CalibratedMetrics(
                key=original.key,
                direction=original.direction,
                timeframe=original.timeframe,
                score_bucket=original.score_bucket,
                n_samples=original.n_samples,
                wins=original.wins,
                raw_win_rate=original.raw_win_rate,
                shrunk_win_rate=original.shrunk_win_rate,
                mean_r_win=original.mean_r_win,
                mean_r_loss=original.mean_r_loss,
                expected_r=new_er,
                confidence=original.confidence,
            )
    return adjusted


def _pool_adjacent_violators(values: list[float], weights: list[float]) -> list[float]:
    """Isotonic regression (non-decreasing) via PAV.

    Ogni blocco tiene traccia di `(weighted_mean, total_weight, count)` per poter
    riallocare il valore alle posizioni originali coperte.
    """
    if not values:
        return []
    blocks: list[tuple[float, float, int]] = [
        (v, max(w, 1e-9), 1) for v, w in zip(values, weights, strict=False)
    ]
    i = 0
    while i < len(blocks) - 1:
        v_i, w_i, c_i = blocks[i]
        v_next, w_next, c_next = blocks[i + 1]
        if v_i <= v_next:
            i += 1
            continue
        merged_w = w_i + w_next
        merged_v = (v_i * w_i + v_next * w_next) / merged_w
        blocks[i : i + 2] = [(merged_v, merged_w, c_i + c_next)]
        if i > 0:
            i -= 1
    expanded: list[float] = []
    for v, _w, count in blocks:
        expanded.extend([v] * count)
    return expanded[: len(values)]


def serialize_metrics(metrics: CalibratedMetrics) -> dict[str, Any]:
    def _float_or_none(value: float) -> float | None:
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)

    return {
        "key": metrics.key,
        "direction": metrics.direction,
        "timeframe": metrics.timeframe,
        "score_bucket": metrics.score_bucket,
        "n_samples": metrics.n_samples,
        "wins": metrics.wins,
        "raw_win_rate": _float_or_none(metrics.raw_win_rate),
        "shrunk_win_rate": _float_or_none(metrics.shrunk_win_rate),
        "mean_r_win": _float_or_none(metrics.mean_r_win),
        "mean_r_loss": _float_or_none(metrics.mean_r_loss),
        "expected_r": _float_or_none(metrics.expected_r),
        "confidence": metrics.confidence,
    }


def serialize_table(table: CalibrationTable) -> dict[str, Any]:
    return {
        "buckets": {key: serialize_metrics(value) for key, value in sorted(table.buckets.items())},
        "computed_at": table.computed_at.isoformat(),
        "lookback_positions": table.lookback_positions,
        "excluded_count": table.excluded_count,
        "prior_strength": table.prior_strength,
        "min_samples_for_use": table.min_samples_for_use,
    }


def build_calibration_table_from_db(
    db: Session,
    *,
    lookback: int = DEFAULT_LOOKBACK,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
    min_samples_for_use: int = DEFAULT_MIN_SAMPLES_FOR_USE,
    enforce_monotonic: bool = False,
    analyzer: SignalPerformanceAnalyzer | None = None,
) -> CalibrationTable:
    """Carica le posizioni paper chiuse e ne deriva la calibration table."""
    analyzer = analyzer or SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=lookback)
    rows = linked_position_signals(db, positions)
    signals_by_id: dict[int, Signal] = {}
    filtered_positions: list[Position] = []
    for row in rows:
        if row.signal is None:
            continue
        signals_by_id[row.signal.id] = row.signal
        filtered_positions.append(row.position)
    return compute_calibration_table(
        filtered_positions,
        signals_by_id,
        prior_strength=prior_strength,
        min_samples_for_use=min_samples_for_use,
        enforce_monotonic=enforce_monotonic,
        lookback_positions=lookback,
    )


# ---------------------------------------------------------------------------
# Cache in-memory
# ---------------------------------------------------------------------------

_CACHE: dict[str, Any] = {"table": None, "computed_at": None}
_CACHE_TTL_SECONDS = 30 * 60  # 30 minuti


def cached_calibration_table(
    db: Session,
    *,
    force_refresh: bool = False,
    lookback: int = DEFAULT_LOOKBACK,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
    min_samples_for_use: int = DEFAULT_MIN_SAMPLES_FOR_USE,
) -> CalibrationTable:
    now = datetime.now(UTC)
    table: CalibrationTable | None = _CACHE.get("table")
    computed_at: datetime | None = _CACHE.get("computed_at")
    is_stale = (
        force_refresh
        or table is None
        or computed_at is None
        or (now - computed_at).total_seconds() > _CACHE_TTL_SECONDS
    )
    if not is_stale:
        assert table is not None
        return table
    table = build_calibration_table_from_db(
        db,
        lookback=lookback,
        prior_strength=prior_strength,
        min_samples_for_use=min_samples_for_use,
    )
    _CACHE["table"] = table
    _CACHE["computed_at"] = now
    return table


def invalidate_calibration_cache() -> None:
    _CACHE["table"] = None
    _CACHE["computed_at"] = None

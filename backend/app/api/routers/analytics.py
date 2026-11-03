"""Analytics: outcome profile, calibrazioni, attribution, correlazioni macro."""
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.runtime_settings import (
    setting_value,
)
from app.db.models import (
    Candle,
    DecisionLog,
    Market,
    Position,
    Signal,
    StrategyConfig,
)
from app.db.session import get_db
from app.engine.diagnostics import build_diagnostics_report, build_equity_slope_diagnostics
from app.engine.edge_profile import build_outcome_profile
from app.engine.experiment import forward_gate_policy
from app.engine.experiment_outcomes import experiment_outcomes_report
from app.engine.live_eligibility import build_live_eligibility_report
from app.engine.macro_direction import btc_macro_direction, suspended_penalty_family
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats
from app.engine.pre_tp1_validation import (
    DEFAULT_LOOKBACK as PRE_TP1_DEFAULT_LOOKBACK,
)
from app.engine.pre_tp1_validation import (
    build_experiment_pre_tp1_validation_table_from_db,
    build_pre_tp1_validation_table_from_db,
)
from app.engine.pre_tp1_validation import (
    serialize_table as serialize_pre_tp1_table,
)
from app.engine.score_calibration import (
    DEFAULT_LOOKBACK as CALIBRATION_DEFAULT_LOOKBACK,
)
from app.engine.score_calibration import (
    build_calibration_table_from_db,
    build_experiment_calibration_table_from_db,
    serialize_table,
)
from app.engine.strategy_attribution import build_strategy_attribution
from app.services.trade_cost import aggregate_live_costs, serialize_live_cost_report

_logger = structlog.get_logger()

router = APIRouter()


@router.get("/analytics/experiments")
def experiment_analytics(
    version: str | None = None,
    recent: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return experiment_outcomes_report(db, version=version, recent=recent)


@router.get("/analytics/signals")
def signal_analytics(
    timeframe: str | None = None,
    market_id: int | None = None,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, timeframe=timeframe, market_id=market_id, limit=limit)
    signal_ids = {
        int(position.metadata_json["source_signal_id"])
        for position in positions
        if isinstance(position.metadata_json, dict)
        and position.metadata_json.get("source_signal_id") is not None
    }
    signals_by_id = (
        {signal.id: signal for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )

    by_timeframe: dict[str, list[Position]] = {}
    by_direction: dict[str, list[Position]] = {}
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        source_timeframe = metadata.get("source_timeframe")
        if isinstance(source_timeframe, str):
            by_timeframe.setdefault(source_timeframe, []).append(position)
        source_signal_id = metadata.get("source_signal_id")
        if source_signal_id is not None:
            try:
                signal = signals_by_id.get(int(source_signal_id))
            except (TypeError, ValueError):
                signal = None
        else:
            signal = None
        if signal is not None:
            by_direction.setdefault(signal.direction, []).append(position)

    return {
        "filters": {"timeframe": timeframe, "market_id": market_id, "limit": limit},
        "overall": serialize_stats(analyzer.summarize_positions(positions)),
        "by_strategy": {
            strategy_name: serialize_stats(stats)
            for strategy_name, stats in analyzer.strategy_stats(db, timeframe, market_id, limit).items()
        },
        "by_timeframe": {
            key: serialize_stats(analyzer.summarize_positions(value))
            for key, value in sorted(by_timeframe.items())
        },
        "by_direction": {
            key: serialize_stats(analyzer.summarize_positions(value))
            for key, value in sorted(by_direction.items())
        },
    }


@router.get("/analytics/diagnostics")
def analytics_diagnostics(
    lookback: int = Query(200, ge=10, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    return build_diagnostics_report(db, lookback=lookback)


@router.get("/analytics/equity-slope")
def analytics_equity_slope(
    lookback: int = Query(500, ge=50, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    return build_equity_slope_diagnostics(db, lookback=lookback)


@router.get("/analytics/outcome-profile")
def analytics_outcome_profile(
    lookback: int = Query(500, ge=50, le=2000),
    mode: str | None = Query(None, pattern="^(paper|live)$"),
    db: Session = Depends(get_db),
) -> dict:
    return build_outcome_profile(db, lookback=lookback, mode=mode)


@router.get("/analytics/live-trade-costs")
def analytics_live_trade_costs(
    lookback: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    """Scompone il PnL live (lordo da closedPnl) in fee, funding e slippage per
    esporre il PnL netto reale dei trade live chiusi."""
    report = aggregate_live_costs(
        db, taker_fee_rate=get_settings().live_taker_fee_rate, limit=lookback
    )
    return serialize_live_cost_report(report)


@router.get("/analytics/strategy-attribution")
def analytics_strategy_attribution(
    lookback: int = Query(500, ge=50, le=2000),
    recent_window: int = Query(150, ge=20, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """Performance per strategia (overall/direzione/timeframe/recente) +
    metrica di partecipazione al flusso tradato. Vista aggregata che il
    calibratore per-cella non calcola."""
    return build_strategy_attribution(db, lookback=lookback, recent_window=recent_window)


def _pf_value(payload: dict[str, Any] | None) -> float | None:
    if not payload:
        return None
    raw = payload.get("profit_factor")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _oos_verdict(
    train_payload: dict[str, Any] | None,
    test_payload: dict[str, Any] | None,
) -> str:
    pf = _pf_value(test_payload)
    if test_payload is None or int(test_payload.get("trades", 0)) <= 0:
        return "INSUFF"
    if pf is None:
        return "no-loss"
    if pf >= 1.2 and _oos_concordant(train_payload, test_payload):
        return "ROBUSTO"
    if pf >= 1.2:
        return "TEST+"
    if pf >= 1.0:
        return "marg+"
    if pf >= 0.9:
        return "marg-"
    return "NEG"


def _oos_concordant(train_payload: dict[str, Any] | None, test_payload: dict[str, Any] | None) -> bool:
    train_pf = _pf_value(train_payload)
    test_pf = _pf_value(test_payload)
    return train_pf is not None and test_pf is not None and train_pf >= 0.95 and test_pf >= 1.0


def _cell_classification(
    *,
    full: dict[str, Any],
    train: dict[str, Any] | None,
    test: dict[str, Any] | None,
) -> str:
    full_pf = _pf_value(full)
    test_pf = _pf_value(test)
    test_trades = int(test.get("trades", 0)) if test else 0
    full_trades = int(full.get("trades", 0))
    if test_trades >= 10 and _oos_concordant(train, test) and test_pf is not None and test_pf >= 1.2:
        return "robust"
    if test_trades >= 10 and test_pf is not None and test_pf >= 1.2:
        return "promising"
    if (test_trades >= 10 and test_pf is not None and test_pf < 0.9) or (
        full_trades >= 20 and full_pf is not None and full_pf < 0.8
    ):
        return "negative"
    return "insufficient"


def _strategy_classification(payload: dict[str, Any]) -> str:
    participation = payload.get("participation") or {}
    if participation.get("inert") is True:
        return "inert"
    overall = payload.get("overall") or {}
    recent = payload.get("recent") or {}
    overall_pf = _pf_value(overall)
    recent_pf = _pf_value(recent)
    overall_trades = int(overall.get("trades", 0))
    recent_trades = int(recent.get("trades", 0))
    if (
        overall_trades >= 50
        and recent_trades >= 20
        and overall_pf is not None
        and recent_pf is not None
        and overall_pf >= 1.2
        and recent_pf >= 1.0
    ):
        return "robust"
    if (
        (recent_trades >= 20 and recent_pf is not None and recent_pf < 0.8)
        or (overall_trades >= 20 and overall_pf is not None and overall_pf < 0.8)
    ):
        return "negative"
    if overall_trades >= 20 and overall_pf is not None and overall_pf >= 1.0:
        return "promising"
    return "insufficient"


@router.get("/analytics/strategy-edge")
def analytics_strategy_edge(
    lookback: int = Query(2000, ge=50, le=100000),
    recent_window: int = Query(150, ge=20, le=500),
    min_test_samples: int = Query(10, ge=5, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """Sintesi OOS + attribution per decidere quali strategie/celle meritano fiducia."""
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=lookback)
    overall = analyzer.summarize_positions(positions)
    split = SignalPerformanceAnalyzer.holdout_split_stats(
        positions,
        train_ratio=0.7,
        min_samples_per_side=min_test_samples,
    )
    train_stats = split[0] if split else None
    test_stats = split[1] if split else None
    time_weighted = SignalPerformanceAnalyzer.time_weighted_summarize(
        positions,
        half_life=50,
    )

    configs = db.query(StrategyConfig).all()
    enabled_by_cell = {
        f"{config.timeframe}:{config.strategy_name}": bool(config.enabled)
        for config in configs
    }
    cells: list[dict[str, Any]] = []
    for timeframe in sorted({config.timeframe for config in configs}):
        by_strategy = analyzer.strategy_positions(db, timeframe=timeframe, limit=lookback)
        for strategy_name, strategy_positions in by_strategy.items():
            full_stats = analyzer.summarize_positions(strategy_positions)
            cell_split = SignalPerformanceAnalyzer.holdout_split_stats(
                strategy_positions,
                train_ratio=0.7,
                min_samples_per_side=min_test_samples,
            )
            cell_train = serialize_stats(cell_split[0]) if cell_split else None
            cell_test = serialize_stats(cell_split[1]) if cell_split else None
            full_payload = serialize_stats(full_stats)
            classification = _cell_classification(
                full=full_payload,
                train=cell_train,
                test=cell_test,
            )
            key = f"{timeframe}:{strategy_name}"
            cells.append(
                {
                    "key": key,
                    "timeframe": timeframe,
                    "strategy": strategy_name,
                    "enabled": enabled_by_cell.get(key, False),
                    "classification": classification,
                    "verdict": _oos_verdict(cell_train, cell_test),
                    "full": full_payload,
                    "train": cell_train,
                    "test": cell_test,
                }
            )

    attribution = build_strategy_attribution(
        db,
        lookback=lookback,
        recent_window=recent_window,
    )
    strategies: list[dict[str, Any]] = []
    for strategy_name, payload in sorted((attribution.get("strategies") or {}).items()):
        strategies.append(
            {
                "name": strategy_name,
                "classification": _strategy_classification(payload),
                "overall": payload.get("overall") or {},
                "recent": payload.get("recent") or {},
                "avg_contribution": payload.get("avg_contribution"),
                "participation": payload.get("participation") or {},
            }
        )

    cell_counts = dict.fromkeys(("robust", "promising", "negative", "insufficient"), 0)
    for cell in cells:
        cell_counts[cell["classification"]] = cell_counts.get(cell["classification"], 0) + 1
    strategy_counts = dict.fromkeys(
        ("robust", "promising", "negative", "inert", "insufficient"), 0
    )
    for strategy in strategies:
        strategy_counts[strategy["classification"]] = (
            strategy_counts.get(strategy["classification"], 0) + 1
        )

    return {
        "filters": {
            "lookback": lookback,
            "recent_window": recent_window,
            "min_test_samples": min_test_samples,
        },
        "overall": {
            "full": serialize_stats(overall),
            "train": serialize_stats(train_stats) if train_stats else None,
            "test": serialize_stats(test_stats) if test_stats else None,
            "time_weighted": serialize_stats(time_weighted),
            "verdict": _oos_verdict(
                serialize_stats(train_stats) if train_stats else None,
                serialize_stats(test_stats) if test_stats else None,
            ),
            "classification": _cell_classification(
                full=serialize_stats(overall),
                train=serialize_stats(train_stats) if train_stats else None,
                test=serialize_stats(test_stats) if test_stats else None,
            ),
        },
        "summary": {
            "cells": cell_counts,
            "strategies": strategy_counts,
            "positions_linked": attribution.get("positions_linked", 0),
        },
        "cells": cells,
        "strategies": strategies,
        "note": (
            "robust richiede train/test concordi e PF_test >= 1.20; promising include "
            "edge OOS non concorde o edge totale positivo; negative segnala PF recente "
            "o OOS sotto soglia."
        ),
    }


@router.get("/analytics/live-eligibility")
def analytics_live_eligibility(
    lookback: int = Query(2000, ge=50, le=100000),
    recent_window: int = Query(200, ge=20, le=1000),
    min_trades: int = Query(30, ge=5, le=500),
    min_recent_trades: int = Query(10, ge=3, le=200),
    min_net_profit_factor: float = Query(1.05, ge=0.0, le=10.0),
    min_recent_net_profit_factor: float = Query(1.0, ge=0.0, le=10.0),
    min_avg_net_r: float = Query(0.02, ge=-5.0, le=5.0),
    min_recent_avg_net_r: float = Query(0.0, ge=-5.0, le=5.0),
    configuration_hash: str | None = Query(None, min_length=1, max_length=64),
    experiment_version: str | None = Query(None, min_length=1, max_length=64),
    db: Session = Depends(get_db),
) -> dict:
    """Preview read-only delle coorti paper candidabili al live.

    La metrica è paper-first e cost-adjusted: ogni trade viene normalizzato in R
    e decurtato di fee taker round-trip simulate. Non cambia guardrail o live
    execution: serve a decidere cosa promuovere manualmente.
    """
    return build_live_eligibility_report(
        db,
        lookback=lookback,
        recent_window=recent_window,
        taker_fee_rate=get_settings().live_taker_fee_rate,
        min_trades=min_trades,
        min_recent_trades=min_recent_trades,
        min_net_profit_factor=min_net_profit_factor,
        min_recent_net_profit_factor=min_recent_net_profit_factor,
        min_avg_net_r=min_avg_net_r,
        min_recent_avg_net_r=min_recent_avg_net_r,
        configuration_hash=configuration_hash,
        experiment_version=experiment_version,
    )


@router.get("/analytics/macro-direction")
def analytics_macro_direction(db: Session = Depends(get_db)) -> dict:
    """Direzione macro strutturale BTC usata per la sospensione regime-aware
    delle penalità direzionali."""
    risk = setting_value(db, "risk")
    macro = btc_macro_direction(db, risk)
    return {
        **macro,
        "suspended_family": suspended_penalty_family(macro, risk),
    }


@router.get("/analytics/expected-r-calibration")
def analytics_expected_r_calibration(
    lookback: int = Query(CALIBRATION_DEFAULT_LOOKBACK, ge=50, le=2000),
    enforce_monotonic: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict:
    """Calibrazione empirica score → Expected R-multiple per (direction, timeframe, score_bucket).

    Distinto dall'endpoint legacy `/analytics/score-calibration` (binning PnL grezzo
    senza shrinkage). Questo applica Bayesian shrinkage e usa R-multiple normalizzato
    per rischio iniziale invece di PnL assoluto.
    """
    policy = forward_gate_policy(db)
    if bool(policy.get("forward_ready", False)):
        table = build_experiment_calibration_table_from_db(
            db,
            experiment_version=str(policy["version"]),
            assignment=str(policy["assignment"]),
            lookback=lookback,
        )
    else:
        table = build_calibration_table_from_db(
            db,
            lookback=lookback,
            enforce_monotonic=enforce_monotonic,
        )
    return {**serialize_table(table), "forward_gate": policy}


@router.get("/analytics/pre-tp1-validation")
def analytics_pre_tp1_validation(
    lookback: int = Query(PRE_TP1_DEFAULT_LOOKBACK, ge=50, le=2000),
    min_samples: int = Query(30, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """Empirical TP1-before-stop cohorts used by the pre-entry validation gate."""
    policy = forward_gate_policy(db)
    if bool(policy.get("forward_ready", False)):
        table = build_experiment_pre_tp1_validation_table_from_db(
            db,
            experiment_version=str(policy["version"]),
            assignment=str(policy["assignment"]),
            lookback=lookback,
            min_samples_for_use=min_samples,
        )
    else:
        table = build_pre_tp1_validation_table_from_db(
            db,
            lookback=lookback,
            min_samples_for_use=min_samples,
        )
    return {**serialize_pre_tp1_table(table), "forward_gate": policy}


@router.get("/analytics/score-calibration")
def analytics_score_calibration(
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=limit)

    signal_ids = {
        int(position.metadata_json["source_signal_id"])
        for position in positions
        if isinstance(position.metadata_json, dict)
        and position.metadata_json.get("source_signal_id") is not None
    }
    signals_by_id = (
        {signal.id: signal for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )

    pairs: list[tuple[int, float, Any]] = []
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        source_signal_id = metadata.get("source_signal_id")
        if source_signal_id is None:
            continue
        try:
            signal = signals_by_id.get(int(source_signal_id))
        except (TypeError, ValueError):
            continue
        if signal is None or signal.score is None:
            continue
        pnl = float(position.realized_pnl or 0)
        closed_at = position.closed_at or position.opened_at
        pairs.append((signal.score, pnl, closed_at))

    if not pairs:
        return {"buckets": [], "correlation": None, "total_trades": 0, "by_week": []}

    bucket_data: dict[int, list[float]] = {}
    for score, pnl, _ in pairs:
        bucket = (score // 10) * 10
        bucket_data.setdefault(bucket, []).append(pnl)

    buckets = []
    for min_score in sorted(bucket_data.keys()):
        pnls = bucket_data[min_score]
        wins = sum(1 for p in pnls if p > 0)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        buckets.append({
            "range": f"{min_score}-{min_score + 9}",
            "min_score": min_score,
            "count": len(pnls),
            "win_rate": wins / len(pnls),
            "avg_pnl": sum(pnls) / len(pnls),
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0),
        })

    scores_list = [p[0] for p in pairs]
    pnls_list = [p[1] for p in pairs]
    correlation = _pearson(scores_list, pnls_list)

    now = datetime.now(UTC)
    period_days = 14
    n_periods = 8
    by_week = []
    for i in range(n_periods - 1, -1, -1):
        period_end = now - timedelta(days=i * period_days)
        period_start = period_end - timedelta(days=period_days)
        period_pairs = []
        for s, p, dt in pairs:
            if dt is None:
                continue
            dt_utc = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
            if period_start <= dt_utc < period_end:
                period_pairs.append((s, p))
        if not period_pairs:
            continue
        wins = sum(1 for _, p in period_pairs if p > 0)
        by_week.append({
            "period": period_start.strftime("%Y-%m-%d"),
            "avg_score": round(sum(s for s, _ in period_pairs) / len(period_pairs), 1),
            "win_rate": round(wins / len(period_pairs), 3),
            "count": len(period_pairs),
        })

    return {
        "buckets": buckets,
        "correlation": correlation,
        "total_trades": len(pairs),
        "by_week": by_week,
    }


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Correlazione di Pearson; None se < 2 campioni o varianza nulla."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False)) / n
    std_x = (sum((x - mean_x) ** 2 for x in xs) / n) ** 0.5
    std_y = (sum((y - mean_y) ** 2 for y in ys) / n) ** 0.5
    if std_x > 0 and std_y > 0:
        return round(cov / (std_x * std_y), 3)
    return None


def _beta(dependent: Sequence[float], independent: Sequence[float]) -> float | None:
    """Beta del dependent rispetto all'independent: cov(d, i) / var(i)."""
    n = len(dependent)
    if n < 2 or n != len(independent):
        return None
    mean_d = sum(dependent) / n
    mean_i = sum(independent) / n
    cov = sum((d - mean_d) * (i - mean_i) for d, i in zip(dependent, independent, strict=False)) / n
    var_i = sum((i - mean_i) ** 2 for i in independent) / n
    if var_i <= 0:
        return None
    return round(cov / var_i, 3)


@router.get("/macro/correlation")
def macro_correlation(
    base: str = Query("XYZ100", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-/:]+$"),
    ref: str = Query("BTC", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-/:]+$"),
    timeframe: str = Query("1h", pattern=r"^(5m|15m|30m|1h|4h|1d)$"),
    lookback: int = Query(200, ge=2, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """Correlazione/beta rolling tra due market, calcolate sui rendimenti.

    `base` è l'indipendente (es. XYZ100/Nasdaq-100), `ref` il dipendente
    (es. BTC). La correlazione è su rendimenti percentuali (non sui prezzi
    assoluti, che per serie in trend sono spuriamente correlati); `beta` è
    quanto si muove `ref` per unità di movimento di `base`. Fail-soft:
    `correlation`/`beta` = null se i campioni comuni sono insufficienti.
    """
    base_market = db.query(Market).filter(Market.symbol == base).first()
    ref_market = db.query(Market).filter(Market.symbol == ref).first()
    if base_market is None or ref_market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    def _closes(market: Market) -> dict[datetime, float]:
        rows = (
            db.query(Candle.open_time, Candle.close)
            .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(lookback)
            .all()
        )
        return {row.open_time: float(row.close) for row in rows}

    base_closes = _closes(base_market)
    ref_closes = _closes(ref_market)

    # Inner join sui timestamp comuni: XYZ100 e BTC devono avere la barra allo
    # stesso open_time per essere confrontati.
    common = sorted(set(base_closes) & set(ref_closes))
    base_series = [base_closes[t] for t in common]
    ref_series = [ref_closes[t] for t in common]

    base_returns: list[float] = []
    ref_returns: list[float] = []
    for i in range(1, len(common)):
        b_prev, r_prev = base_series[i - 1], ref_series[i - 1]
        if b_prev <= 0 or r_prev <= 0:
            continue
        base_returns.append(base_series[i] / b_prev - 1.0)
        ref_returns.append(ref_series[i] / r_prev - 1.0)

    return {
        "base": base,
        "ref": ref,
        "timeframe": timeframe,
        "lookback": lookback,
        "samples": len(base_returns),
        "correlation": _pearson(ref_returns, base_returns),
        "beta": _beta(ref_returns, base_returns),
        "base_last_close": base_series[-1] if base_series else None,
        "ref_last_close": ref_series[-1] if ref_series else None,
    }


@router.post("/analytics/report/send")
async def send_diagnostic_report(
    lookback: int = Query(200, ge=10, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    from app.notifications.diagnostic_report import DiagnosticReporter
    from app.notifications.telegram import TelegramNotifier
    settings = get_settings()
    notifier = TelegramNotifier(settings)
    return await DiagnosticReporter().send(db, notifier, lookback=lookback)


@router.get("/analytics/signal-rejections")
def signal_rejections(db: Session = Depends(get_db), limit: int = Query(50, ge=1, le=200)) -> dict:
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "live_order_rejected")
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    reason_counts: dict[str, int] = {}
    recent: list[dict] = []
    for row in rows:
        reasons = (row.data or {}).get("reasons", [])
        if isinstance(reasons, list):
            for r in reasons:
                reason_counts[r] = reason_counts.get(r, 0) + 1
        recent.append({
            "id": row.id,
            "timestamp": row.timestamp,
            "reasons": reasons,
        })
    return {"total": len(rows), "by_reason": reason_counts, "recent": recent[:10]}


@router.get("/analytics/signal-candidate-rejections")
def signal_candidate_rejections(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "signal_candidate_rejected")
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    reason_counts: dict[str, int] = {}
    recent: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row.data) if isinstance(row.data, dict) else {}
        reason = str(data.get("reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        recent.append({"id": row.id, "timestamp": row.timestamp, **data})
    return {"total": len(rows), "by_reason": reason_counts, "recent": recent[:10]}

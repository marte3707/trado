"""Auto-calibration (preview/run/baseline/history/stability) e auto-tuner."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.db.models import (
    AppSetting,
    DecisionLog,
    StrategyConfig,
)
from app.db.session import get_db
from app.engine.auto_tuner import AutoTuner
from app.engine.calibration import AutoCalibrationEngine, serialize_calibration
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats

_logger = structlog.get_logger()

router = APIRouter()


@router.get("/analytics/calibration")
def calibration_preview(db: Session = Depends(get_db)) -> dict:
    result = AutoCalibrationEngine().preview(db)
    payload = serialize_calibration(result)
    state = db.get(AppSetting, "auto_calibration_state")
    baseline = db.get(AppSetting, "auto_calibration_baseline")
    payload["state"] = state.value if state is not None else {}
    payload["baseline"] = baseline.value if baseline is not None else {}
    db.rollback()
    return payload


@router.post("/auto-calibration/run")
def run_auto_calibration(db: Session = Depends(get_db)) -> dict:
    result = AutoCalibrationEngine().run(db, force=True)
    return serialize_calibration(result)


@router.get("/auto-tuner/status")
def auto_tuner_status(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().status(db)


@router.get("/auto-tuner/source-tests")
def auto_tuner_source_tests(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().source_tests(db)


@router.get("/auto-tuner/strategy-tests")
def auto_tuner_strategy_tests(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().strategy_tests(db)


@router.post("/auto-tuner/source-tests/{source}/start")
def auto_tuner_start_source_test(
    source: str = Path(..., pattern="^(whale_flow|fear_greed)$"),
    db: Session = Depends(get_db),
) -> dict:
    result = AutoTuner().start_source_test(db, source)
    status = result.get("status")
    if status == "unknown_source":
        raise HTTPException(status_code=404, detail=result)
    if status == "conflict":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/auto-tuner/strategy-tests/{timeframe}/{strategy}/start")
def auto_tuner_start_strategy_test(
    timeframe: str = Path(..., pattern="^[0-9a-zA-Z]+$"),
    strategy: str = Path(..., pattern="^[a-zA-Z0-9_-]+$"),
    db: Session = Depends(get_db),
) -> dict:
    result = AutoTuner().start_strategy_test(db, timeframe, strategy)
    status = result.get("status")
    if status == "unsupported_strategy_test":
        raise HTTPException(status_code=404, detail=result)
    if status == "conflict":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/auto-tuner/strategy-tests/{timeframe}/{strategy}/queue")
def auto_tuner_queue_strategy_test(
    timeframe: str = Path(..., pattern="^[0-9a-zA-Z]+$"),
    strategy: str = Path(..., pattern="^[a-zA-Z0-9_-]+$"),
    db: Session = Depends(get_db),
) -> dict:
    result = AutoTuner().queue_strategy_test(db, timeframe, strategy)
    status = result.get("status")
    if status == "unsupported_strategy_test":
        raise HTTPException(status_code=404, detail=result)
    if status == "queue_conflict":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.delete("/auto-tuner/strategy-tests/queue")
def auto_tuner_clear_queued_strategy_test(db: Session = Depends(get_db)) -> dict:
    result = AutoTuner().clear_queued_strategy_test(db)
    if result.get("status") == "queue_empty":
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/auto-tuner/run")
def auto_tuner_run(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().run(db)


@router.post("/auto-tuner/pause")
def auto_tuner_pause(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().pause(db)


@router.post("/auto-tuner/resume")
def auto_tuner_resume(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().resume(db)


@router.post("/auto-tuner/rollback")
def auto_tuner_rollback(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().rollback_current(db)


@router.post("/auto-tuner/reset")
def auto_tuner_reset(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().reset(db)


@router.post("/auto-calibration/baseline/reset")
def reset_calibration_baseline(db: Session = Depends(get_db)) -> dict:
    deleted = (
        db.query(AppSetting)
        .filter(AppSetting.key == "auto_calibration_baseline")
        .delete()
    )
    db.add(
        DecisionLog(
            event_type="auto_calibration_baseline_reset",
            message="Auto calibration baseline reset",
            data={"deleted": int(deleted)},
        )
    )
    db.commit()
    return {"ok": True, "deleted": int(deleted)}


@router.get("/analytics/calibration/history")
def calibration_history(db: Session = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    event_types = [
        "auto_calibration_applied",
        "auto_calibration_checked",
        "auto_calibration_skipped",
        "auto_calibration_failed",
    ]
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(event_types))
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "message": row.message,
            "status": (row.data or {}).get("status"),
            "reason": (row.data or {}).get("reason"),
            "overall": (row.data or {}).get("overall"),
            "changes": (row.data or {}).get("changes", []),
            "error": (row.data or {}).get("error"),
            "force": (row.data or {}).get("force"),
        }
        for row in rows
    ]


@router.get("/analytics/calibration/stability")
def calibration_stability(db: Session = Depends(get_db)) -> dict:
    """Diagnostica walk-forward per strategia: train vs test stats + factor history.

    Mostra per ogni `(timeframe:strategy)`:
    - WR / PnL / PF su finestra di train (più vecchia) e test (più recente)
    - "concordance": train e test puntano nello stesso verso (defensive vs baseline)?
    - factor_history: storico dei factor decisi dalle ultime N calibrazioni
    - factor_stddev: deviazione standard dei factor recenti (proxy di overfitting)
    """
    import math as _math


    analyzer = SignalPerformanceAnalyzer()
    auto_cal_setting = db.get(AppSetting, "auto_calibration")
    settings = (
        auto_cal_setting.value if auto_cal_setting and isinstance(auto_cal_setting.value, dict)
        else {}
    )
    train_ratio = float(settings.get("walk_forward_train_ratio", 0.7))
    min_per_side = int(settings.get("walk_forward_min_per_side", 5))
    lookback = int(settings.get("lookback_trades", 500))

    state_setting = db.get(AppSetting, "auto_calibration_state")
    state_value = (
        state_setting.value if state_setting and isinstance(state_setting.value, dict)
        else {}
    )
    factor_history = dict(state_value.get("factor_history") or {})

    strategies = db.query(StrategyConfig).all()
    timeframes = sorted({s.timeframe for s in strategies})

    by_strategy: list[dict] = []
    for timeframe in timeframes:
        positions_by_strategy = analyzer.strategy_positions(
            db, timeframe=timeframe, limit=lookback
        )
        for strategy_config in strategies:
            if strategy_config.timeframe != timeframe:
                continue
            key = f"{timeframe}:{strategy_config.strategy_name}"
            positions = positions_by_strategy.get(strategy_config.strategy_name, [])
            history = factor_history.get(key, [])
            stddev = 0.0
            if len(history) >= 2:
                mean = sum(history) / len(history)
                stddev = _math.sqrt(sum((v - mean) ** 2 for v in history) / (len(history) - 1))
            split = SignalPerformanceAnalyzer.holdout_split_stats(
                positions, train_ratio=train_ratio, min_samples_per_side=min_per_side
            )
            entry: dict = {
                "key": key,
                "timeframe": timeframe,
                "strategy": strategy_config.strategy_name,
                "enabled": strategy_config.enabled,
                "weight": strategy_config.weight,
                "trades": len(positions),
                "factor_history": history,
                "factor_stddev": round(stddev, 4),
            }
            if split is None:
                entry["holdout"] = None
            else:
                train, test = split
                entry["holdout"] = {
                    "train": serialize_stats(train),
                    "test": serialize_stats(test),
                    "wr_delta": round(test.win_rate - train.win_rate, 4),
                }
            by_strategy.append(entry)
    return {
        "settings": {
            "walk_forward_train_ratio": train_ratio,
            "walk_forward_min_per_side": min_per_side,
            "factor_history_window": int(settings.get("factor_history_window", 5)),
            "factor_history_std_threshold": float(
                settings.get("factor_history_std_threshold", 0.25)
            ),
        },
        "by_strategy": by_strategy,
    }



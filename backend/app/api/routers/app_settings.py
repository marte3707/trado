"""Settings runtime, configurazione strategie e kill switch."""

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.db.init_db import seed_timeframe
from app.db.models import (
    AppSetting,
    StrategyConfig,
)
from app.db.session import get_db
from app.schemas import (
    LiveExecutionSettings,
    RiskControlsSettings,
    SettingsPatch,
    StrategyPatch,
    TheBullSettings,
)

_logger = structlog.get_logger()

router = APIRouter()


@router.get("/settings")
def settings(db: Session = Depends(get_db)) -> dict:
    return {row.key: row.value for row in db.query(AppSetting).all()}


_SETTINGS_VALIDATORS: dict[str, type[BaseModel]] = {
    "risk": RiskControlsSettings,
    "live_execution": LiveExecutionSettings,
    "the_bull": TheBullSettings,
}


@router.patch("/settings")
def patch_settings(payload: dict[str, SettingsPatch], db: Session = Depends(get_db)) -> dict:
    for key, patch in payload.items():
        validator = _SETTINGS_VALIDATORS.get(key)
        if validator is not None:
            try:
                validator.model_validate(patch.value)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"key": key, "errors": exc.errors(include_url=False)},
                ) from exc
        setting = db.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key, value=patch.value)
            db.add(setting)
        else:
            setting.value = patch.value

        if key == "timeframes":
            enabled = patch.value.get("enabled") if isinstance(patch.value, dict) else None
            if isinstance(enabled, list):
                existing_tfs = {tf for (tf,) in db.query(StrategyConfig.timeframe).distinct()}
                for timeframe in enabled:
                    if timeframe not in existing_tfs:
                        seed_timeframe(db, timeframe)

    db.commit()
    return {"status": "updated"}


@router.get("/strategies")
def strategies(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {
            "id": row.id,
            "strategy_name": row.strategy_name,
            "enabled": row.enabled,
            "timeframe": row.timeframe,
            "params": row.params,
            "weight": row.weight,
        }
        for row in db.query(StrategyConfig).order_by(StrategyConfig.timeframe, StrategyConfig.strategy_name).all()
    ]


@router.patch("/strategies/{strategy_id}")
def patch_strategy(strategy_id: int, payload: StrategyPatch, db: Session = Depends(get_db)) -> dict:
    strategy = db.get(StrategyConfig, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if payload.enabled is not None:
        strategy.enabled = payload.enabled
    if payload.params is not None:
        strategy.params = payload.params
    if payload.weight is not None:
        strategy.weight = payload.weight
    db.commit()
    return {"status": "updated"}


@router.post("/kill-switch/enable")
def enable_kill_switch(db: Session = Depends(get_db)) -> dict:
    setting = db.get(AppSetting, "kill_switch") or AppSetting(key="kill_switch", value={})
    setting.value = {"enabled": True}
    db.add(setting)
    db.commit()
    return {"enabled": True}


@router.post("/kill-switch/disable")
def disable_kill_switch(db: Session = Depends(get_db)) -> dict:
    setting = db.get(AppSetting, "kill_switch") or AppSetting(key="kill_switch", value={})
    setting.value = {"enabled": False}
    db.add(setting)
    db.commit()
    return {"enabled": False}


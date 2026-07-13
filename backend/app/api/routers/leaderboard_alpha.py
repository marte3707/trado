from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routers.common import _log_and_redact
from app.db.models import LeaderboardTrader
from app.db.session import get_db
from app.services.leaderboard_alpha import (
    build_dashboard,
    enable_copy_live_mode,
    import_top_leaderboard_traders,
    leaderboard_alpha_settings,
    normalize_address,
    run_copy_paper_replay,
    run_leaderboard_alpha_maintenance,
    serialize_snapshot,
    serialize_trader,
    sync_enabled_traders,
    sync_trader,
)

router = APIRouter()


class LeaderboardTraderCreate(BaseModel):
    address: str
    label: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class LeaderboardTraderPatch(BaseModel):
    label: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None
    source: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    metadata_json: dict[str, Any] | None = None


@router.get("/leaderboard-alpha")
def leaderboard_alpha_dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    return build_dashboard(db)


@router.post("/leaderboard-alpha/import-top")
async def import_top_leaderboard_alpha_traders(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = leaderboard_alpha_settings(db)
    try:
        result = await import_top_leaderboard_traders(
            db,
            limit=int(settings.get("auto_import_limit") or 20),
            window=str(settings.get("auto_import_window") or "week"),
            min_volume_usd=Decimal(str(settings.get("auto_import_min_volume_usd") or "0")),
        )
    except Exception as exc:
        error_id = _log_and_redact("leaderboard_alpha_import_top_failed", exc)
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Import leaderboard fallito; error_id={error_id}") from exc
    db.commit()
    return result


@router.post("/leaderboard-alpha/maintenance")
async def run_leaderboard_alpha_maintenance_now(db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = await run_leaderboard_alpha_maintenance(db)
    except Exception as exc:
        error_id = _log_and_redact("leaderboard_alpha_maintenance_failed", exc)
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Maintenance fallita; error_id={error_id}") from exc
    db.commit()
    return result


@router.post("/leaderboard-alpha/copy-paper/run")
def run_leaderboard_copy_paper(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = leaderboard_alpha_settings(db)
    try:
        result = run_copy_paper_replay(db, settings=settings)
    except Exception as exc:
        error_id = _log_and_redact("leaderboard_alpha_copy_paper_failed", exc)
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Copy-paper fallito; error_id={error_id}") from exc
    db.commit()
    return result


@router.post("/leaderboard-alpha/copy-live/enable")
def enable_leaderboard_copy_live(db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = enable_copy_live_mode(db)
    except Exception as exc:
        error_id = _log_and_redact("leaderboard_alpha_copy_live_enable_failed", exc)
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Attivazione copy-live fallita; error_id={error_id}") from exc
    if result.get("status") == "blocked":
        db.rollback()
        blockers = [str(item) for item in result.get("blockers", [])]
        reason = ", ".join(blockers) if blockers else "readiness non soddisfatta"
        raise HTTPException(
            status_code=409,
            detail=f"Copy-live non attivabile: {reason}",
        )
    db.commit()
    return result


@router.post("/leaderboard-alpha/traders")
def create_leaderboard_trader(
    payload: LeaderboardTraderCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        address = normalize_address(payload.address)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = db.query(LeaderboardTrader).filter(LeaderboardTrader.address == address).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Trader gia' presente nella watchlist")

    trader = LeaderboardTrader(
        address=address,
        label=payload.label.strip() if payload.label else None,
        source=payload.source.strip() if payload.source else "hyperliquid_leaderboard",
        notes=payload.notes.strip() if payload.notes else None,
    )
    db.add(trader)
    db.commit()
    db.refresh(trader)
    return serialize_trader(trader)


@router.patch("/leaderboard-alpha/traders/{trader_id}")
def update_leaderboard_trader(
    payload: LeaderboardTraderPatch,
    trader_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    trader = db.get(LeaderboardTrader, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader non trovato")
    if payload.label is not None:
        trader.label = payload.label.strip() or None
    if payload.enabled is not None:
        trader.enabled = payload.enabled
    if payload.source is not None:
        trader.source = payload.source.strip() or "hyperliquid_leaderboard"
    if payload.notes is not None:
        trader.notes = payload.notes.strip() or None
    if payload.metadata_json is not None:
        trader.metadata_json = payload.metadata_json
    db.commit()
    db.refresh(trader)
    return serialize_trader(trader)


@router.post("/leaderboard-alpha/traders/{trader_id}/sync")
async def sync_leaderboard_trader(
    trader_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    trader = db.get(LeaderboardTrader, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader non trovato")
    settings = leaderboard_alpha_settings(db)
    try:
        result = await sync_trader(
            db,
            trader,
            lookback_days=int(settings.get("lookback_days") or 30),
        )
    except Exception as exc:
        error_id = _log_and_redact("leaderboard_alpha_sync_trader_failed", exc)
        trader.last_sync_status = "error"
        trader.last_sync_error = f"error_id={error_id}"
        db.commit()
        raise HTTPException(status_code=502, detail=f"Sync Hyperliquid fallita; error_id={error_id}") from exc
    db.commit()
    db.refresh(trader)
    return {
        "status": result.status,
        "reason": result.reason,
        "trader": serialize_trader(trader, result.snapshot),
        "fetched": result.fetched,
        "inserted": result.inserted,
        "snapshot": serialize_snapshot(result.snapshot),
    }


@router.post("/leaderboard-alpha/sync")
async def sync_leaderboard_watchlist(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = leaderboard_alpha_settings(db)
    results = await sync_enabled_traders(
        db,
        limit=int(settings.get("sync_limit_per_run") or 25),
        lookback_days=int(settings.get("lookback_days") or 30),
    )
    db.commit()
    return {
        "status": "ok",
        "synced": len([row for row in results if row.status == "ok"]),
        "rate_limited": len([row for row in results if row.status == "rate_limited"]),
        "results": [
            {
                "trader_id": result.trader_id,
                "address": result.address,
                "status": result.status,
                "reason": result.reason,
                "fetched": result.fetched,
                "inserted": result.inserted,
                "snapshot": serialize_snapshot(result.snapshot),
            }
            for result in results
        ],
    }

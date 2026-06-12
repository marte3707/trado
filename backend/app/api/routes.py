"""Aggregatore dei router API per dominio.

Il vecchio monolite (3200+ righe) è stato scomposto in `app/api/routers/`:
ogni modulo possiede un `APIRouter` senza prefisso, qui aggregati sotto
`/api`. Gli helper condivisi vivono in `routers/common.py`.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import (
    account,
    analytics,
    app_settings,
    calibration,
    markets,
    ops,
    positions,
    signals,
    system,
)

router = APIRouter(prefix="/api")
for module in (
    ops,
    system,
    markets,
    signals,
    positions,
    analytics,
    calibration,
    app_settings,
    account,
):
    router.include_router(module.router)

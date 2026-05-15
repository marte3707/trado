import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import API_KEY_HEADER, APIKeyAuthMiddleware
from app.api.routes import router
from app.core.config import get_settings
from app.core.enums import OperationMode
from app.core.logging import configure_logging
from app.db.init_db import create_schema, seed_defaults
from app.db.migrations import apply_migrations
from app.db.session import SessionLocal

configure_logging()
logger = structlog.get_logger()
settings = get_settings()

if settings.operation_mode == OperationMode.AUTO_TRADE and settings.enable_live_trading:
    missing = []
    if not settings.hyperliquid_private_key:
        missing.append("HYPERLIQUID_PRIVATE_KEY")
    if not settings.hyperliquid_account_address:
        missing.append("HYPERLIQUID_ACCOUNT_ADDRESS")
    if missing:
        raise RuntimeError(
            "AUTO_TRADE with enable_live_trading=True requires: " + ", ".join(missing)
        )

if not settings.api_key:
    logger.warning(
        "api_key_not_configured",
        message="API_KEY is empty — mutating endpoints are unauthenticated. Set API_KEY in env.",
    )

app = FastAPI(title="Trado API", version="0.1.0")
app.add_middleware(APIKeyAuthMiddleware, api_key=settings.api_key)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", API_KEY_HEADER.title()],
)
app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    create_schema()
    apply_migrations()
    db = SessionLocal()
    try:
        seed_defaults(db)
    finally:
        db.close()

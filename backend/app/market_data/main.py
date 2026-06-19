from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.init_db import create_schema, seed_defaults
from app.db.migrations import apply_migrations
from app.db.session import SessionLocal
from app.services.hyperliquid_ws import HyperliquidMarketDataService


async def main() -> None:
    configure_logging()
    create_schema()
    apply_migrations()
    db = SessionLocal()
    try:
        seed_defaults(db)
    finally:
        db.close()
    await HyperliquidMarketDataService(get_settings()).run_forever()


if __name__ == "__main__":
    asyncio.run(main())

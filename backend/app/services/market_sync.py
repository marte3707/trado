from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Market
from app.services.hyperliquid import HyperliquidClient


class MarketSyncService:
    def __init__(self) -> None:
        self.client = HyperliquidClient(get_settings())

    async def sync(self, db: Session) -> int:
        meta, spot_meta = await self.client.meta(), await self.client.spot_meta()
        payloads = self.client.perp_markets(meta) + self.client.spot_markets(spot_meta)
        count = 0
        for payload in payloads:
            statement = insert(Market).values(
                exchange="hyperliquid",
                symbol=payload["symbol"],
                base=payload["base"],
                quote=payload["quote"],
                market_type=payload["market_type"],
                active=True,
                metadata_json={
                    "asset_id": payload["asset_id"],
                    "hl_coin": payload["hl_coin"],
                    "raw": payload["raw"],
                },
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_market",
                set_={
                    "base": payload["base"],
                    "quote": payload["quote"],
                    "active": True,
                    "metadata": {
                        "asset_id": payload["asset_id"],
                        "hl_coin": payload["hl_coin"],
                        "raw": payload["raw"],
                    },
                },
            )
            db.execute(statement)
            count += 1
        db.commit()
        return count


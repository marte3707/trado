from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings
from app.core.enums import MarketType


class HyperliquidError(RuntimeError):
    pass


class HyperliquidTransientError(HyperliquidError):
    pass


class HyperliquidClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _post_info(self, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            last_status: int | None = None
            last_text = ""
            for attempt in range(4):
                response = await client.post(self.settings.info_url, json=payload)
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_status = response.status_code
                    last_text = response.text[:500]
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                if response.is_error:
                    raise HyperliquidError(f"Hyperliquid info error {response.status_code}: {response.text}")
                return response.json()
        raise HyperliquidTransientError(
            f"Hyperliquid info request failed after retries status={last_status} body={last_text}"
        )

    async def meta(self) -> dict[str, Any]:
        return await self._post_info({"type": "meta"})

    async def spot_meta(self) -> dict[str, Any]:
        return await self._post_info({"type": "spotMeta"})

    async def mids(self) -> dict[str, str]:
        return await self._post_info({"type": "allMids"})

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return await self._post_info(
            {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
            }
        )

    async def user_state(self, address: str) -> dict[str, Any]:
        return await self._post_info({"type": "clearinghouseState", "user": address})

    async def spot_user_state(self, address: str) -> dict[str, Any]:
        return await self._post_info({"type": "spotClearinghouseState", "user": address})

    @staticmethod
    def perp_markets(meta: dict[str, Any]) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        for index, item in enumerate(meta.get("universe", [])):
            symbol = item["name"]
            markets.append(
                {
                    "symbol": symbol,
                    "base": symbol.split(":")[-1],
                    "quote": "USDC",
                    "market_type": MarketType.PERP.value,
                    "asset_id": index,
                    "hl_coin": symbol,
                    "raw": item,
                }
            )
        return markets

    @staticmethod
    def spot_markets(spot_meta: dict[str, Any]) -> list[dict[str, Any]]:
        tokens = {token["index"]: token for token in spot_meta.get("tokens", [])}
        markets: list[dict[str, Any]] = []
        for item in spot_meta.get("universe", []):
            base_token = tokens.get(item.get("tokens", [None, None])[0], {})
            quote_token = tokens.get(item.get("tokens", [None, None])[1], {})
            hl_coin = item.get("name")
            symbol = hl_coin if "/" in hl_coin else f"{base_token.get('name', hl_coin)}/{quote_token.get('name', 'USDC')}"
            markets.append(
                {
                    "symbol": symbol,
                    "base": base_token.get("name", symbol.split("/")[0]),
                    "quote": quote_token.get("name", "USDC"),
                    "market_type": MarketType.SPOT.value,
                    "asset_id": 10000 + int(item["index"]),
                    "hl_coin": hl_coin,
                    "raw": item,
                }
            )
        return markets


def parse_hl_candle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "open_time": datetime.fromtimestamp(int(row["t"]) / 1000, tz=timezone.utc),
        "open": Decimal(str(row["o"])),
        "high": Decimal(str(row["h"])),
        "low": Decimal(str(row["l"])),
        "close": Decimal(str(row["c"])),
        "volume": Decimal(str(row["v"])),
    }

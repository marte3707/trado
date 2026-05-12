from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.config import Settings

logger = structlog.get_logger()


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    price: float | None
    volume_24h_usd: float | None
    volume_change_24h: float | None
    percent_change_1h: float | None
    percent_change_24h: float | None
    percent_change_7d: float | None
    market_cap: float | None
    last_updated: str | None
    source: str = "coinmarketcap"


@dataclass(frozen=True)
class FearGreedIndex:
    value: int
    classification: str | None
    timestamp: str | None
    source: str = "coinmarketcap"


@dataclass(frozen=True)
class AltcoinSeasonIndex:
    altcoin_index: int
    altcoin_marketcap: float | None
    snapshot_time: str | None
    yearly_high: int | None
    yearly_high_date: str | None
    yearly_low: int | None
    yearly_low_date: str | None
    source: str = "coinmarketcap"


class CoinMarketCapClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._quote_cache: dict[str, tuple[float, MarketQuote | None]] = {}
        self._fear_greed_cache: tuple[float, FearGreedIndex | None] | None = None
        self._altcoin_season_cache: tuple[float, AltcoinSeasonIndex | None] | None = None

    def latest_quote(self, symbol: str) -> MarketQuote | None:
        if not self.settings.coinmarketcap_api_key:
            return None
        candidates = symbol_candidates(symbol)
        cache_key = ",".join(candidates)
        cached = self._quote_cache.get(cache_key)
        now = time.time()
        if cached and now - cached[0] < self.settings.external_market_data_cache_ttl_seconds:
            return cached[1]

        quote = self._fetch_latest_quote(candidates)
        self._quote_cache[cache_key] = (now, quote)
        return quote

    def latest_fear_greed(self) -> FearGreedIndex | None:
        if not self.settings.coinmarketcap_api_key:
            return None
        now = time.time()
        if (
            self._fear_greed_cache
            and now - self._fear_greed_cache[0] < self.settings.external_market_data_cache_ttl_seconds
        ):
            return self._fear_greed_cache[1]

        index = self._fetch_fear_greed()
        self._fear_greed_cache = (now, index)
        return index

    def latest_altcoin_season(self) -> AltcoinSeasonIndex | None:
        if not self.settings.coinmarketcap_api_key:
            return None
        now = time.time()
        if (
            self._altcoin_season_cache
            and now - self._altcoin_season_cache[0] < self.settings.external_market_data_cache_ttl_seconds
        ):
            return self._altcoin_season_cache[1]

        index = self._fetch_altcoin_season()
        self._altcoin_season_cache = (now, index)
        return index

    def _fetch_latest_quote(self, candidates: list[str]) -> MarketQuote | None:
        try:
            response = httpx.get(
                f"{self.settings.coinmarketcap_base_url}/v3/cryptocurrency/quotes/latest",
                headers=self._headers(),
                params={"symbol": ",".join(candidates), "convert": "USD"},
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("coinmarketcap_quote_failed", symbols=candidates, error=str(exc))
            return None

        payload = response.json()
        records = _iter_records(payload.get("data"))
        candidate_set = {item.upper() for item in candidates}
        for record in records:
            record_symbol = str(record.get("symbol", "")).upper()
            if record_symbol not in candidate_set:
                continue
            usd = _quote_usd(record)
            return MarketQuote(
                symbol=record_symbol,
                price=_as_float(usd.get("price")),
                volume_24h_usd=_as_float(usd.get("volume_24h")),
                volume_change_24h=_as_float(usd.get("volume_change_24h")),
                percent_change_1h=_as_float(usd.get("percent_change_1h")),
                percent_change_24h=_as_float(usd.get("percent_change_24h")),
                percent_change_7d=_as_float(usd.get("percent_change_7d")),
                market_cap=_as_float(usd.get("market_cap")),
                last_updated=str(usd.get("last_updated") or record.get("last_updated") or "") or None,
            )
        return None

    def _fetch_fear_greed(self) -> FearGreedIndex | None:
        try:
            response = httpx.get(
                f"{self.settings.coinmarketcap_base_url}/v3/fear-and-greed/latest",
                headers=self._headers(),
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("coinmarketcap_fear_greed_failed", error=str(exc))
            return None

        data = response.json().get("data") or {}
        value = _as_float(data.get("value"))
        if value is None:
            return None
        return FearGreedIndex(
            value=max(0, min(100, int(round(value)))),
            classification=str(data.get("value_classification") or "") or None,
            timestamp=str(data.get("timestamp") or "") or None,
        )

    def _fetch_altcoin_season(self) -> AltcoinSeasonIndex | None:
        try:
            response = httpx.get(
                f"{self.settings.coinmarketcap_base_url}/v1/altcoin-season-index/latest",
                headers=self._headers(),
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("coinmarketcap_altcoin_season_failed", error=str(exc))
            return None

        return parse_altcoin_season(response.json().get("data") or {})

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-CMC_PRO_API_KEY": self.settings.coinmarketcap_api_key or "",
        }


def symbol_candidates(symbol: str) -> list[str]:
    base = symbol.split("/")[0].split(":")[-1].strip().upper()
    candidates = [base]
    if base.startswith("K") and len(base) > 2:
        candidates.append(base[1:])
    if base.startswith("U") and len(base) > 2:
        candidates.append(base[1:])
    return list(dict.fromkeys(candidates))


def _iter_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    records: list[dict[str, Any]] = []
    for value in data.values():
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            records.append(value)
    return records


def _quote_usd(record: dict[str, Any]) -> dict[str, Any]:
    quote = record.get("quote")
    if isinstance(quote, dict):
        usd = quote.get("USD")
        if isinstance(usd, dict):
            return usd
    return {}


def parse_altcoin_season(data: dict[str, Any]) -> AltcoinSeasonIndex | None:
    value = _as_float(data.get("altcoin_index"))
    if value is None:
        return None
    yearly_high = _as_float(data.get("yearly_high"))
    yearly_low = _as_float(data.get("yearly_low"))
    return AltcoinSeasonIndex(
        altcoin_index=max(0, min(100, int(round(value)))),
        altcoin_marketcap=_as_float(data.get("altcoin_marketcap")),
        snapshot_time=str(data.get("snapshot_time") or "") or None,
        yearly_high=int(round(yearly_high)) if yearly_high is not None else None,
        yearly_high_date=str(data.get("yearly_high_date") or "") or None,
        yearly_low=int(round(yearly_low)) if yearly_low is not None else None,
        yearly_low_date=str(data.get("yearly_low_date") or "") or None,
    )


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

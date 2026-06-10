from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.core.config import Settings

logger = structlog.get_logger()

_CMC_CIRCUIT_BREAKER_STATUSES = {401, 402, 403, 429}


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


@dataclass(frozen=True)
class CoinMarketCapUsage:
    enabled: bool
    available: bool
    checked_at: str | None
    disabled_until: str | None
    disabled_reason: str | None
    plan: dict[str, Any]
    usage: dict[str, Any]
    source: str = "coinmarketcap"


@dataclass(frozen=True)
class CoinGeckoUsage:
    enabled: bool
    checked_at: str | None
    data: dict[str, Any]
    source: str = "coingecko"


@dataclass(frozen=True)
class GlobalMarketData:
    btc_price_usd: float | None
    btc_price_change_24h_pct: float | None
    total_market_cap_usd: float | None
    total_market_cap_ex_top10_usd: float | None
    total_volume_24h_usd: float | None
    volume_trend: str | None  # "up" | "down" | None
    btc_dominance_pct: float | None
    market_cap_change_24h_pct: float | None
    last_updated: str | None
    source: str = "coingecko"


_STABLECOIN_SYMBOLS: frozenset[str] = frozenset({
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "USDD", "FDUSD",
    "FRAX", "PYUSD", "GUSD", "CUSD", "USDB", "CRVUSD", "USDE", "SUSDE",
    "LUSD", "SUSD", "ALUSD", "USDX", "USDS",
})


class CoinGeckoClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._quote_cache: dict[str, tuple[float, MarketQuote | None]] = {}
        self._usage_cache: tuple[float, CoinGeckoUsage | None] | None = None
        self._global_cache: tuple[float, GlobalMarketData | None] | None = None
        self._prev_volume: float | None = None
        self._altcoin_season_cache: tuple[float, AltcoinSeasonIndex | None] | None = None

    def prefetch_quotes(self, base_symbols: list[str]) -> None:
        if (
            not self.settings.external_quote_enrichment_enabled
            or not self.settings.coingecko_enabled
            or not self.settings.coingecko_api_key
            or not base_symbols
        ):
            return
        now = time.time()
        ttl = self.settings.external_market_data_cache_ttl_seconds
        pending: dict[str, list[str]] = {}
        for base in base_symbols:
            candidates = symbol_candidates(base)
            cache_key = ",".join(candidates)
            cached = self._quote_cache.get(cache_key)
            if cached and now - cached[0] < ttl:
                continue
            pending[cache_key] = candidates

        if not pending:
            return

        all_symbols = list(dict.fromkeys(s.lower() for candidates in pending.values() for s in candidates))
        quotes = self._fetch_quotes_batch(all_symbols)
        for cache_key, candidates in pending.items():
            candidate_set = {c.lower() for c in candidates}
            hit = next((q for q in quotes if q.symbol.lower() in candidate_set), None)
            self._quote_cache[cache_key] = (now, hit)

    def latest_quote(self, symbol: str) -> MarketQuote | None:
        if (
            not self.settings.external_quote_enrichment_enabled
            or not self.settings.coingecko_enabled
            or not self.settings.coingecko_api_key
        ):
            return None
        candidates = symbol_candidates(symbol)
        cache_key = ",".join(candidates)
        now = time.time()
        cached = self._quote_cache.get(cache_key)
        if cached and now - cached[0] < self.settings.external_market_data_cache_ttl_seconds:
            return cached[1]

        results = self._fetch_quotes_batch([c.lower() for c in candidates])
        candidate_set = {c.lower() for c in candidates}
        quote = next((q for q in results if q.symbol.lower() in candidate_set), None)
        self._quote_cache[cache_key] = (now, quote)
        return quote

    def key_usage(self, force: bool = False) -> CoinGeckoUsage:
        enabled = bool(self.settings.coingecko_enabled and self.settings.coingecko_api_key)
        now = time.time()
        cached = self._usage_cache
        if not force and cached and now - cached[0] < self.settings.coinmarketcap_usage_cache_ttl_seconds:
            return cached[1] or CoinGeckoUsage(enabled=enabled, checked_at=None, data={})
        if not self.settings.coingecko_api_key:
            usage = CoinGeckoUsage(enabled=False, checked_at=None, data={})
            self._usage_cache = (now, usage)
            return usage
        usage = self._fetch_key_usage()
        self._usage_cache = (now, usage)
        return usage

    def _fetch_key_usage(self) -> CoinGeckoUsage:
        checked_at = _iso_from_epoch(time.time())
        try:
            response = httpx.get(
                f"{self.settings.coingecko_base_url}/key",
                headers=self._headers(),
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("coingecko_key_usage_failed", error=str(exc))
            return CoinGeckoUsage(
                enabled=bool(self.settings.coingecko_enabled and self.settings.coingecko_api_key),
                checked_at=checked_at,
                data={},
            )
        payload = response.json()
        return CoinGeckoUsage(
            enabled=bool(self.settings.coingecko_enabled and self.settings.coingecko_api_key),
            checked_at=checked_at,
            data=payload if isinstance(payload, dict) else {},
        )

    def global_market_data(self) -> GlobalMarketData | None:
        if not self.settings.coingecko_enabled or not self.settings.coingecko_api_key:
            return None
        now = time.time()
        cached = self._global_cache
        if cached and now - cached[0] < self.settings.external_market_data_cache_ttl_seconds:
            return cached[1]
        result = self._fetch_global_market_data()
        self._global_cache = (now, result)
        return result

    def _fetch_global_market_data(self) -> GlobalMarketData | None:
        try:
            global_resp = httpx.get(
                f"{self.settings.coingecko_base_url}/global",
                headers=self._headers(),
                timeout=10,
            )
            global_resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("coingecko_global_failed", error=str(exc))
            return None

        gdata = global_resp.json().get("data") or {}
        total_market_cap = _as_float((gdata.get("total_market_cap") or {}).get("usd"))
        total_volume = _as_float((gdata.get("total_volume") or {}).get("usd"))
        btc_dominance = _as_float((gdata.get("market_cap_percentage") or {}).get("btc"))
        market_cap_change_24h = _as_float(gdata.get("market_cap_change_percentage_24h_usd"))
        updated_at = gdata.get("updated_at")
        last_updated = _iso_from_epoch(float(updated_at)) if updated_at else None

        volume_trend: str | None = None
        if total_volume is not None and self._prev_volume is not None:
            volume_trend = "up" if total_volume > self._prev_volume else "down"
        if total_volume is not None:
            self._prev_volume = total_volume

        btc_price: float | None = None
        btc_price_change_24h: float | None = None
        top10_market_cap: float | None = None
        try:
            top_resp = httpx.get(
                f"{self.settings.coingecko_base_url}/coins/markets",
                headers=self._headers(),
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 10,
                    "page": 1,
                    "sparkline": "false",
                },
                timeout=10,
            )
            top_resp.raise_for_status()
            top_coins = top_resp.json()
            if isinstance(top_coins, list):
                btc_coin = next(
                    (c for c in top_coins if isinstance(c, dict) and str(c.get("symbol", "")).upper() == "BTC"),
                    None,
                )
                if btc_coin:
                    btc_price = _as_float(btc_coin.get("current_price"))
                    btc_price_change_24h = _as_float(btc_coin.get("price_change_percentage_24h"))
                top10_market_cap = sum(
                    _as_float(c.get("market_cap")) or 0
                    for c in top_coins
                    if isinstance(c, dict)
                )
        except httpx.HTTPError as exc:
            logger.warning("coingecko_top10_failed", error=str(exc))

        ex_top10 = (
            total_market_cap - top10_market_cap
            if total_market_cap is not None and top10_market_cap is not None
            else None
        )

        return GlobalMarketData(
            btc_price_usd=btc_price,
            btc_price_change_24h_pct=btc_price_change_24h,
            total_market_cap_usd=total_market_cap,
            total_market_cap_ex_top10_usd=ex_top10,
            total_volume_24h_usd=total_volume,
            volume_trend=volume_trend,
            btc_dominance_pct=btc_dominance,
            market_cap_change_24h_pct=market_cap_change_24h,
            last_updated=last_updated,
        )

    def altcoin_season_index(self) -> AltcoinSeasonIndex | None:
        if not self.settings.coingecko_enabled or not self.settings.coingecko_api_key:
            return None
        now = time.time()
        cached = self._altcoin_season_cache
        if cached and now - cached[0] < self.settings.external_market_data_cache_ttl_seconds:
            return cached[1]
        result = self._fetch_altcoin_season_index()
        self._altcoin_season_cache = (now, result)
        return result

    def _fetch_altcoin_season_index(self) -> AltcoinSeasonIndex | None:
        try:
            resp = httpx.get(
                f"{self.settings.coingecko_base_url}/coins/markets",
                headers=self._headers(),
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 100,
                    "page": 1,
                    "sparkline": "false",
                    "price_change_percentage": "200d",
                },
                timeout=15,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("coingecko_altcoin_season_index_failed", error=str(exc))
            return None

        coins = resp.json()
        if not isinstance(coins, list):
            return None

        btc = next(
            (c for c in coins if isinstance(c, dict) and str(c.get("symbol", "")).upper() == "BTC"),
            None,
        )
        btc_200d = _as_float(btc.get("price_change_percentage_200d_in_currency")) if btc else None
        if btc_200d is None:
            return None

        valid_coins = [
            c for c in coins
            if isinstance(c, dict)
            and str(c.get("symbol", "")).upper() not in _STABLECOIN_SYMBOLS
            and str(c.get("symbol", "")).upper() != "BTC"
            and c.get("price_change_percentage_200d_in_currency") is not None
        ]
        if not valid_coins:
            return None

        outperformed = sum(
            1 for c in valid_coins
            if (_as_float(c.get("price_change_percentage_200d_in_currency")) or 0) > btc_200d
        )
        altcoin_index = max(0, min(100, int(round((outperformed / len(valid_coins)) * 100))))
        return AltcoinSeasonIndex(
            altcoin_index=altcoin_index,
            altcoin_marketcap=None,
            snapshot_time=_iso_from_epoch(time.time()),
            yearly_high=None,
            yearly_high_date=None,
            yearly_low=None,
            yearly_low_date=None,
            source="coingecko",
        )

    def _fetch_quotes_batch(self, symbols: list[str]) -> list[MarketQuote]:
        if not symbols:
            return []
        try:
            response = httpx.get(
                f"{self.settings.coingecko_base_url}/coins/markets",
                headers=self._headers(),
                params={
                    "vs_currency": "usd",
                    "symbols": ",".join(symbols),
                    "price_change_percentage": "1h,7d",
                    "per_page": 250,
                },
                timeout=10,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("coingecko_batch_quote_failed", symbols=symbols, error=str(exc))
            return []
        return _parse_coingecko_quotes(response.json())

    def _headers(self) -> dict[str, str]:
        key = self.settings.coingecko_api_key or ""
        header_name = "x-cg-pro-api-key" if "pro-api" in self.settings.coingecko_base_url else "x-cg-demo-api-key"
        return {"Accept": "application/json", header_name: key}


_ALTERNATIVE_ME_URL = "https://api.alternative.me/fng/?limit=1"


class AlternativeMeClient:
    """Fear & Greed index from alternative.me — free, no API key required."""

    def __init__(self) -> None:
        self._cache: tuple[float, FearGreedIndex | None] | None = None
        self._cache_ttl = 300

    def latest_fear_greed(self) -> FearGreedIndex | None:
        now = time.time()
        if self._cache and now - self._cache[0] < self._cache_ttl:
            return self._cache[1]
        result = self._fetch()
        self._cache = (now, result)
        return result

    def _fetch(self) -> FearGreedIndex | None:
        try:
            resp = httpx.get(_ALTERNATIVE_ME_URL, timeout=8)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("alternative_me_fear_greed_failed", error=str(exc))
            return None
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        value = _as_float(item.get("value"))
        if value is None:
            return None
        ts = item.get("timestamp")
        timestamp = _iso_from_epoch(float(ts)) if ts else None
        return FearGreedIndex(
            value=max(0, min(100, int(round(value)))),
            classification=str(item.get("value_classification") or "") or None,
            timestamp=timestamp,
            source="alternative.me",
        )


class CoinMarketCapClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._coingecko = CoinGeckoClient(settings)
        self._alt_me = AlternativeMeClient()
        self._quote_cache: dict[str, tuple[float, MarketQuote | None]] = {}
        self._fear_greed_cache: tuple[float, FearGreedIndex | None] | None = None
        self._altcoin_season_cache: tuple[float, AltcoinSeasonIndex | None] | None = None
        self._cmc_disabled_until: float = 0.0
        self._cmc_disabled_reason: str | None = None
        self._cmc_usage_cache: tuple[float, CoinMarketCapUsage | None] | None = None

    def prefetch_quotes(self, base_symbols: list[str]) -> None:
        """Fetch all symbols in a single batch call and prime the cache."""
        if not self.settings.external_quote_enrichment_enabled:
            return
        if base_symbols:
            self._coingecko.prefetch_quotes(base_symbols)
        if not self._cmc_available() or not base_symbols:
            return
        now = time.time()
        ttl = self.settings.external_market_data_cache_ttl_seconds
        pending: dict[str, list[str]] = {}
        for base in base_symbols:
            candidates = symbol_candidates(base)
            cache_key = ",".join(candidates)
            cached = self._quote_cache.get(cache_key)
            if cached and now - cached[0] < ttl:
                continue
            pending[cache_key] = candidates

        if not pending:
            return

        all_symbols = list({s for candidates in pending.values() for s in candidates})
        quotes_by_symbol = self._fetch_quotes_batch(all_symbols)
        for cache_key, candidates in pending.items():
            candidate_set = {c.upper() for c in candidates}
            hit = next((q for q in quotes_by_symbol if q.symbol in candidate_set), None)
            self._quote_cache[cache_key] = (now, hit)

    def latest_quote(self, symbol: str) -> MarketQuote | None:
        if not self.settings.external_quote_enrichment_enabled:
            return None
        result = self._cmc_latest_quote(symbol)
        if result is not None:
            return result
        return self._coingecko.latest_quote(symbol)

    def key_usage(self, force: bool = False) -> CoinMarketCapUsage:
        has_key = bool(self.settings.coinmarketcap_api_key)
        enabled = bool(self.settings.coinmarketcap_enabled and has_key)
        now = time.time()
        cached = self._cmc_usage_cache
        if not force and cached and now - cached[0] < self.settings.coinmarketcap_usage_cache_ttl_seconds:
            return cached[1] or self._empty_usage(enabled=enabled)
        if not has_key:
            usage = self._empty_usage(enabled=False)
            self._cmc_usage_cache = (now, usage)
            return usage
        usage = self._fetch_key_usage()  # type: ignore[assignment]
        if usage is None:
            usage = self._empty_usage(enabled=enabled)
        self._cmc_usage_cache = (now, usage)
        return usage

    def coingecko_key_usage(self, force: bool = False) -> CoinGeckoUsage:
        return self._coingecko.key_usage(force=force)

    def latest_fear_greed(self) -> FearGreedIndex | None:
        if not self.settings.coinmarketcap_enabled:
            return None
        if self._cmc_available():
            now = time.time()
            if (
                self._fear_greed_cache
                and now - self._fear_greed_cache[0] < self.settings.external_market_data_cache_ttl_seconds
            ):
                return self._fear_greed_cache[1]
            index = self._fetch_fear_greed()
            self._fear_greed_cache = (now, index)
            if index is not None:
                return index
        return self._alt_me.latest_fear_greed()

    def latest_altcoin_season(self) -> AltcoinSeasonIndex | None:
        if self._cmc_available():
            now = time.time()
            if (
                self._altcoin_season_cache
                and now - self._altcoin_season_cache[0] < self.settings.external_market_data_cache_ttl_seconds
            ):
                return self._altcoin_season_cache[1]
            index = self._fetch_altcoin_season()
            self._altcoin_season_cache = (now, index)
            if index is not None:
                return index
        return self._coingecko.altcoin_season_index()

    def latest_global_market(self) -> GlobalMarketData | None:
        return self._coingecko.global_market_data()

    def _cmc_latest_quote(self, symbol: str) -> MarketQuote | None:
        if not self._cmc_available():
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

    def _fetch_quotes_batch(self, symbols: list[str]) -> list[MarketQuote]:
        if not symbols:
            return []
        chunk_size = 100
        results: list[MarketQuote] = []
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            try:
                response = httpx.get(
                    f"{self.settings.coinmarketcap_base_url}/v3/cryptocurrency/quotes/latest",
                    headers=self._headers(),
                    params={"symbol": ",".join(chunk), "convert": "USD"},
                    timeout=10,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                self._handle_cmc_status_error(exc, endpoint="quotes/latest")
                logger.warning("coinmarketcap_batch_quote_failed", symbols=chunk, error=str(exc))
                continue
            except httpx.HTTPError as exc:
                logger.warning("coinmarketcap_batch_quote_failed", symbols=chunk, error=str(exc))
                continue
            results.extend(_parse_quotes(response.json().get("data")))
        return results

    def _fetch_latest_quote(self, candidates: list[str]) -> MarketQuote | None:
        results = self._fetch_quotes_batch(candidates)
        candidate_set = {c.upper() for c in candidates}
        return next((q for q in results if q.symbol in candidate_set), None)

    def _fetch_fear_greed(self) -> FearGreedIndex | None:
        try:
            response = httpx.get(
                f"{self.settings.coinmarketcap_base_url}/v3/fear-and-greed/latest",
                headers=self._headers(),
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._handle_cmc_status_error(exc, endpoint="fear-and-greed/latest")
            logger.warning("coinmarketcap_fear_greed_failed", error=str(exc))
            return None
        except httpx.HTTPError as exc:
            logger.warning("coinmarketcap_fear_greed_failed", error=str(exc))
            return None

        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}
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
        except httpx.HTTPStatusError as exc:
            self._handle_cmc_status_error(exc, endpoint="altcoin-season-index/latest")
            logger.warning("coinmarketcap_altcoin_season_failed", error=str(exc))
            return None
        except httpx.HTTPError as exc:
            logger.warning("coinmarketcap_altcoin_season_failed", error=str(exc))
            return None

        return parse_altcoin_season(response.json().get("data") or {})

    def _cmc_available(self) -> bool:
        if not self.settings.coinmarketcap_enabled:
            return False
        if not self.settings.coinmarketcap_api_key:
            return False
        if self._cmc_disabled_until <= time.time():
            return self._cmc_usage_allows_calls()
        logger.debug(
            "coinmarketcap_skipped_temporarily_disabled",
            reason=self._cmc_disabled_reason,
            disabled_until=self._cmc_disabled_until,
        )
        return False

    def _handle_cmc_status_error(self, exc: httpx.HTTPStatusError, endpoint: str) -> None:
        status_code = exc.response.status_code
        if status_code not in _CMC_CIRCUIT_BREAKER_STATUSES:
            return
        cooldown = max(0, self.settings.coinmarketcap_quota_cooldown_seconds)
        if cooldown == 0:
            return
        self._cmc_disabled_until = max(self._cmc_disabled_until, time.time() + cooldown)
        self._cmc_disabled_reason = f"http_{status_code}"
        logger.warning(
            "coinmarketcap_temporarily_disabled",
            endpoint=endpoint,
            status_code=status_code,
            cooldown_seconds=cooldown,
        )

    def _cmc_usage_allows_calls(self) -> bool:
        now = time.time()
        cached = self._cmc_usage_cache
        if cached and now - cached[0] < self.settings.coinmarketcap_usage_cache_ttl_seconds:
            usage = cached[1]
        else:
            usage = self._fetch_key_usage()
            self._cmc_usage_cache = (now, usage)
        if usage is None:
            return self._cmc_disabled_until <= time.time()
        return usage.available

    def _fetch_key_usage(self) -> CoinMarketCapUsage | None:
        checked_at = _iso_from_epoch(time.time())
        try:
            response = httpx.get(
                f"{self.settings.coinmarketcap_base_url}/v1/key/info",
                headers=self._headers(),
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._handle_cmc_status_error(exc, endpoint="key/info")
            logger.warning("coinmarketcap_key_info_failed", error=str(exc))
            return None
        except httpx.HTTPError as exc:
            logger.warning("coinmarketcap_key_info_failed", error=str(exc))
            return None

        raw_data = response.json().get("data") or {}
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        plan_raw = data.get("plan")
        plan: dict[str, Any] = plan_raw if isinstance(plan_raw, dict) else {}
        usage_raw = data.get("usage")
        usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
        available = self._usage_has_credits(plan, usage)
        return CoinMarketCapUsage(
            enabled=bool(self.settings.coinmarketcap_enabled and self.settings.coinmarketcap_api_key),
            available=bool(self.settings.coinmarketcap_enabled and available),
            checked_at=checked_at,
            disabled_until=_iso_from_epoch(self._cmc_disabled_until) if self._cmc_disabled_until > time.time() else None,
            disabled_reason=self._cmc_disabled_reason,
            plan=plan,
            usage=usage,
        )

    def _usage_has_credits(self, plan: dict[str, Any], usage: dict[str, Any]) -> bool:
        min_left = max(0, self.settings.coinmarketcap_min_credits_left)
        month_left = _nested_float(usage, "current_month", "credits_left")
        day_left = _nested_float(usage, "current_day", "credits_left")
        if month_left is not None and month_left < min_left:
            reset_at = _parse_timestamp(plan.get("credit_limit_monthly_reset_timestamp"))
            self._disable_cmc_until(reset_at, reason="monthly_credits_exhausted")
            return False
        if day_left is not None and day_left < min_left:
            self._disable_cmc_for_cooldown(reason="daily_credits_exhausted")
            return False
        return True

    def _disable_cmc_for_cooldown(self, reason: str) -> None:
        cooldown = max(0, self.settings.coinmarketcap_quota_cooldown_seconds)
        if cooldown == 0:
            return
        self._disable_cmc_until(time.time() + cooldown, reason=reason)

    def _disable_cmc_until(self, disabled_until: float | None, reason: str) -> None:
        if disabled_until is None:
            disabled_until = time.time() + max(0, self.settings.coinmarketcap_quota_cooldown_seconds)
        self._cmc_disabled_until = max(self._cmc_disabled_until, disabled_until)
        self._cmc_disabled_reason = reason
        logger.warning(
            "coinmarketcap_temporarily_disabled",
            reason=reason,
            disabled_until=_iso_from_epoch(self._cmc_disabled_until),
        )

    def _empty_usage(self, enabled: bool) -> CoinMarketCapUsage:
        return CoinMarketCapUsage(
            enabled=enabled,
            available=enabled and self._cmc_disabled_until <= time.time(),
            checked_at=None,
            disabled_until=_iso_from_epoch(self._cmc_disabled_until) if self._cmc_disabled_until > time.time() else None,
            disabled_reason=self._cmc_disabled_reason,
            plan={},
            usage={},
        )

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


def _parse_quotes(data: Any) -> list[MarketQuote]:
    records = _iter_records(data)
    quotes: list[MarketQuote] = []
    for record in records:
        symbol = str(record.get("symbol", "")).upper()
        if not symbol:
            continue
        usd = _quote_usd(record)
        quotes.append(MarketQuote(
            symbol=symbol,
            price=_as_float(usd.get("price")),
            volume_24h_usd=_as_float(usd.get("volume_24h")),
            volume_change_24h=_as_float(usd.get("volume_change_24h")),
            percent_change_1h=_as_float(usd.get("percent_change_1h")),
            percent_change_24h=_as_float(usd.get("percent_change_24h")),
            percent_change_7d=_as_float(usd.get("percent_change_7d")),
            market_cap=_as_float(usd.get("market_cap")),
            last_updated=str(usd.get("last_updated") or record.get("last_updated") or "") or None,
        ))
    return quotes


def _parse_coingecko_quotes(data: Any) -> list[MarketQuote]:
    if not isinstance(data, list):
        return []
    quotes: list[MarketQuote] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        quotes.append(MarketQuote(
            symbol=symbol,
            price=_as_float(item.get("current_price")),
            volume_24h_usd=_as_float(item.get("total_volume")),
            volume_change_24h=None,
            percent_change_1h=_as_float(item.get("price_change_percentage_1h_in_currency")),
            percent_change_24h=_as_float(item.get("price_change_percentage_24h")),
            percent_change_7d=_as_float(item.get("price_change_percentage_7d_in_currency")),
            market_cap=_as_float(item.get("market_cap")),
            last_updated=str(item.get("last_updated") or "") or None,
            source="coingecko",
        ))
    return quotes


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


def _nested_float(data: dict[str, Any], *path: str) -> float | None:
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _as_float(value)


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")

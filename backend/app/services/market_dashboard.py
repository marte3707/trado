from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog

from app.core.config import Settings

logger = structlog.get_logger()

BITCOIN_NEWS_URL = "https://coinmarketcap.com/currencies/bitcoin/#News"
DASHBOARD_MARKET_CACHE_SECONDS = 15 * 60
_ALTERNATIVE_ME_URL = "https://api.alternative.me/fng/?limit=1"
_STABLECOIN_SYMBOLS = {
    "USDT",
    "USDC",
    "BUSD",
    "DAI",
    "TUSD",
    "USDP",
    "USDD",
    "FDUSD",
    "FRAX",
    "PYUSD",
    "GUSD",
    "CUSD",
    "USDB",
    "CRVUSD",
    "USDE",
    "SUSDE",
    "LUSD",
    "SUSD",
    "ALUSD",
    "USDX",
    "USDS",
}


@dataclass(frozen=True)
class DashboardNewsItem:
    title: str
    url: str
    source: str | None
    published_at: str | None
    excerpt: str | None


class MarketDashboardService:
    """Fresh dashboard-only market snapshot.

    This intentionally uses a hard 15 minute cache instead of the longer quote
    enrichment cache used by the trading engine.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: tuple[float, dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        if self._cache and now - self._cache[0] < DASHBOARD_MARKET_CACHE_SECONDS:
            return self._with_cache_metadata(self._cache[1], now=now, hit=True)

        generated_at = _iso_from_epoch(now)
        payload: dict[str, Any] = {
            "generated_at": generated_at,
            "source_url": BITCOIN_NEWS_URL,
            "metrics": None,
            "fear_greed": None,
            "altcoin_season": None,
            "news": [],
            "errors": [],
        }
        errors: list[str] = payload["errors"]

        try:
            payload.update(self._fetch_market_metrics())
        except httpx.HTTPError as exc:
            logger.warning("dashboard_market_metrics_failed", error=str(exc))
            errors.append("market_metrics_unavailable")
        except (TypeError, ValueError) as exc:
            logger.warning("dashboard_market_metrics_parse_failed", error=str(exc))
            errors.append("market_metrics_parse_failed")

        try:
            payload["fear_greed"] = self._fetch_fear_greed()
        except httpx.HTTPError as exc:
            logger.warning("dashboard_fear_greed_failed", error=str(exc))
            errors.append("fear_greed_unavailable")
        except (TypeError, ValueError) as exc:
            logger.warning("dashboard_fear_greed_parse_failed", error=str(exc))
            errors.append("fear_greed_parse_failed")

        try:
            payload["news"] = [asdict(item) for item in self._fetch_bitcoin_news()]
        except httpx.HTTPError as exc:
            logger.warning("dashboard_bitcoin_news_failed", error=str(exc))
            errors.append("bitcoin_news_unavailable")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("dashboard_bitcoin_news_parse_failed", error=str(exc))
            errors.append("bitcoin_news_parse_failed")

        self._cache = (now, payload)
        return self._with_cache_metadata(payload, now=time.time(), hit=False)

    def _fetch_market_metrics(self) -> dict[str, Any]:
        headers = self._coingecko_headers()
        global_resp = httpx.get(
            f"{self.settings.coingecko_base_url}/global",
            headers=headers,
            timeout=10,
        )
        global_resp.raise_for_status()
        global_data = (global_resp.json().get("data") or {})

        coins_resp = httpx.get(
            f"{self.settings.coingecko_base_url}/coins/markets",
            headers=headers,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 100,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d,200d",
            },
            timeout=15,
        )
        coins_resp.raise_for_status()
        coins = coins_resp.json()
        if not isinstance(coins, list):
            coins = []

        btc = next(
            (
                coin
                for coin in coins
                if isinstance(coin, dict) and str(coin.get("symbol", "")).upper() == "BTC"
            ),
            None,
        )
        top10_market_cap = sum(
            _as_float(coin.get("market_cap")) or 0
            for coin in coins[:10]
            if isinstance(coin, dict)
        )
        total_market_cap = _as_float((global_data.get("total_market_cap") or {}).get("usd"))
        total_volume = _as_float((global_data.get("total_volume") or {}).get("usd"))
        updated_at = _as_float(global_data.get("updated_at"))

        movers = [
            _coin_mover(coin)
            for coin in coins
            if isinstance(coin, dict)
            and str(coin.get("symbol", "")).upper() not in _STABLECOIN_SYMBOLS
            and _as_float(coin.get("price_change_percentage_24h")) is not None
        ]
        movers = [m for m in movers if m is not None]

        return {
            "metrics": {
                "btc_price_usd": _as_float(btc.get("current_price")) if btc else None,
                "btc_price_change_1h_pct": _as_float(
                    btc.get("price_change_percentage_1h_in_currency")
                )
                if btc
                else None,
                "btc_price_change_24h_pct": _as_float(btc.get("price_change_percentage_24h"))
                if btc
                else None,
                "btc_price_change_7d_pct": _as_float(
                    btc.get("price_change_percentage_7d_in_currency")
                )
                if btc
                else None,
                "total_market_cap_usd": total_market_cap,
                "total_market_cap_ex_top10_usd": total_market_cap - top10_market_cap
                if total_market_cap is not None and top10_market_cap
                else None,
                "total_volume_24h_usd": total_volume,
                "btc_dominance_pct": _as_float(
                    (global_data.get("market_cap_percentage") or {}).get("btc")
                ),
                "market_cap_change_24h_pct": _as_float(
                    global_data.get("market_cap_change_percentage_24h_usd")
                ),
                "last_updated": _iso_from_epoch(updated_at) if updated_at else _iso_from_epoch(time.time()),
                "source": "coingecko",
                "top_gainers_24h": sorted(
                    movers,
                    key=lambda m: m["price_change_24h_pct"],
                    reverse=True,
                )[:3],
                "top_losers_24h": sorted(movers, key=lambda m: m["price_change_24h_pct"])[:3],
            },
            "altcoin_season": self._compute_altcoin_season(coins),
        }

    def _fetch_fear_greed(self) -> dict[str, Any] | None:
        response = httpx.get(_ALTERNATIVE_ME_URL, timeout=8)
        response.raise_for_status()
        data = response.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        value = _as_float(item.get("value"))
        timestamp = _as_float(item.get("timestamp"))
        if value is None:
            return None
        return {
            "value": max(0, min(100, int(round(value)))),
            "classification": str(item.get("value_classification") or "") or None,
            "timestamp": _iso_from_epoch(timestamp) if timestamp else None,
            "source": "alternative.me",
        }

    def _fetch_bitcoin_news(self) -> list[DashboardNewsItem]:
        response = httpx.get(
            BITCOIN_NEWS_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
                ),
            },
            follow_redirects=True,
            timeout=12,
        )
        response.raise_for_status()
        return parse_coinmarketcap_bitcoin_news(response.text)

    def _compute_altcoin_season(self, coins: list[Any]) -> dict[str, Any] | None:
        btc = next(
            (
                coin
                for coin in coins
                if isinstance(coin, dict) and str(coin.get("symbol", "")).upper() == "BTC"
            ),
            None,
        )
        btc_200d = _as_float(btc.get("price_change_percentage_200d_in_currency")) if btc else None
        if btc_200d is None:
            return None
        valid = [
            coin
            for coin in coins
            if isinstance(coin, dict)
            and str(coin.get("symbol", "")).upper() not in _STABLECOIN_SYMBOLS
            and str(coin.get("symbol", "")).upper() != "BTC"
            and _as_float(coin.get("price_change_percentage_200d_in_currency")) is not None
        ]
        if not valid:
            return None
        outperformed = sum(
            1
            for coin in valid
            if (_as_float(coin.get("price_change_percentage_200d_in_currency")) or 0) > btc_200d
        )
        return {
            "altcoin_index": max(0, min(100, int(round((outperformed / len(valid)) * 100)))),
            "snapshot_time": _iso_from_epoch(time.time()),
            "source": "coingecko",
        }

    def _coingecko_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.coingecko_api_key:
            key_name = (
                "x-cg-pro-api-key"
                if "pro-api" in self.settings.coingecko_base_url
                else "x-cg-demo-api-key"
            )
            headers[key_name] = self.settings.coingecko_api_key
        return headers

    def _with_cache_metadata(self, payload: dict[str, Any], *, now: float, hit: bool) -> dict[str, Any]:
        cached_at = self._cache[0] if self._cache else now
        result = dict(payload)
        result["cache"] = {
            "hit": hit,
            "age_seconds": max(0, int(now - cached_at)),
            "max_age_seconds": DASHBOARD_MARKET_CACHE_SECONDS,
            "expires_at": _iso_from_epoch(cached_at + DASHBOARD_MARKET_CACHE_SECONDS),
        }
        return result


def parse_coinmarketcap_bitcoin_news(raw_html: str, limit: int = 6) -> list[DashboardNewsItem]:
    candidates: list[DashboardNewsItem] = []
    for payload in _extract_json_payloads(raw_html):
        _collect_news_items(payload, candidates)
    candidates.extend(_extract_anchor_news(raw_html))
    return _dedupe_news(candidates)[:limit]


def _extract_json_payloads(raw_html: str) -> list[Any]:
    payloads: list[Any] = []
    next_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        raw_html,
        flags=re.I | re.S,
    )
    if next_match:
        payloads.append(json.loads(html.unescape(next_match.group(1))))

    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        raw_html,
        flags=re.I | re.S,
    ):
        try:
            payloads.append(json.loads(html.unescape(match.group(1))))
        except json.JSONDecodeError:
            continue
    return payloads


def _collect_news_items(value: Any, out: list[DashboardNewsItem]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_news_items(item, out)
        return
    if not isinstance(value, dict):
        return

    item = _news_from_mapping(value)
    if item:
        out.append(item)

    for child in value.values():
        if isinstance(child, (dict, list)):
            _collect_news_items(child, out)


def _news_from_mapping(data: dict[str, Any]) -> DashboardNewsItem | None:
    title = _first_string(data, "title", "headline", "name")
    url = _first_string(data, "url", "link", "canonicalUrl", "sourceUrl")
    published_at = _first_string(data, "publishedAt", "datePublished", "createdAt", "updatedAt")
    excerpt = _first_string(data, "description", "subtitle", "excerpt", "summary")
    source = _first_string(data, "sourceName", "siteName", "providerName")
    if not source:
        source_value = data.get("source") or data.get("publisher") or data.get("provider")
        if isinstance(source_value, dict):
            source = _first_string(source_value, "name", "title")
    return _build_news_item(title, url, source, published_at, excerpt)


def _extract_anchor_news(raw_html: str) -> list[DashboardNewsItem]:
    related = _news_sections(raw_html, ("related articles",))
    items = _extract_anchor_news_from_sections(related)
    if items:
        return items
    fallback = _news_sections(raw_html, ("bitcoin news", "latest")) + [raw_html]
    return _extract_anchor_news_from_sections(fallback)


def _extract_anchor_news_from_sections(sections: list[str]) -> list[DashboardNewsItem]:
    items: list[DashboardNewsItem] = []
    for section in sections:
        for match in re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            section,
            flags=re.I | re.S,
        ):
            href = html.unescape(match.group(1))
            if not _looks_like_news_url(href):
                continue
            title = _clean_text(match.group(2))
            item = _build_news_item(title, href, "CoinMarketCap", None, None)
            if item:
                items.append(item)
    return items


def _news_sections(raw_html: str, markers: tuple[str, ...]) -> list[str]:
    sections: list[str] = []
    lowered = raw_html.lower()
    for marker in markers:
        index = lowered.find(marker)
        if index >= 0:
            sections.append(raw_html[index : index + 30_000])
    return sections


def _build_news_item(
    title: str | None,
    url: str | None,
    source: str | None,
    published_at: str | None,
    excerpt: str | None,
) -> DashboardNewsItem | None:
    if not title or not url:
        return None
    clean_title = _clean_text(title)
    if len(clean_title) < 24 or len(clean_title) > 220:
        return None
    absolute_url = urljoin("https://coinmarketcap.com", html.unescape(url))
    if not absolute_url.startswith("http"):
        return None
    if not (_looks_like_news_url(absolute_url) or published_at):
        return None
    return DashboardNewsItem(
        title=clean_title,
        url=absolute_url,
        source=_clean_text(source) if source else None,
        published_at=published_at,
        excerpt=_clean_text(excerpt)[:220] if excerpt else None,
    )


def _looks_like_news_url(value: str) -> bool:
    lowered = value.lower()
    return any(part in lowered for part in ("/news/", "/headlines/", "/community/articles", "/alexandria/"))


def _dedupe_news(items: list[DashboardNewsItem]) -> list[DashboardNewsItem]:
    seen: set[str] = set()
    result: list[DashboardNewsItem] = []
    for item in items:
        key = re.sub(r"\W+", "", item.title.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(value))).strip()


def _coin_mover(coin: dict[str, Any]) -> dict[str, Any] | None:
    change = _as_float(coin.get("price_change_percentage_24h"))
    symbol = str(coin.get("symbol", "")).upper()
    if change is None or not symbol:
        return None
    return {
        "symbol": symbol,
        "name": str(coin.get("name") or symbol),
        "price_usd": _as_float(coin.get("current_price")),
        "price_change_24h_pct": change,
    }


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")

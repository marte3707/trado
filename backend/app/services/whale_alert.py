from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.config import Settings
from app.core.enums import Direction
from app.services.coinmarketcap import symbol_candidates

logger = structlog.get_logger()


@dataclass(frozen=True)
class WhaleFlow:
    symbol: str
    direction: Direction
    confidence: int
    net_flow_usd: float
    bullish_flow_usd: float
    bearish_flow_usd: float
    neutral_flow_usd: float
    transaction_count: int
    lookback_minutes: int
    min_value_usd: int
    source: str = "whale_alert"


@dataclass(frozen=True)
class PublicWhaleAlert:
    timestamp: int
    symbol: str
    amount: float | None
    amount_usd: float
    text: str
    url: str | None


class WhaleAlertClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[float, WhaleFlow | None]] = {}
        self._public_alert_cache: tuple[float, list[PublicWhaleAlert]] | None = None

    def recent_flow(self, symbol: str) -> WhaleFlow | None:
        candidates = [item.lower() for item in symbol_candidates(symbol)]
        cache_key = ",".join(candidates)
        cached = self._cache.get(cache_key)
        now = time.time()
        if cached and now - cached[0] < self.settings.whale_alert_public_cache_ttl_seconds:
            return cached[1]

        alerts = self._public_alerts()
        flow = summarize_public_alerts(
            symbols={candidate.upper() for candidate in candidates},
            alerts=alerts,
            min_value_usd=self.settings.whale_alert_min_value_usd,
        )
        if flow is None and self.settings.whale_alert_api_key:
            for candidate in candidates:
                flow = self._fetch_recent_flow(candidate)
                if flow is not None:
                    break
        self._cache[cache_key] = (now, flow)
        return flow

    def _public_alerts(self) -> list[PublicWhaleAlert]:
        now = time.time()
        if (
            self._public_alert_cache
            and now - self._public_alert_cache[0] < self.settings.whale_alert_public_cache_ttl_seconds
        ):
            return self._public_alert_cache[1]

        try:
            response = httpx.get(
                self.settings.whale_alert_public_url,
                params={"alerts": self.settings.whale_alert_public_alerts},
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("whale_alert_public_feed_failed", error=str(exc))
            self._public_alert_cache = (now, [])
            return []

        raw_alerts = response.json().get("alerts") or []
        alerts = [
            alert
            for item in raw_alerts
            if isinstance(item, str)
            for alert in [parse_public_alert(item)]
            if alert is not None
        ]
        self._public_alert_cache = (now, alerts)
        return alerts

    def _fetch_recent_flow(self, symbol: str) -> WhaleFlow | None:
        start = int(time.time() - (self.settings.whale_alert_lookback_minutes * 60))
        try:
            response = httpx.get(
                f"{self.settings.whale_alert_base_url}/transactions",
                params={
                    "api_key": self.settings.whale_alert_api_key,
                    "currency": symbol,
                    "min_value": self.settings.whale_alert_min_value_usd,
                    "start": start,
                    "limit": 100,
                },
                timeout=8,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("whale_alert_flow_failed", symbol=symbol, error=str(exc))
            return None

        transactions = response.json().get("transactions") or []
        if not isinstance(transactions, list):
            return None
        return summarize_transactions(
            symbol=symbol.upper(),
            transactions=transactions,
            lookback_minutes=self.settings.whale_alert_lookback_minutes,
            min_value_usd=self.settings.whale_alert_min_value_usd,
        )


def summarize_transactions(
    *,
    symbol: str,
    transactions: list[Any],
    lookback_minutes: int,
    min_value_usd: int,
) -> WhaleFlow | None:
    bullish = 0.0
    bearish = 0.0
    neutral = 0.0
    count = 0
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue
        amount_usd = _as_float(transaction.get("amount_usd"))
        if amount_usd is None or amount_usd <= 0:
            continue
        count += 1
        pressure = _transaction_pressure(transaction)
        if pressure > 0:
            bullish += amount_usd
        elif pressure < 0:
            bearish += amount_usd
        else:
            neutral += amount_usd

    total_directional = bullish + bearish
    if count == 0 or total_directional <= 0:
        return None

    net = bullish - bearish
    dominance = abs(net) / total_directional
    confidence = min(90, max(20, round(25 + dominance * 65)))
    if dominance < 0.15:
        direction = Direction.NEUTRAL
        confidence = 0
    elif net > 0:
        direction = Direction.LONG
    else:
        direction = Direction.SHORT

    return WhaleFlow(
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        net_flow_usd=net,
        bullish_flow_usd=bullish,
        bearish_flow_usd=bearish,
        neutral_flow_usd=neutral,
        transaction_count=count,
        lookback_minutes=lookback_minutes,
        min_value_usd=min_value_usd,
    )


def summarize_public_alerts(
    *,
    symbols: set[str],
    alerts: list[PublicWhaleAlert],
    min_value_usd: int,
) -> WhaleFlow | None:
    bullish = 0.0
    bearish = 0.0
    neutral = 0.0
    count = 0
    matched_symbol = sorted(symbols)[0] if symbols else ""
    for alert in alerts:
        if alert.symbol.upper() not in symbols or alert.amount_usd < min_value_usd:
            continue
        matched_symbol = alert.symbol.upper()
        count += 1
        pressure = _public_alert_pressure(alert.text)
        if pressure > 0:
            bullish += alert.amount_usd
        elif pressure < 0:
            bearish += alert.amount_usd
        else:
            neutral += alert.amount_usd

    total_directional = bullish + bearish
    if count == 0 or total_directional <= 0:
        return None

    net = bullish - bearish
    dominance = abs(net) / total_directional
    confidence = min(90, max(20, round(25 + dominance * 65)))
    if dominance < 0.15:
        direction = Direction.NEUTRAL
        confidence = 0
    elif net > 0:
        direction = Direction.LONG
    else:
        direction = Direction.SHORT

    return WhaleFlow(
        symbol=matched_symbol,
        direction=direction,
        confidence=confidence,
        net_flow_usd=net,
        bullish_flow_usd=bullish,
        bearish_flow_usd=bearish,
        neutral_flow_usd=neutral,
        transaction_count=count,
        lookback_minutes=0,
        min_value_usd=min_value_usd,
        source="whale_alert_public",
    )


def parse_public_alert(raw: str) -> PublicWhaleAlert | None:
    try:
        row = next(csv.reader([raw]))
    except csv.Error:
        return None
    if len(row) < 6:
        return None
    try:
        timestamp = int(row[0])
    except ValueError:
        return None
    symbol = _symbol_from_amount(row[2])
    amount_usd = _money_to_float(row[3])
    if not symbol or amount_usd is None:
        return None
    return PublicWhaleAlert(
        timestamp=timestamp,
        symbol=symbol,
        amount=_amount_to_float(row[2]),
        amount_usd=amount_usd,
        text=row[4].strip(),
        url=row[5].strip() or None,
    )


def _transaction_pressure(transaction: dict[str, Any]) -> int:
    from_type = _owner_type(transaction.get("from"))
    to_type = _owner_type(transaction.get("to"))
    if from_type == "exchange" and to_type != "exchange":
        return 1
    if from_type != "exchange" and to_type == "exchange":
        return -1
    return 0


def _public_alert_pressure(text: str) -> int:
    lower = text.lower()
    if " minted " in lower or " burned " in lower or (" at " in lower and "transferred" not in lower):
        return 0
    marker = "transferred from "
    if marker not in lower or " to " not in lower:
        return 0
    _, route = lower.split(marker, 1)
    from_part, to_part = route.split(" to ", 1)
    from_exchange = _looks_like_exchange(from_part)
    to_exchange = _looks_like_exchange(to_part)
    if from_exchange and not to_exchange:
        return 1
    if not from_exchange and to_exchange:
        return -1
    return 0


def _owner_type(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    return str(value.get("owner_type") or "").strip().lower()


def _looks_like_exchange(value: str) -> bool:
    normalized = value.lower().replace("#", "")
    exchanges = {
        "binance",
        "bitfinex",
        "bitget",
        "bithumb",
        "bitmex",
        "bitstamp",
        "bybit",
        "coinbase",
        "coinbase institutional",
        "crypto.com",
        "gate.io",
        "gemini",
        "huobi",
        "kraken",
        "kucoin",
        "okx",
        "upbit",
    }
    return any(exchange in normalized for exchange in exchanges)


def _symbol_from_amount(value: str) -> str | None:
    for part in value.replace(",", "").split():
        if part.startswith("#") and len(part) > 1:
            return part[1:].upper()
    return None


def _amount_to_float(value: str) -> float | None:
    parts = value.replace(",", "").split()
    if not parts:
        return None
    return _as_float(parts[0])


def _money_to_float(value: str) -> float | None:
    return _as_float(value.upper().replace("USD", "").replace(",", "").strip())


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

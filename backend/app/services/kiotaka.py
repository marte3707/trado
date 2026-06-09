"""Client minimale per l'API kiyotaka.ai — dati di flusso REALI su BTC.

Pensato per alimentare il solo gate macro-BTC (`btc_macro_protection`), non
l'arricchimento per-mercato: il free tier (10 weight/min, 1000 weight/giorno,
finestra storica max ~96h/richiesta) regge solo un punto di osservazione globale
su BTC interrogato ogni N minuti.

Espone `KiotakaClient.btc_flow()` che ritorna direzione di Open Interest, CVD reale
(volume aggressori BUY vs SELL) e liquidazioni long/short sulle ultime ore. Tutto
degrada in sicurezza: senza API key o in caso di errore HTTP ritorna `None`, così il
chiamante mantiene il comportamento attuale (OHLCV-only).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import structlog

from app.core.config import Settings

logger = structlog.get_logger()


@dataclass(frozen=True)
class BtcFlowSnapshot:
    """Sintesi direzionale del flusso BTC su una finestra di poche ore."""

    oi_change_pct: float        # variazione % OI close (ultimo vs primo punto)
    cvd_net: float              # somma (volume BUY - volume SELL) sulla finestra
    liq_long: float             # liquidazioni long totali (ordini SELL forzati), in BTC
    liq_short: float            # liquidazioni short totali (ordini BUY forzati), in BTC
    points: int                 # numero di ore effettive nei dati
    fetched_at: float           # monotonic time del fetch (per la cache)

    @property
    def oi_direction(self) -> int:
        return 1 if self.oi_change_pct > 0 else (-1 if self.oi_change_pct < 0 else 0)

    @property
    def cvd_direction(self) -> int:
        return 1 if self.cvd_net > 0 else (-1 if self.cvd_net < 0 else 0)


@dataclass(frozen=True)
class LiqPressure:
    """Intensità di liquidazione per un `coin`: finestra recente vs baseline orario.

    I valori sono in USD (`transform.normalize.quote=USD`). `short_*` aggrega le
    liquidazioni BUY-side (short forzati a comprare → spinta rialzista), `long_*`
    le SELL-side (long forzati a vendere → spinta ribassista).
    """

    short_recent: float            # liq short (USD) sulla finestra recente
    long_recent: float             # liq long (USD) sulla finestra recente
    short_baseline_hourly: float   # media oraria liq short sul baseline
    long_baseline_hourly: float    # media oraria liq long sul baseline
    recent_hours: int
    baseline_hours: int
    points: int                    # ore effettive nei dati
    fetched_at: float              # monotonic time del fetch (per la cache)

    @property
    def short_pressure_ratio(self) -> float:
        """Quante volte le liq short recenti (per ora) superano la media baseline."""
        if self.short_baseline_hourly <= 0:
            return 0.0
        recent_hourly = self.short_recent / max(1, self.recent_hours)
        return recent_hourly / self.short_baseline_hourly

    @property
    def long_pressure_ratio(self) -> float:
        if self.long_baseline_hourly <= 0:
            return 0.0
        recent_hourly = self.long_recent / max(1, self.recent_hours)
        return recent_hourly / self.long_baseline_hourly


class KiotakaClient:
    """Client REST con cache TTL in-process (l'istanza vive quanto il worker)."""

    def __init__(self, settings: Settings, cache_ttl_seconds: int = 600) -> None:
        self.settings = settings
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[tuple[str, str, int], BtcFlowSnapshot] = {}
        self._liq_cache: dict[tuple[str, str, int], LiqPressure] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.kiotaka_api_key)

    def btc_flow(
        self,
        lookback_hours: int = 6,
        exchange: str = "BINANCE_FUTURES",
        symbol: str = "BTCUSDT",
        ttl_seconds: int | None = None,
    ) -> BtcFlowSnapshot | None:
        """Flusso BTC sulle ultime `lookback_hours`. None se disabilitato o su errore."""
        if not self.enabled:
            return None
        ttl = self.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        cache_key = (exchange, symbol, lookback_hours)
        cached = self._cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached.fetched_at) < ttl:
            return cached

        try:
            snapshot = self._fetch(lookback_hours, exchange, symbol)
        except Exception as exc:  # noqa: BLE001 — degradazione sicura: il gate prosegue OHLCV-only
            logger.warning("kiotaka_btc_flow_failed", error=str(exc))
            return None
        if snapshot is not None:
            self._cache[cache_key] = snapshot
        return snapshot

    def _fetch(
        self, lookback_hours: int, exchange: str, symbol: str
    ) -> BtcFlowSnapshot | None:
        now = int(time.time())
        period = max(1, lookback_hours) * 3600
        from_s = now - period
        base = self.settings.kiotaka_base_url.rstrip("/")
        headers = {"X-Kiyotaka-Key": self.settings.kiotaka_api_key or ""}

        with httpx.Client(timeout=15.0) as client:
            oi = self._points(client, base, headers, "OPEN_INTEREST_AGG", exchange, symbol, from_s, period)
            liq = self._points(client, base, headers, "LIQUIDATION_AGG", exchange, symbol, from_s, period)
            trade = self._points(client, base, headers, "TRADE_AGG", exchange, symbol, from_s, period)

        oi_closes = self._field_series(oi, "close")
        oi_change_pct = 0.0
        if len(oi_closes) >= 2 and oi_closes[0] > 0:
            oi_change_pct = (oi_closes[-1] - oi_closes[0]) / oi_closes[0] * 100.0

        buy = sum(self._field_series(trade, "volume", side="BUY"))
        sell = sum(self._field_series(trade, "volume", side="SELL"))
        cvd_net = buy - sell

        liq_short = sum(self._field_series(liq, "liquidations", side="BUY"))   # short liquidati
        liq_long = sum(self._field_series(liq, "liquidations", side="SELL"))   # long liquidati

        return BtcFlowSnapshot(
            oi_change_pct=oi_change_pct,
            cvd_net=cvd_net,
            liq_long=liq_long,
            liq_short=liq_short,
            points=len(oi_closes),
            fetched_at=time.monotonic(),
        )

    def liquidation_pressure(
        self,
        coin: str,
        recent_hours: int = 1,
        baseline_hours: int = 24,
        exchange: str = "BINANCE_FUTURES",
        ttl_seconds: int | None = None,
    ) -> LiqPressure | None:
        """Pressione di liquidazione (USD) per `coin`: intensità oraria recente vs
        baseline. None se disabilitato o su errore (degradazione sicura)."""
        if not self.enabled:
            return None
        ttl = self.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        cache_key = (coin, exchange, baseline_hours)
        cached = self._liq_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached.fetched_at) < ttl:
            return cached

        try:
            pressure = self._fetch_liquidation_pressure(
                coin, recent_hours, baseline_hours, exchange
            )
        except Exception as exc:  # noqa: BLE001 — fail-open: nessuna penalità su errore
            logger.warning("kiotaka_liquidation_pressure_failed", coin=coin, error=str(exc))
            return None
        if pressure is not None:
            self._liq_cache[cache_key] = pressure
        return pressure

    def _fetch_liquidation_pressure(
        self, coin: str, recent_hours: int, baseline_hours: int, exchange: str
    ) -> LiqPressure | None:
        now = int(time.time())
        period = max(1, baseline_hours) * 3600
        from_s = now - period
        base = self.settings.kiotaka_base_url.rstrip("/")
        headers = {"X-Kiyotaka-Key": self.settings.kiotaka_api_key or ""}
        params: dict[str, str | int] = {
            "type": "LIQUIDATION_AGG", "exchange": exchange, "coin": coin,
            "interval": "HOUR", "from": from_s, "period": period,
            "transform.normalize.quote": "USD",
        }
        with httpx.Client(timeout=15.0) as client:
            series = self._request_points(client, base, headers, params)

        short_series = self._field_series(series, "liquidations", side="BUY")   # short liquidati
        long_series = self._field_series(series, "liquidations", side="SELL")   # long liquidati
        points = max(len(short_series), len(long_series))
        if points == 0:
            return None

        recent_n = max(1, recent_hours)
        return LiqPressure(
            short_recent=sum(short_series[-recent_n:]),
            long_recent=sum(long_series[-recent_n:]),
            short_baseline_hourly=(sum(short_series) / len(short_series)) if short_series else 0.0,
            long_baseline_hourly=(sum(long_series) / len(long_series)) if long_series else 0.0,
            recent_hours=recent_n,
            baseline_hours=baseline_hours,
            points=points,
            fetched_at=time.monotonic(),
        )

    @staticmethod
    def _request_points(
        client: httpx.Client, base: str, headers: dict[str, str],
        params: dict[str, str | int],
    ) -> list[dict]:
        resp = client.get(f"{base}/points", params=params, headers=headers)
        if resp.status_code != 200:
            point_type = params.get("type", "POINTS")
            raise RuntimeError(f"{point_type}: HTTP {resp.status_code} {resp.text[:120]}")
        return resp.json().get("series", [])

    @staticmethod
    def _points(
        client: httpx.Client, base: str, headers: dict[str, str], point_type: str,
        exchange: str, symbol: str, from_s: int, period_s: int,
    ) -> list[dict]:
        params: dict[str, str | int] = {
            "type": point_type, "exchange": exchange, "rawSymbol": symbol,
            "interval": "HOUR", "from": from_s, "period": period_s,
        }
        return KiotakaClient._request_points(client, base, headers, params)

    @staticmethod
    def _field_series(series: list[dict], field: str, side: str | None = None) -> list[float]:
        for s in series:
            if side is not None and s.get("id", {}).get("side") != side:
                continue
            return [
                float(p.get("Point", p).get(field, 0.0))
                for p in s.get("points", [])
            ]
        return []

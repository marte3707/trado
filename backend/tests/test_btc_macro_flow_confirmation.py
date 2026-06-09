"""Test della conferma di flusso reale (kiyotaka) nel gate macro-BTC."""

from __future__ import annotations

from app.core.config import Settings
from app.engine.btc_macro_protection import BtcMacroProtectionEngine, BtcTrendState
from app.services.kiotaka import BtcFlowSnapshot, KiotakaClient


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://x:x@localhost/x",
        hyperliquid_network="testnet",
    )


def _snapshot(cvd_net: float, points: int = 6) -> BtcFlowSnapshot:
    return BtcFlowSnapshot(
        oi_change_pct=1.0, cvd_net=cvd_net, liq_long=0.0, liq_short=0.0,
        points=points, fetched_at=0.0,
    )


def _engine_with_flow(snapshot: BtcFlowSnapshot | None) -> BtcMacroProtectionEngine:
    engine = BtcMacroProtectionEngine(_settings())
    engine._kiotaka.btc_flow = lambda **_: snapshot  # type: ignore[assignment]
    return engine


CONFIG = {"require_flow_confirmation": True}


def test_flow_confirms_bullish_when_cvd_positive() -> None:
    engine = _engine_with_flow(_snapshot(cvd_net=1500.0))
    confirmed, info = engine._check_flow_confluence(BtcTrendState.BULLISH, CONFIG)
    assert confirmed is True
    assert info["confirmed"] is True
    assert info["cvd_direction"] == 1


def test_flow_blocks_bullish_when_cvd_negative() -> None:
    engine = _engine_with_flow(_snapshot(cvd_net=-1500.0))
    confirmed, info = engine._check_flow_confluence(BtcTrendState.BULLISH, CONFIG)
    assert confirmed is False
    assert info["confirmed"] is False


def test_flow_blocks_bearish_when_cvd_positive() -> None:
    engine = _engine_with_flow(_snapshot(cvd_net=900.0))
    confirmed, _ = engine._check_flow_confluence(BtcTrendState.BEARISH, CONFIG)
    assert confirmed is False


def test_flow_confirms_bearish_when_cvd_negative() -> None:
    engine = _engine_with_flow(_snapshot(cvd_net=-900.0))
    confirmed, _ = engine._check_flow_confluence(BtcTrendState.BEARISH, CONFIG)
    assert confirmed is True


def test_fail_open_when_snapshot_missing() -> None:
    # Dato assente (free tier giù / no key): il reversal NON viene bloccato.
    engine = _engine_with_flow(None)
    confirmed, info = engine._check_flow_confluence(BtcTrendState.BULLISH, CONFIG)
    assert confirmed is True
    assert info == {"flow_available": False}


def test_fail_open_when_too_few_points() -> None:
    engine = _engine_with_flow(_snapshot(cvd_net=-5000.0, points=1))
    confirmed, info = engine._check_flow_confluence(BtcTrendState.BULLISH, CONFIG)
    assert confirmed is True
    assert info["flow_available"] is False


# --------------------------------------------------------------------------- #
# Client: parsing & gating su API key
# --------------------------------------------------------------------------- #
def test_client_disabled_without_key() -> None:
    settings = _settings()
    settings.kiotaka_api_key = None  # override eventuale chiave da .env nell'ambiente di test
    client = KiotakaClient(settings)
    assert client.enabled is False
    assert client.btc_flow() is None


def test_field_series_filters_by_side() -> None:
    series = [
        {"id": {"side": "BUY"}, "points": [{"Point": {"volume": 10.0}}, {"Point": {"volume": 5.0}}]},
        {"id": {"side": "SELL"}, "points": [{"Point": {"volume": 3.0}}]},
    ]
    assert KiotakaClient._field_series(series, "volume", side="BUY") == [10.0, 5.0]
    assert KiotakaClient._field_series(series, "volume", side="SELL") == [3.0]
    assert KiotakaClient._field_series(series, "volume", side="MISSING") == []


def test_snapshot_directions() -> None:
    assert _snapshot(cvd_net=1.0).cvd_direction == 1
    assert _snapshot(cvd_net=-1.0).cvd_direction == -1
    assert _snapshot(cvd_net=0.0).cvd_direction == 0

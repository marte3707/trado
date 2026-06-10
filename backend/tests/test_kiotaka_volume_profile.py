from __future__ import annotations

import time
from dataclasses import replace

from app.core.config import Settings
from app.indicators.volume_profile import build_volume_profile_from_triplets
from app.services.kiotaka import KiotakaClient


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://x:x@localhost/x",
        hyperliquid_network="testnet",
        kiotaka_api_key="test-key",
    )


def test_kiotaka_volume_profile_parser_aggregates_triplets() -> None:
    profile = KiotakaClient._profile_from_series(
        [
            {
                "points": [
                    {"Point": {"profile": [100, 10, 5, 101, 2, 3]}},
                    {"Point": {"profile": [100, 4, 1, 102, 1, 1]}},
                ]
            }
        ]
    )

    assert profile is not None
    assert profile.source == "kiotaka"
    assert profile.poc_price == 100.0
    assert profile.poc_volume == 20.0
    assert profile.delta_volume == 7.0


def test_kiotaka_volume_profile_disabled_without_opt_in() -> None:
    settings = _settings()
    settings.kiotaka_volume_profile_enabled = False
    client = KiotakaClient(settings)
    client._fetch_volume_profile = lambda **_: (_ for _ in ()).throw(AssertionError("network"))  # type: ignore[assignment]

    result = client.volume_profile(
        exchange="HYPERLIQUID_FUTURES",
        raw_symbol="BTC",
        interval="HOUR",
        from_s=1_700_000_000,
        period_s=3600,
    )

    assert result is None


def test_kiotaka_volume_profile_uses_cache_when_enabled() -> None:
    settings = _settings()
    settings.kiotaka_volume_profile_enabled = True
    client = KiotakaClient(settings)
    calls = 0
    base_profile = build_volume_profile_from_triplets([(100.0, 10.0, 1.0)])
    assert base_profile is not None

    def fetch(**_: object):
        nonlocal calls
        calls += 1
        return replace(base_profile, fetched_at=time.monotonic())

    client._fetch_volume_profile = fetch  # type: ignore[assignment]
    first = client.volume_profile(
        exchange="HYPERLIQUID_FUTURES",
        raw_symbol="BTC",
        interval="HOUR",
        from_s=1_700_000_000,
        period_s=3600,
    )
    second = client.volume_profile(
        exchange="HYPERLIQUID_FUTURES",
        raw_symbol="BTC",
        interval="HOUR",
        from_s=1_700_000_000,
        period_s=3600,
    )

    assert first is second
    assert calls == 1

from datetime import datetime, timezone

import httpx

from app.core.config import Settings
from app.core.enums import Direction
from app.engine.signal_engine import SignalEngine
from app.services.coinmarketcap import (
    AltcoinSeasonIndex,
    CoinMarketCapClient,
    FearGreedIndex,
    _iter_records,
    parse_altcoin_season,
    symbol_candidates,
)
from app.services.market_dashboard import MarketDashboardService, parse_coinmarketcap_bitcoin_news
from app.services.whale_alert import parse_public_alert, summarize_public_alerts, summarize_transactions
from app.strategies.base import CandleInput, StrategyContext


def test_symbol_candidates_normalizes_hyperliquid_wrappers() -> None:
    assert symbol_candidates("kPEPE") == ["KPEPE", "PEPE"]
    assert symbol_candidates("BTC/USDC") == ["BTC"]


def test_coinmarketcap_record_parser_accepts_list_and_maps() -> None:
    assert _iter_records({"BTC": [{"symbol": "BTC"}]}) == [{"symbol": "BTC"}]
    assert _iter_records([{"symbol": "ETH"}]) == [{"symbol": "ETH"}]


def test_altcoin_season_parser_clamps_index() -> None:
    parsed = parse_altcoin_season(
        {
            "altcoin_index": 120,
            "altcoin_marketcap": "12345.6",
            "snapshot_time": "2026-05-12T00:00:00Z",
            "yearly_high": 91,
            "yearly_low": 12,
        }
    )

    assert parsed is not None
    assert parsed.altcoin_index == 100
    assert parsed.altcoin_marketcap == 12345.6


def test_coinmarketcap_can_be_disabled(monkeypatch) -> None:
    def fail_get(*args, **kwargs):
        raise AssertionError("external HTTP should not be called when CoinMarketCap is disabled")

    monkeypatch.setattr(httpx, "get", fail_get)
    client = CoinMarketCapClient(
        Settings(
            coinmarketcap_api_key="cmc-key",
            coinmarketcap_enabled=False,
            coingecko_api_key=None,
        )
    )

    assert client.latest_quote("BTC") is None
    assert client.latest_fear_greed() is None
    assert client.latest_altcoin_season() is None


def test_coinmarketcap_quota_error_temporarily_disables_cmc_and_falls_back(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        request = httpx.Request("GET", url)
        if "coinmarketcap" in url:
            response = httpx.Response(429, request=request, json={"status": {"error_code": 1008}})
            raise httpx.HTTPStatusError("quota exceeded", request=request, response=response)
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "symbol": "btc",
                    "current_price": 100_000,
                    "total_volume": 10_000_000,
                    "price_change_percentage_24h": 1.2,
                }
            ],
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = CoinMarketCapClient(
        Settings(
            coinmarketcap_api_key="cmc-key",
            coinmarketcap_enabled=True,
            external_quote_enrichment_enabled=True,
            coingecko_api_key="cg-key",
            coingecko_enabled=True,
            coinmarketcap_quota_cooldown_seconds=60,
        )
    )

    quote = client.latest_quote("BTC")
    assert quote is not None
    assert quote.source == "coingecko"
    assert quote.price == 100_000

    assert client.latest_fear_greed() is None
    assert sum("coinmarketcap" in call for call in calls) == 1


def test_external_quote_enrichment_must_be_enabled_for_coingecko(monkeypatch) -> None:
    def fail_get(*args, **kwargs):
        raise AssertionError("quote enrichment should not call external providers by default")

    monkeypatch.setattr(httpx, "get", fail_get)
    client = CoinMarketCapClient(
        Settings(
            coinmarketcap_api_key=None,
            coingecko_api_key="cg-key",
            coingecko_enabled=True,
            external_quote_enrichment_enabled=False,
        )
    )

    assert client.latest_quote("BTC") is None
    client.prefetch_quotes(["BTC", "ETH"])


def test_coinmarketcap_key_info_blocks_calls_when_monthly_credits_are_exhausted(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        request = httpx.Request("GET", url)
        assert url.endswith("/v1/key/info")
        return httpx.Response(
            200,
            request=request,
            json={
                "data": {
                    "plan": {
                        "credit_limit_monthly": 10_000,
                        "credit_limit_monthly_reset_timestamp": "2026-06-01T00:00:00.000Z",
                        "rate_limit_minute": 60,
                    },
                    "usage": {
                        "current_minute": {"requests_made": 1, "requests_left": 59},
                        "current_day": {"credits_used": 100, "credits_left": 0},
                        "current_month": {"credits_used": 10_000, "credits_left": 0},
                    },
                }
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = CoinMarketCapClient(
        Settings(
            coinmarketcap_api_key="cmc-key",
            coinmarketcap_enabled=True,
            coinmarketcap_min_credits_left=1,
        )
    )

    usage = client.key_usage(force=True)

    assert usage.available is False
    assert usage.disabled_reason == "monthly_credits_exhausted"
    assert usage.usage["current_month"]["credits_left"] == 0


def test_coinmarketcap_bitcoin_news_parser_reads_next_data() -> None:
    raw = """
    <html>
      <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "news": [
                {
                  "title": "Bitcoin holds key support as traders watch ETF flows",
                  "url": "/news/bitcoin-holds-key-support",
                  "sourceName": "CoinMarketCap",
                  "publishedAt": "2026-05-28T10:00:00Z",
                  "description": "Market participants are tracking liquidity and ETF flows."
                }
              ]
            }
          }
        }
      </script>
    </html>
    """

    parsed = parse_coinmarketcap_bitcoin_news(raw)

    assert len(parsed) == 1
    assert parsed[0].title == "Bitcoin holds key support as traders watch ETF flows"
    assert parsed[0].url == "https://coinmarketcap.com/news/bitcoin-holds-key-support"
    assert parsed[0].source == "CoinMarketCap"


def test_coinmarketcap_bitcoin_news_parser_prioritizes_related_articles() -> None:
    raw = """
    <html>
      <a href="/alexandria/article/what-is-wrapped-bitcoin">What is wrapped Bitcoin?</a>
      <h3>Related Articles</h3>
      <a href="/alexandria/article/bitcoin-drops-below-75k-as-peace-deal-progress-sends-oil-lower">
        Bitcoin Drops Below $75K as Peace Deal Progress Sends Oil Lower
      </a>
      <a href="/alexandria/article/bitcoin-4-year-cycle-points-to-october-2026-bottom">
        Bitcoin 4-Year Cycle Points to October 2026 Bottom, Benjamin Cowen Says
      </a>
    </html>
    """

    parsed = parse_coinmarketcap_bitcoin_news(raw)

    assert len(parsed) == 2
    assert parsed[0].title == "Bitcoin Drops Below $75K as Peace Deal Progress Sends Oil Lower"
    assert parsed[1].title == "Bitcoin 4-Year Cycle Points to October 2026 Bottom, Benjamin Cowen Says"


def test_market_dashboard_snapshot_uses_15_minute_cache(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/global"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "total_market_cap": {"usd": 3_000_000_000_000},
                        "total_volume": {"usd": 120_000_000_000},
                        "market_cap_percentage": {"btc": 54.2},
                        "market_cap_change_percentage_24h_usd": 1.5,
                        "updated_at": 1779960000,
                    }
                },
            )
        if url.endswith("/coins/markets"):
            return httpx.Response(
                200,
                request=request,
                json=[
                    {
                        "symbol": "btc",
                        "name": "Bitcoin",
                        "current_price": 109_000,
                        "market_cap": 2_000_000_000_000,
                        "price_change_percentage_24h": 2.1,
                        "price_change_percentage_1h_in_currency": 0.2,
                        "price_change_percentage_7d_in_currency": 4.0,
                        "price_change_percentage_200d_in_currency": 50,
                    },
                    {
                        "symbol": "eth",
                        "name": "Ethereum",
                        "current_price": 4_000,
                        "market_cap": 480_000_000_000,
                        "price_change_percentage_24h": 3.1,
                        "price_change_percentage_200d_in_currency": 60,
                    },
                ],
            )
        if "alternative.me" in url:
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": [
                        {
                            "value": "65",
                            "value_classification": "Greed",
                            "timestamp": "1779960000",
                        }
                    ]
                },
            )
        if "coinmarketcap.com/currencies/bitcoin" in url:
            return httpx.Response(
                200,
                request=request,
                text=(
                    '<script id="__NEXT_DATA__" type="application/json">'
                    '{"props":{"pageProps":{"news":[{"title":"Bitcoin market update keeps traders focused on liquidity",'
                    '"url":"/news/bitcoin-market-update","sourceName":"CoinMarketCap",'
                    '"publishedAt":"2026-05-28T10:00:00Z"}]}}}'
                    "</script>"
                ),
            )
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "get", fake_get)
    service = MarketDashboardService(Settings(coingecko_api_key=None, coinmarketcap_api_key=None))

    first = service.snapshot()
    second = service.snapshot()

    assert first["metrics"]["btc_price_usd"] == 109_000
    assert first["fear_greed"]["value"] == 65
    assert first["altcoin_season"]["altcoin_index"] == 100
    assert first["news"][0]["title"] == "Bitcoin market update keeps traders focused on liquidity"
    assert first["cache"]["max_age_seconds"] == 900
    assert second["cache"]["hit"] is True
    assert len(calls) == 4


def test_signal_liquidity_filter_uses_average_quote_volume() -> None:
    candles = [
        CandleInput(
            open_time=datetime.now(timezone.utc),
            open=1,
            high=1,
            low=1,
            close=1,
            volume=100,
        )
        for _ in range(19)
    ]
    candles.append(
        CandleInput(
            open_time=datetime.now(timezone.utc),
            open=1,
            high=1,
            low=1,
            close=2,
            volume=500,
        )
    )

    assert SignalEngine._average_quote_volume(candles) == 145


def test_whale_flow_treats_exchange_withdrawals_as_bullish() -> None:
    flow = summarize_transactions(
        symbol="BTC",
        lookback_minutes=60,
        min_value_usd=500_000,
        transactions=[
            {
                "amount_usd": 3_000_000,
                "from": {"owner_type": "exchange"},
                "to": {"owner_type": "unknown"},
            },
            {
                "amount_usd": 500_000,
                "from": {"owner_type": "unknown"},
                "to": {"owner_type": "exchange"},
            },
            {
                "amount_usd": 1_000_000,
                "from": {"owner_type": "unknown"},
                "to": {"owner_type": "unknown"},
            },
        ],
    )

    assert flow is not None
    assert flow.direction == Direction.LONG
    assert flow.confidence >= 70
    assert flow.net_flow_usd == 2_500_000
    assert flow.neutral_flow_usd == 1_000_000


def test_whale_flow_balanced_directional_flow_is_neutral() -> None:
    flow = summarize_transactions(
        symbol="ETH",
        lookback_minutes=60,
        min_value_usd=500_000,
        transactions=[
            {
                "amount_usd": 1_000_000,
                "from": {"owner_type": "exchange"},
                "to": {"owner_type": "unknown"},
            },
            {
                "amount_usd": 950_000,
                "from": {"owner_type": "unknown"},
                "to": {"owner_type": "exchange"},
            },
        ],
    )

    assert flow is not None
    assert flow.direction == Direction.NEUTRAL
    assert flow.confidence == 0


def test_public_whale_alert_parser_reads_csv_like_feed() -> None:
    alert = parse_public_alert(
        '1778514627,🚨🚨🚨,"742 #BTC","60,335,999 USD",'
        '" transferred from unknown wallet to Coinbase Institutional",'
        "https://whale-alert.io/transaction/bitcoin/example"
    )

    assert alert is not None
    assert alert.symbol == "BTC"
    assert alert.amount == 742
    assert alert.amount_usd == 60_335_999
    assert "Coinbase" in alert.text


def test_public_whale_alert_flow_uses_exchange_direction() -> None:
    alerts = [
        parse_public_alert(
            '1778514627,🚨🚨🚨,"742 #BTC","60,335,999 USD",'
            '" transferred from unknown wallet to Coinbase Institutional",'
            "https://whale-alert.io/transaction/bitcoin/example"
        ),
        parse_public_alert(
            '1778515093,🚨🚨🚨,"657 #BTC","10,000,000 USD",'
            '" transferred from Coinbase Institutional to unknown new wallet",'
            "https://whale-alert.io/transaction/bitcoin/example2"
        ),
    ]

    flow = summarize_public_alerts(
        symbols={"BTC"},
        alerts=[alert for alert in alerts if alert is not None],
        min_value_usd=500_000,
    )

    assert flow is not None
    assert flow.direction == Direction.SHORT
    assert flow.bearish_flow_usd == 60_335_999
    assert flow.bullish_flow_usd == 10_000_000
    assert flow.source == "whale_alert_public"


def test_fear_greed_result_is_low_confidence_context_signal() -> None:
    context = StrategyContext(
        market="hyperliquid",
        symbol="BTC",
        timeframe="1h",
        market_type="perp",
        candles=[
            CandleInput(
                open_time=datetime.now(timezone.utc),
                open=100,
                high=102,
                low=99,
                close=101,
                volume=1_000,
            )
        ],
    )

    result = SignalEngine.__new__(SignalEngine)._fear_greed_result(
        context,
        FearGreedIndex(value=78, classification="Greed", timestamp="1710000000"),
    )

    assert result is not None
    assert result.strategy_name == "fear_greed"
    assert result.direction == Direction.LONG
    assert result.confidence == 48


def test_altcoin_season_result_supports_altcoins_above_75() -> None:
    context = StrategyContext(
        market="hyperliquid",
        symbol="SOL",
        timeframe="1h",
        market_type="perp",
        candles=[
            CandleInput(
                open_time=datetime.now(timezone.utc),
                open=100,
                high=102,
                low=99,
                close=101,
                volume=1_000,
            )
        ],
    )

    result = SignalEngine.__new__(SignalEngine)._altcoin_season_result(
        context,
        AltcoinSeasonIndex(
            altcoin_index=82,
            altcoin_marketcap=None,
            snapshot_time=None,
            yearly_high=None,
            yearly_high_date=None,
            yearly_low=None,
            yearly_low_date=None,
        ),
        "SOL",
    )

    assert result is not None
    assert result.strategy_name == "altcoin_season"
    assert result.direction == Direction.LONG
    assert result.confidence == 27

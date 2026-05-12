from datetime import datetime, timezone

from app.core.enums import Direction
from app.engine.signal_engine import SignalEngine
from app.services.coinmarketcap import (
    AltcoinSeasonIndex,
    FearGreedIndex,
    _iter_records,
    parse_altcoin_season,
    symbol_candidates,
)
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

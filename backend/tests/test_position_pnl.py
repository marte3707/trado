from decimal import Decimal

from app.db.models import Market, Position
from app.services.position_pnl import calculate_unrealized_pnl, mark_price_for_market


def test_calculate_unrealized_pnl_for_long_position() -> None:
    position = Position(
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("2"),
        status="open",
        metadata_json={},
    )

    assert calculate_unrealized_pnl(position, Decimal("103.5")) == Decimal("7.0")


def test_calculate_unrealized_pnl_for_short_position() -> None:
    position = Position(
        market_id=1,
        mode="paper",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("2"),
        status="open",
        metadata_json={},
    )

    assert calculate_unrealized_pnl(position, Decimal("97.5")) == Decimal("5.0")


def test_mark_price_uses_hyperliquid_coin_metadata_first() -> None:
    market = Market(
        exchange="hyperliquid",
        symbol="BTC/USDC",
        base="BTC",
        quote="USDC",
        market_type="spot",
        active=True,
        metadata_json={"hl_coin": "@1"},
    )

    assert mark_price_for_market(market, {"BTC": "100", "@1": "101.25"}) == Decimal("101.25")


def test_mark_price_falls_back_to_symbol_or_base() -> None:
    market = Market(
        exchange="hyperliquid",
        symbol="ETH/USDC",
        base="ETH",
        quote="USDC",
        market_type="spot",
        active=True,
        metadata_json={},
    )

    assert mark_price_for_market(market, {"ETH": "2500.5"}) == Decimal("2500.5")

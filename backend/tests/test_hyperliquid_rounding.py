from decimal import Decimal
from types import SimpleNamespace

from app.services.hyperliquid import _price_decimals_for_coin, _round_price_for_coin


def _info(*, asset: int, sz_decimals: int, coin: str = "APT") -> SimpleNamespace:
    return SimpleNamespace(
        name_to_coin={coin: coin},
        coin_to_asset={coin: asset},
        asset_to_sz_decimals={asset: sz_decimals},
    )


def test_price_decimals_for_perp_follow_hl_rule() -> None:
    info = _info(asset=27, sz_decimals=2, coin="APT")

    assert _price_decimals_for_coin("APT", info) == 4


def test_round_price_for_perp_uses_sz_decimals_adjusted_precision() -> None:
    info = _info(asset=27, sz_decimals=2, coin="APT")

    assert _round_price_for_coin(Decimal("0.917911"), "APT", info) == 0.9179


def test_round_price_for_large_perp_preserves_hl_sig_fig_rule() -> None:
    info = _info(asset=0, sz_decimals=5, coin="BTC")

    assert _round_price_for_coin(Decimal("106234.567"), "BTC", info) == 106230.0


def test_round_price_for_spot_uses_8_minus_sz_decimals() -> None:
    info = _info(asset=10000, sz_decimals=5, coin="PURR/USDC")

    assert _round_price_for_coin(Decimal("0.1234567"), "PURR/USDC", info) == 0.123


def test_round_price_for_coin_falls_back_when_info_missing() -> None:
    assert _round_price_for_coin(Decimal("0.917911"), "APT", None) == 0.91791

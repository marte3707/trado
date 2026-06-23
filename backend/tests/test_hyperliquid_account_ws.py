from app.services.hyperliquid_account_ws import (
    account_channel_payload,
    account_subscription_payload,
)


def test_account_subscription_payload_includes_public_address() -> None:
    assert account_subscription_payload("clearinghouseState", "0xabc") == {
        "method": "subscribe",
        "subscription": {"type": "clearinghouseState", "user": "0xabc"},
    }


def test_account_channel_payload_unwraps_account_snapshots() -> None:
    user_state = {"marginSummary": {"accountValue": "100"}}
    spot_state = {"balances": [{"coin": "USDC", "total": "10"}]}
    orders = [{"coin": "BTC", "oid": 123}]

    assert account_channel_payload(
        {
            "channel": "clearinghouseState",
            "data": {"user": "0xabc", "clearinghouseState": user_state},
        }
    ) == ("user_state", user_state)
    assert account_channel_payload(
        {"channel": "spotState", "data": {"user": "0xabc", "spotState": spot_state}}
    ) == ("spot_state", spot_state)
    assert account_channel_payload(
        {"channel": "openOrders", "data": {"user": "0xabc", "orders": orders}}
    ) == ("open_orders", orders)


def test_account_channel_payload_ignores_ack_and_invalid_messages() -> None:
    assert account_channel_payload(
        {
            "channel": "subscriptionResponse",
            "data": {"subscription": {"type": "openOrders", "user": "0xabc"}},
        }
    ) is None
    assert account_channel_payload({"channel": "openOrders", "data": {"orders": {}}}) is None

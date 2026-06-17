import pytest

from app.core.config import Settings
from app.services._circuit import (
    hyperliquid_account_breaker,
    hyperliquid_market_data_breaker,
    hyperliquid_order_breaker,
)
from app.services.hyperliquid import (
    HyperliquidClient,
    HyperliquidTradingClient,
    HyperliquidTransientError,
    _call_trading_with_retry,
)


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_order_retry_only_on_429_then_breaker(monkeypatch) -> None:
    monkeypatch.setattr("app.services.hyperliquid.time.sleep", lambda _: None)
    hyperliquid_order_breaker.reset()
    hyperliquid_account_breaker.reset()
    calls = {"n": 0}

    def always_429():
        calls["n"] += 1
        raise _StatusError(429)

    with pytest.raises(HyperliquidTransientError):
        _call_trading_with_retry(
            always_429,
            attempts=4,
            breaker=hyperliquid_order_breaker,
            retryable_status={429},
        )
    assert calls["n"] == 4  # 429 e' un throttle ⇒ ritenta tutti i tentativi
    # Il breaker ordini ha registrato il fallimento; quello account resta intatto.
    assert hyperliquid_account_breaker.allow() is True
    hyperliquid_order_breaker.reset()


def test_order_non_retryable_status_propagates_without_retry(monkeypatch) -> None:
    monkeypatch.setattr("app.services.hyperliquid.time.sleep", lambda _: None)
    hyperliquid_order_breaker.reset()
    calls = {"n": 0}

    def rejected():
        calls["n"] += 1
        raise _StatusError(422)  # rifiuto logico, NON un throttle

    with pytest.raises(_StatusError):
        _call_trading_with_retry(
            rejected,
            breaker=hyperliquid_order_breaker,
            retryable_status={429},
        )
    assert calls["n"] == 1  # nessun retry su status non-retryable
    hyperliquid_order_breaker.reset()


def test_set_leverage_retries_429_on_order_breaker(monkeypatch) -> None:
    monkeypatch.setattr("app.services.hyperliquid.time.sleep", lambda _: None)
    hyperliquid_order_breaker.reset()
    hyperliquid_account_breaker.reset()
    calls = {"n": 0}

    class FakeExchange:
        def update_leverage(self, _leverage: int, _coin: str, _is_cross: bool) -> None:
            calls["n"] += 1
            raise _StatusError(429)

    client = HyperliquidTradingClient(Settings())
    monkeypatch.setattr(client, "_exchange", lambda _expires_after_seconds: FakeExchange())

    with pytest.raises(HyperliquidTransientError):
        client.set_leverage("BTC", 3, is_cross=False)

    assert calls["n"] == 3
    assert hyperliquid_account_breaker.allow() is True
    hyperliquid_order_breaker.reset()


@pytest.mark.asyncio
async def test_candle_rate_limit_breaker_does_not_block_account_calls(monkeypatch) -> None:
    hyperliquid_account_breaker.reset()
    hyperliquid_market_data_breaker.reset()
    client = HyperliquidClient(Settings())
    calls: list[tuple[str, int]] = []

    async def fake_post_info_raw(payload, *, attempts: int = 4):
        calls.append((payload["type"], attempts))
        if payload["type"] == "candleSnapshot":
            raise HyperliquidTransientError("simulated 429")
        return {"ok": True}

    monkeypatch.setattr(client, "_post_info_raw", fake_post_info_raw)

    for _ in range(5):
        with pytest.raises(HyperliquidTransientError):
            await client.candles("BTC", "5m", 1, 2)

    assert hyperliquid_market_data_breaker.allow() is False
    assert await client.user_state("0xabc") == {"ok": True}
    assert calls[:5] == [("candleSnapshot", 2)] * 5
    assert calls[-1] == ("clearinghouseState", 4)

    hyperliquid_account_breaker.reset()
    hyperliquid_market_data_breaker.reset()

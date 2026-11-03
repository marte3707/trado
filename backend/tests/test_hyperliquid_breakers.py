from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.models import AppSetting
from app.services import hyperliquid_rate_limit as hl_rate_limit
from app.services._circuit import (
    hyperliquid_account_breaker,
    hyperliquid_market_data_breaker,
    hyperliquid_order_breaker,
)
from app.services.hyperliquid import (
    HyperliquidClient,
    HyperliquidRateLimitError,
    HyperliquidTradingClient,
    HyperliquidTransientError,
    _call_trading_with_retry,
)


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _RateLimitDb:
    def __init__(self) -> None:
        self.rows: dict[str, AppSetting] = {
            hl_rate_limit.HYPERLIQUID_RATE_LIMIT_SETTING_KEY: AppSetting(
                key=hl_rate_limit.HYPERLIQUID_RATE_LIMIT_SETTING_KEY,
                value={
                    "ip_weight_budget_per_minute": 3,
                    "live_preflight_reserve_weight": 0,
                    "noncritical_cooldown_seconds": 600,
                },
            )
        }
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def get(self, model, key, **_kwargs):
        if model is AppSetting:
            return self.rows.get(key)
        return None

    def add(self, row: object) -> None:
        self.added.append(row)
        if isinstance(row, AppSetting):
            self.rows[row.key] = row

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


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


def test_rate_limit_governor_accounts_weight_and_denies_over_budget(monkeypatch) -> None:
    db = _RateLimitDb()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(hl_rate_limit, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        hl_rate_limit,
        "_now",
        lambda: datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )

    first = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="allMids",
        rate_limit_class="info",
    )
    second = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="allMids",
        rate_limit_class="info",
    )

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "ip_weight_budget"
    state = db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_STATE_KEY].value
    assert state["window_weight"] == 2
    assert any(
        getattr(row, "event_type", None) == "hyperliquid_rate_limit_skipped"
        for row in db.added
    )


def test_rate_limit_governor_reserves_info_budget_for_live_preflight(monkeypatch) -> None:
    db = _RateLimitDb()
    db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_SETTING_KEY].value = {
        "ip_weight_budget_per_minute": 60,
        "live_preflight_reserve_weight": 20,
        "noncritical_cooldown_seconds": 600,
    }
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(hl_rate_limit, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        hl_rate_limit,
        "_now",
        lambda: datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )

    assert hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="candleSnapshot", rate_limit_class="info"
    ).allowed
    assert hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="candleSnapshot", rate_limit_class="info"
    ).allowed
    denied_prefetch = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="l2Book", rate_limit_class="info"
    )
    allowed_preflight = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="l2Book", rate_limit_class="info", live_preflight=True
    )
    exhausted_preflight = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="l2Book", rate_limit_class="info", live_preflight=True
    )

    assert denied_prefetch.allowed is False
    assert allowed_preflight.allowed is True
    assert exhausted_preflight.allowed is False
    assert db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_STATE_KEY].value["window_weight"] == 60


def test_available_info_capacity_excludes_live_reserve(monkeypatch) -> None:
    db = _RateLimitDb()
    db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_SETTING_KEY].value = {
        "ip_weight_budget_per_minute": 60,
        "live_preflight_reserve_weight": 20,
        "noncritical_cooldown_seconds": 600,
    }
    db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_STATE_KEY] = AppSetting(
        key=hl_rate_limit.HYPERLIQUID_RATE_LIMIT_STATE_KEY,
        value={
            "window_started_at": "2026-06-17T12:00:00+00:00",
            "window_weight": 20,
        },
    )
    monkeypatch.setattr(
        hl_rate_limit,
        "_now",
        lambda: datetime(2026, 6, 17, 12, 0, 30, tzinfo=UTC),
    )

    capacity = hl_rate_limit.available_hyperliquid_info_request_capacity(
        db,
        request_type="candleSnapshot",
    )

    assert capacity == 1


def test_live_risk_reads_use_reserve_without_exceeding_total_budget(monkeypatch) -> None:
    db = _RateLimitDb()
    db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_SETTING_KEY].value = {
        "ip_weight_budget_per_minute": 64,
        "live_preflight_reserve_weight": 24,
    }
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(hl_rate_limit, "SessionLocal", lambda: db)
    monkeypatch.setattr(hl_rate_limit, "_now", lambda: datetime(2026, 9, 3, tzinfo=UTC))

    def request(kind):
        return hl_rate_limit.check_hyperliquid_rate_limit(request_type=kind, rate_limit_class="info")

    assert request("candleSnapshot").allowed
    assert request("candleSnapshot").allowed
    assert not request("clearinghouseState").allowed
    with hl_rate_limit.live_risk_request_priority():
        assert request("clearinghouseState").allowed
        assert request("spotClearinghouseState").allowed
        assert request("frontendOpenOrders").allowed
        denied = request("clearinghouseState")
    assert not denied.allowed
    assert denied.reason == "ip_weight_budget"
    assert db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_STATE_KEY].value["window_weight"] == 64


def test_live_risk_priority_preserves_cooldown_and_resets_after_exception(monkeypatch) -> None:
    db = _RateLimitDb()
    db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_SETTING_KEY].value = {
        "ip_weight_budget_per_minute": 24,
        "live_preflight_reserve_weight": 24,
    }
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(hl_rate_limit, "SessionLocal", lambda: db)
    monkeypatch.setattr(hl_rate_limit, "_now", lambda: datetime(2026, 9, 3, tzinfo=UTC))
    hl_rate_limit.record_hyperliquid_rate_limit(
        request_type="userFills", rate_limit_class="info", status_code=429
    )
    with (
        pytest.raises(RuntimeError, match="failed guard"),
        hl_rate_limit.live_risk_request_priority(),
    ):
        denied = hl_rate_limit.check_hyperliquid_rate_limit(
            request_type="clearinghouseState", rate_limit_class="info"
        )
        assert denied.reason == "global_cooldown"
        raise RuntimeError("failed guard")

    # Once the external cooldown expires, ordinary reads still cannot use the reserve.
    monkeypatch.setattr(hl_rate_limit, "_now", lambda: datetime(2026, 9, 3, 1, tzinfo=UTC))
    denied = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="clearinghouseState", rate_limit_class="info"
    )
    assert denied.reason == "ip_weight_budget"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async_client", [False, True])
async def test_governor_deferral_preserves_retry_deadline_without_network(
    monkeypatch, use_async_client
) -> None:
    deadline = "2026-09-03T12:01:00+00:00"
    decision = hl_rate_limit.HyperliquidRateLimitDecision(
        False, reason="ip_weight_budget", next_retry_after=deadline, rate_limit_class="info"
    )
    monkeypatch.setattr("app.services.hyperliquid.check_hyperliquid_rate_limit", lambda **_: decision)

    def unexpected_request():
        pytest.fail("A deferred request must not reach Hyperliquid")

    client = HyperliquidClient(Settings())
    monkeypatch.setattr(client, "_post_info_raw", unexpected_request)
    with pytest.raises(HyperliquidRateLimitError) as error:
        if use_async_client:
            await client.user_state("0xabc")
        else:
            _call_trading_with_retry(unexpected_request, request_type="clearinghouseState")
    assert error.value.reason == "ip_weight_budget"
    assert error.value.next_retry_after == deadline


def test_available_fill_notional_uses_live_preflight_reserve(monkeypatch) -> None:
    call_options: dict[str, object] = {}

    class FakeInfo:
        def l2_snapshot(self, _coin: str) -> dict:
            return {
                "levels": [
                    [{"px": "100", "sz": "2"}],
                    [{"px": "101", "sz": "2"}],
                ]
            }

    def call_without_network(fn, **kwargs):
        call_options.update(kwargs)
        return fn()

    client = HyperliquidTradingClient(Settings())
    monkeypatch.setattr(client, "_info", lambda: FakeInfo())
    monkeypatch.setattr("app.services.hyperliquid._call_trading_with_retry", call_without_network)

    fillable = client.available_fill_notional(
        coin="BTC",
        is_buy=True,
        slippage_pct=2,
        mid_price=100,
    )

    assert fillable == Decimal("200.0")
    assert call_options["request_type"] == "l2Book"
    assert call_options["live_preflight"] is True


def test_rate_limit_governor_deduplicates_repeated_skip_logs(monkeypatch) -> None:
    db = _RateLimitDb()
    with hl_rate_limit._LOCAL_SKIP_LOGS_LOCK:
        hl_rate_limit._LOCAL_SKIP_LOGS.clear()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(hl_rate_limit, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        hl_rate_limit,
        "_now",
        lambda: datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )

    allowed = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="allMids",
        rate_limit_class="info",
    )
    first_denied = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="allMids",
        rate_limit_class="info",
    )
    second_denied = hl_rate_limit.check_hyperliquid_rate_limit(
        request_type="allMids",
        rate_limit_class="info",
    )

    assert allowed.allowed is True
    assert first_denied.allowed is False
    assert second_denied.allowed is False
    skip_logs = [
        row
        for row in db.added
        if getattr(row, "event_type", None) == "hyperliquid_rate_limit_skipped"
    ]
    assert len(skip_logs) == 1
    state = db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_STATE_KEY].value
    assert state["last_skip_logs"]["info:allMids:ip_weight_budget"]["suppressed_count"] == 1


def test_rate_limit_429_records_global_and_order_cooldown(monkeypatch) -> None:
    db = _RateLimitDb()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(hl_rate_limit, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        hl_rate_limit,
        "_now",
        lambda: datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )

    next_retry = hl_rate_limit.record_hyperliquid_rate_limit(
        request_type="order",
        rate_limit_class="exchange",
        status_code=429,
        error="429 too many requests",
    )

    state = db.rows[hl_rate_limit.HYPERLIQUID_RATE_LIMIT_STATE_KEY].value
    assert next_retry == state["order_action_available_at"]
    assert state["cooldown_until"] == "2026-06-17T12:10:00+00:00"
    assert state["order_action_available_at"] == "2026-06-17T12:00:10+00:00"
    assert any(
        getattr(row, "event_type", None) == "hyperliquid_rate_limit_entered"
        for row in db.added
    )


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


def test_user_fills_by_time_uses_bounded_endpoint(monkeypatch) -> None:
    calls: list[tuple[str, int, int | None, bool]] = []

    class FakeInfo:
        def user_fills_by_time(self, address, start_time, end_time=None, aggregate_by_time=False):
            calls.append((address, start_time, end_time, aggregate_by_time))
            return [{"time": start_time + 1}]

    client = HyperliquidTradingClient(Settings(hyperliquid_account_address="0xabc"))
    monkeypatch.setattr(client, "_info", lambda: FakeInfo())

    fills = client.user_fills_by_time(start_time=1000, end_time=2000)

    assert fills == [{"time": 1001}]
    assert calls == [("0xabc", 1000, 2000, False)]


def test_modify_trigger_order_uses_sdk_modify_order(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeExchange:
        info = None

        def modify_order(self, oid, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None):
            calls.append(
                {
                    "oid": oid,
                    "name": name,
                    "is_buy": is_buy,
                    "sz": sz,
                    "limit_px": limit_px,
                    "order_type": order_type,
                    "reduce_only": reduce_only,
                    "cloid": cloid,
                }
            )
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 123}}]}}}

    client = HyperliquidTradingClient(
        Settings(
            hyperliquid_account_address="0x0000000000000000000000000000000000000001",
            hyperliquid_private_key="0x" + "1" * 64,
        )
    )
    monkeypatch.setattr(client, "_exchange", lambda _expires_after_seconds: FakeExchange())

    result = client.modify_trigger_order(
        oid=111,
        coin="BTC",
        is_buy=False,
        size=Decimal("1.25"),
        trigger_price=Decimal("95"),
        tpsl="sl",
        expires_after_seconds=60,
        cloid=None,
    )

    assert result.status == "ok"
    assert result.oid == "123"
    assert calls[0]["oid"] == 111
    assert calls[0]["name"] == "BTC"
    assert calls[0]["reduce_only"] is True
    assert calls[0]["order_type"]["trigger"]["tpsl"] == "sl"


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

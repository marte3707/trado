from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.enums import OperationMode
from app.db.models import AppSetting, Base, Candle, DecisionLog, Market, Order, Position, Signal
from app.engine.execution import OrderExecutionEngine, _signal_risk_multiplier
from app.schemas import TheBullSettings
from app.services import the_bull_signals
from app.services.hyperliquid import HyperliquidOrderResult
from app.services.the_bull_signals import (
    DEFAULT_THE_BULL_SETTINGS,
    TelegramChannelMessage,
    TheBullPublicFeed,
    classify_entry_message,
    entry_symbol_from_message,
    exit_reason_from_message,
    parse_telegram_preview_html,
    process_the_bull_messages,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


class RecordingExecutor:
    def __init__(self) -> None:
        self.paper_signals: list[int] = []
        self.live_signals: list[int] = []
        self.additions: list[tuple[int, int, int, Decimal]] = []
        self.closed: list[tuple[int, Decimal, str]] = []

    def execute_paper(self, db: Session, signal: Signal) -> Position:
        self.paper_signals.append(signal.id)
        position = Position(
            market_id=signal.market_id,
            mode="paper",
            side="long",
            entry_price=signal.entry_price,
            size=Decimal("1"),
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_2,
            status="open",
            metadata_json={"source_signal_id": signal.id, "source_timeframe": signal.timeframe},
        )
        db.add(position)
        signal.status = "executed"
        db.commit()
        db.refresh(position)
        return position

    def execute_live(self, _db: Session, signal: Signal) -> Position:
        self.live_signals.append(signal.id)
        return Position(id=999, mode="live", status="open")

    def execute_live_addition(
        self,
        db: Session,
        signal: Signal,
        position: Position,
        *,
        max_additions: int,
        max_fraction: Decimal,
    ) -> Position:
        self.additions.append((signal.id, position.id, max_additions, max_fraction))
        signal.status = "executed"
        db.commit()
        return position

    def close_position(
        self,
        db: Session,
        position: Position,
        exit_price: Decimal,
        reason: str,
    ) -> None:
        self.closed.append((position.id, exit_price, reason))
        position.status = "closed"
        position.closed_at = datetime.now(UTC)
        db.commit()

    def close_live_position(self, _db: Session, _position: Position, _reason: str) -> None:
        raise AssertionError("paper test must not close a live position")


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_market_data(db: Session) -> Market:
    market = Market(
        exchange="hyperliquid",
        symbol="XPL",
        base="XPL",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "XPL", "raw": {"szDecimals": 2}},
    )
    db.add(market)
    db.flush()
    start = datetime(2026, 8, 20, tzinfo=UTC)
    for index in range(45):
        close = Decimal("100") + Decimal(index) * Decimal("0.2")
        db.add(
            Candle(
                market_id=market.id,
                timeframe="4h",
                open_time=start + timedelta(hours=index * 4),
                open=close - Decimal("0.1"),
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    db.commit()
    return market


def _message(
    message_id: int,
    text: str,
    published_at: datetime,
    *,
    reply_to: int | None = None,
    has_media: bool = False,
    media_urls: tuple[str, ...] = (),
) -> TelegramChannelMessage:
    return TelegramChannelMessage(
        channel_username="thebull_crypto",
        message_id=message_id,
        published_at=published_at,
        text=text,
        reply_to_message_id=reply_to,
        has_media=has_media,
        media_urls=media_urls,
    )


def test_public_preview_parser_separates_reply_text_from_message_text() -> None:
    html = """
    <div class="tgme_widget_message text_not_supported_wrap js-widget_message"
         data-post="thebull_crypto/19371">
      <a class="tgme_widget_message_reply user-color-default"
         href="https://t.me/thebull_crypto/19369">
        <div class="tgme_widget_message_text js-message_reply_text">Buying $XPL here!</div>
      </a>
      <div class="tgme_widget_message_text js-message_text">$XPL up +25% from entry!</div>
      <time datetime="2026-08-29T16:46:51+00:00">16:46</time>
    </div>
    """

    messages = parse_telegram_preview_html(html)

    assert len(messages) == 1
    assert messages[0].message_id == 19371
    assert messages[0].reply_to_message_id == 19369
    assert messages[0].text == "$XPL up +25% from entry!"


def test_public_preview_parser_marks_media_and_external_context() -> None:
    html = """
    <div class="tgme_widget_message" data-post="thebull_crypto/20001">
      <a class="tgme_widget_message_photo_wrap" href="https://cdn.example/chart.jpg"></a>
      <div class="tgme_widget_message_text js-message_text">
        $SUI <a href="https://x.com/thebull_crypto/status/1">chart context</a>
      </div>
      <time datetime="2026-09-18T10:00:00+00:00">10:00</time>
    </div>
    """

    messages = parse_telegram_preview_html(html)

    assert len(messages) == 1
    assert messages[0].has_media is True
    assert messages[0].media_urls == ()
    assert messages[0].external_urls == ("https://x.com/thebull_crypto/status/1",)


def test_public_preview_parser_extracts_safe_chart_url_from_photo_style() -> None:
    html = """
    <div class="tgme_widget_message" data-post="thebull_crypto/20002">
      <a class="tgme_widget_message_photo_wrap"
         style="background-image:url('https://cdn5.telesco.pe/file/chart.png')"></a>
      <div class="tgme_widget_message_text js-message_text">$XPL</div>
      <time datetime="2026-09-18T10:01:00+00:00">10:01</time>
    </div>
    """

    messages = parse_telegram_preview_html(html)

    assert messages[0].has_media is True
    assert messages[0].media_urls == ("https://cdn5.telesco.pe/file/chart.png",)


def test_public_preview_parser_does_not_treat_channel_avatar_as_chart() -> None:
    html = """
    <div class="tgme_widget_message" data-post="thebull_crypto/20003">
      <div class="tgme_widget_message_user"><i class="tgme_widget_message_user_photo">
        <img src="https://cdn5.telesco.pe/file/avatar.jpg"></i></div>
      <div class="tgme_widget_message_text js-message_text">
        The $MEGA dip is for buying!
      </div>
      <time datetime="2026-10-12T10:31:44+00:00">10:31</time>
    </div>
    """

    messages = parse_telegram_preview_html(html)

    assert len(messages) == 1
    assert messages[0].has_media is False
    assert messages[0].media_urls == ()
    assert classify_entry_message(messages[0].text).intent == "buy_the_dip"


def test_public_feed_paginates_to_the_configured_recent_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def page(*message_ids: int) -> str:
        return "\n".join(
            f'''<div class="tgme_widget_message" data-post="thebull_crypto/{message_id}">
                <div class="tgme_widget_message_text js-message_text">post {message_id}</div>
                <time datetime="2026-09-22T10:00:00+00:00">10:00</time>
            </div>'''
            for message_id in message_ids
        )

    pages = {
        None: page(209, 210),
        209: page(207, 208),
        207: page(205, 206),
        205: page(203, 204),
        203: page(201, 202),
    }
    requested: list[int | None] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(
            self,
            _url: str,
            *,
            params: dict[str, int] | None = None,
        ) -> FakeResponse:
            before = params["before"] if params else None
            requested.append(before)
            return FakeResponse(pages[before])

    monkeypatch.setattr(the_bull_signals.httpx, "AsyncClient", FakeClient)

    messages = asyncio.run(
        TheBullPublicFeed("thebull_crypto", history_limit=10).fetch_latest()
    )

    assert [message.message_id for message in messages] == list(range(201, 211))
    assert requested == [None, 209, 207, 205, 203]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Buying $XPL here!", "XPL"),
        ("Buy $ERA", "ERA"),
        ("$INIT Buy Setup", "INIT"),
        ("$PARTI is a BUY!", "PARTI"),
        ("Buying #XPL here!", "XPL"),
        ("#PARTI is a BUY!", "PARTI"),
        ("XRPUSD - Bids a LONG position (Swing)", "XRP"),
    ],
)
def test_entry_parser_accepts_only_explicit_buy_language(text: str, expected: str) -> None:
    assert entry_symbol_from_message(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "$XPL bullish",
        "$ERA is breaking out",
        "$SAPIEN adding more",
        "$ERA buying more!",
        "Market buyers stepped in as $BTC rallied",
        "Buy $189 million of stablecoins",
        "Buy $189M of stablecoins",
    ],
)
def test_entry_parser_rejects_commentary_and_averaging(text: str) -> None:
    assert entry_symbol_from_message(text) is None


def test_numeric_dollar_amount_is_not_a_ticker() -> None:
    assert the_bull_signals._ticker_symbol_from_message(
        "Weekly spending on stablecoin cards hit a new record of $189 million"
    ) is None
    assert the_bull_signals._ticker_symbol_from_message(
        "Spending reached $189 million while $BTC moved higher"
    ) == "BTC"
    assert the_bull_signals._ticker_symbol_from_message(
        "Stablecoin spending reached $189M"
    ) is None
    assert the_bull_signals._ticker_symbol_from_message("#2026") is None


@pytest.mark.parametrize(
    ("text", "symbol", "intent"),
    [
        ("$TREE is in buy zone.", "TREE", "buy_zone"),
        ("Buy the dip $SENT", "SENT", "buy_the_dip"),
        ("$SENT buying here = buying support!", "SENT", "support_buy"),
        ("Adding $OP on this support.", "OP", "support_buy"),
        ("$ERA buying more!", "ERA", "averaging_add"),
        ("$SENT breakout and retest done", "SENT", "breakout_retest"),
        ("$PARTI wedge is breaking out", "PARTI", "breakout_active"),
        ("$BERA clear break-out!", "BERA", "breakout_call"),
        ("$PARTI double bottom", "PARTI", "double_bottom"),
        ("#LPT next leg up", "LPT", "next_leg_up"),
    ],
)
def test_broader_buy_language_is_audit_only(
    text: str,
    symbol: str,
    intent: str,
) -> None:
    classification = classify_entry_message(text)

    assert classification is not None
    assert classification.symbol == symbol
    assert classification.intent == intent
    assert classification.execution_eligible is False
    assert entry_symbol_from_message(text) is None


def test_profit_reply_is_exit_but_continuation_update_is_not() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    profit = _message(2, "$XPL up +25% from entry!", now, reply_to=1)
    continuation = _message(
        3,
        "$XPL up +25% from entry but just getting started",
        now,
        reply_to=1,
    )

    assert exit_reason_from_message(profit, close_on_profit_update=True) == (
        "the_bull_profit_update"
    )
    assert exit_reason_from_message(continuation, close_on_profit_update=True) is None
    assert exit_reason_from_message(profit, close_on_profit_update=False) is None


def test_first_poll_bootstraps_without_trading_history() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {**DEFAULT_THE_BULL_SETTINGS, "enabled": True}

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(100, "Buying $XPL here!", now)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    assert result["status"] == "initialized"
    assert executor.paper_signals == []
    assert db.query(Signal).count() == 0
    assert db.get(AppSetting, "the_bull_state").value["last_message_id"] == 100


def test_poll_without_new_messages_persists_its_timestamp() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {**DEFAULT_THE_BULL_SETTINGS, "enabled": True}
    db.add(
        AppSetting(
            key="the_bull_state",
            value={"last_message_id": 100, "last_polled_at": "older"},
        )
    )
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(100, "Existing channel post", now - timedelta(minutes=1))],
        config=config,
        now=now,
    )

    assert result["processed"] == 0
    assert db.get(AppSetting, "the_bull_state").value["last_polled_at"] == now.isoformat()


def test_new_buy_opens_one_paper_trade_and_profit_reply_closes_it() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {**DEFAULT_THE_BULL_SETTINGS, "enabled": True, "execution_mode": "paper"}
    settings = Settings(operation_mode=OperationMode.PAPER_TRADING)

    process_the_bull_messages(
        db,
        settings,
        executor,  # type: ignore[arg-type]
        [_message(100, "Latest channel post", now)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )
    entry_result = process_the_bull_messages(
        db,
        settings,
        executor,  # type: ignore[arg-type]
        [_message(101, "Buying $XPL here!", now + timedelta(minutes=1))],
        config=config,
        mids={"XPL": "110"},
        now=now + timedelta(minutes=1),
    )

    signal = db.query(Signal).one()
    assert entry_result["opened"] == 1
    assert executor.paper_signals == [signal.id]
    assert signal.direction == "LONG"
    assert signal.stop_loss < signal.entry_price < signal.take_profit_1
    assert signal.strategy_breakdown["_external_source"]["risk_multiplier"] == 0.5

    exit_result = process_the_bull_messages(
        db,
        settings,
        executor,  # type: ignore[arg-type]
        [
            _message(
                102,
                "$XPL up +20% from entry!",
                now + timedelta(minutes=2),
                reply_to=101,
            )
        ],
        config=config,
        mids={"XPL": "120"},
        now=now + timedelta(minutes=2),
    )

    assert exit_result["closed"] == 1
    assert executor.closed == [(db.query(Position).one().id, Decimal("120"), "the_bull_profit_update")]
    assert db.query(Position).one().status == "closed"


def test_broader_buy_is_persisted_for_audit_without_creating_a_trade() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "live",
        "expanded_entry_execution": False,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, "Adding $XPL on this support.", now)],
        config=config,
        now=now,
    )

    candidate = db.query(DecisionLog).filter_by(event_type="the_bull_entry_candidate").one()
    assert result["candidates"] == 1
    assert result["opened"] == 0
    assert executor.paper_signals == []
    assert executor.live_signals == []
    assert db.query(Signal).count() == 0
    assert candidate.data["symbol"] == "XPL"
    assert candidate.data["intent"] == "support_buy"
    assert candidate.data["automatic_execution"] is False
    assert db.get(AppSetting, "the_bull_state").value["last_candidate"]["message_id"] == 101


def test_enabled_expanded_entry_opens_a_breakout_retest_call() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "expanded_entry_execution": True,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(101, "$XPL breakout and retest done", now)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    signal = db.query(Signal).one()
    assert result["opened"] == 1
    assert executor.paper_signals == [signal.id]
    assert signal.strategy_breakdown["_external_source"]["entry_intent"] == "breakout_retest"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("$XPL clear break-out!", "breakout_call"),
        ("$XPL double bottom", "double_bottom"),
        ("$XPL next leg up", "next_leg_up"),
        ("$XPL bullish", "bullish_bias"),
        ("Do not miss $XPL", "watchlist_entry"),
    ],
)
def test_enabled_expanded_entry_executes_broader_source_call_language(
    text: str,
    intent: str,
) -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "expanded_entry_execution": True,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(101, text, now)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    signal = db.query(Signal).one()
    assert result["opened"] == 1
    assert executor.paper_signals == [signal.id]
    assert signal.strategy_breakdown["_external_source"]["entry_intent"] == intent


def test_enabled_expanded_entry_keeps_averaging_as_audit_only() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "expanded_entry_execution": True,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(101, "$XPL buying more!", now)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    candidate = db.query(DecisionLog).filter_by(event_type="the_bull_entry_candidate").one()
    assert result["opened"] == 0
    assert executor.paper_signals == []
    assert db.query(Signal).count() == 0
    assert candidate.data["intent"] == "averaging_add"


class _AdditionTrading:
    def __init__(self, *, stop_modify_fails: bool = False) -> None:
        self.market_orders: list[dict] = []
        self.stop_modifications: list[dict] = []
        self.unwinds: list[dict] = []
        self.stop_modify_fails = stop_modify_fails

    def account_summary(self) -> tuple[Decimal, Decimal]:
        return Decimal("10000"), Decimal("10000")

    def user_state(self, _address: str) -> dict:
        return {"assetPositions": [{"position": {
            "coin": "XPL", "szi": "10", "leverage": {"type": "isolated", "value": "2"},
        }}]}

    def frontend_open_orders(self, _address: str) -> list[dict]:
        return [{"coin": "XPL", "oid": 123}]

    def available_fill_depth(self, **_kwargs) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(fillable_notional=Decimal("100000"))

    def submit_market_order(self, **kwargs) -> HyperliquidOrderResult:
        self.market_orders.append(kwargs)
        return HyperliquidOrderResult(
            status="ok", oid="456", avg_price=Decimal("110"),
            filled_size=kwargs["size"], raw_response={"status": "ok"},
        )

    def modify_trigger_order(self, **kwargs) -> HyperliquidOrderResult:
        self.stop_modifications.append(kwargs)
        if self.stop_modify_fails:
            raise RuntimeError("stop modify failed")
        return HyperliquidOrderResult(
            status="ok", oid="124", avg_price=None, filled_size=None,
            raw_response={"status": "ok"},
        )

    def submit_market_close(self, **kwargs) -> HyperliquidOrderResult:
        self.unwinds.append(kwargs)
        return HyperliquidOrderResult(
            status="ok", oid="789", avg_price=Decimal("109"),
            filled_size=kwargs["size"], raw_response={"status": "ok"},
        )


def _addition_case(*, initial_size: Decimal = Decimal("10")) -> tuple[Session, Signal, Position, OrderExecutionEngine]:
    db = _session()
    market = _seed_market_data(db)
    db.add_all([
        AppSetting(key="operation_mode", value={"value": "AUTO_TRADE"}),
        AppSetting(key="enable_live_trading", value={"value": True}),
        AppSetting(key="live_execution", value={
            "enable_mainnet_live_trading": True,
            "order_notional_cap_usd": 500,
            "min_order_notional_usd": 10,
        }),
        AppSetting(key="risk", value={"max_open_positions": 1}),
    ])
    signal = Signal(
        market_id=market.id, timeframe="4h", direction="LONG", score=90,
        confidence_label="external", entry_price=Decimal("110"),
        stop_loss=Decimal("90"), take_profit_1=Decimal("150"),
        risk_reward=Decimal("3"), reason="The Bull dip",
        strategy_breakdown={"_external_source": {
            "provider": "the_bull", "message_id": 101, "entry_intent": "buy_the_dip",
        }},
        status="new",
    )
    position = Position(
        market_id=market.id, mode="live", side="long", status="open",
        entry_price=Decimal("100"), size=Decimal("10"), stop_loss=Decimal("95"),
        metadata_json={
            "external_source": {"provider": "the_bull"},
            "initial_size": str(initial_size), "remaining_size": "10",
            "contract_leverage": 2,
        },
    )
    db.add_all([signal, position])
    db.flush()
    db.add(Order(
        position_id=position.id, signal_id=signal.id, mode="live",
        order_type="stop_loss", side="sell", price=Decimal("95"),
        size=Decimal("10"), status="ok", reduce_only=True,
        hyperliquid_order_id="123", cloid="stop-original", raw_response={},
    ))
    db.commit()
    settings = Settings(
        operation_mode=OperationMode.AUTO_TRADE,
        enable_live_trading=True,
        hyperliquid_account_address="0x1111111111111111111111111111111111111111",
        hyperliquid_private_key="0x2222222222222222222222222222222222222222222222222222222222222222",
    )
    return db, signal, position, OrderExecutionEngine(settings)


def test_live_dip_addition_caps_size_and_resizes_existing_stop() -> None:
    db, signal, position, executor = _addition_case()
    trading = _AdditionTrading()

    result = executor.execute_live_addition(db, signal, position, trading_client=trading)

    assert result is position
    assert trading.market_orders[0]["size"] == Decimal("3.00")
    assert trading.stop_modifications[0]["size"] == Decimal("13.00")
    assert position.size == Decimal("13.00")
    assert position.metadata_json["initial_size"] == "10"
    assert position.metadata_json["the_bull_additions"][0]["stop_update"] == "protected"
    assert len(db.query(Position).filter_by(mode="live").all()) == 1


def test_live_dip_addition_never_bumps_thirty_percent_to_exchange_minimum() -> None:
    db, signal, position, executor = _addition_case(initial_size=Decimal("0.30"))
    trading = _AdditionTrading()

    with pytest.raises(PermissionError, match="below live minimum"):
        executor.execute_live_addition(db, signal, position, trading_client=trading)

    assert trading.market_orders == []
    assert position.size == Decimal("10")


def test_live_dip_addition_unwinds_if_stop_resize_fails() -> None:
    db, signal, position, executor = _addition_case()
    trading = _AdditionTrading(stop_modify_fails=True)

    with pytest.raises(PermissionError, match="reversed"):
        executor.execute_live_addition(db, signal, position, trading_client=trading)

    assert len(trading.market_orders) == 1
    assert trading.unwinds[0]["size"] == Decimal("3.00")
    assert position.size == Decimal("10")
    assert position.metadata_json["the_bull_additions"][0]["stop_update"] == "unwound"
    assert position.realized_pnl == Decimal("-3.00")
    assert db.query(Order).filter_by(order_type="addition_unwind").count() == 1


def test_live_dip_addition_honors_two_additions_per_open_ticker() -> None:
    db, signal, position, executor = _addition_case()
    position.metadata_json = {
        **position.metadata_json,
        "the_bull_additions": [{"message_id": 99}, {"message_id": 100}],
    }
    db.commit()
    trading = _AdditionTrading()

    with pytest.raises(PermissionError, match="addition limit reached"):
        executor.execute_live_addition(db, signal, position, trading_client=trading)

    assert trading.market_orders == []


def test_live_dip_addition_requires_the_existing_exchange_stop() -> None:
    db, signal, position, executor = _addition_case()
    trading = _AdditionTrading()
    trading.frontend_open_orders = lambda _address: []  # type: ignore[method-assign]

    with pytest.raises(PermissionError, match="protective stop is not open"):
        executor.execute_live_addition(db, signal, position, trading_client=trading)

    assert trading.market_orders == []


def test_live_dip_addition_blocks_after_an_uncertain_market_order() -> None:
    db, signal, position, executor = _addition_case()
    db.add(Order(
        position_id=position.id, signal_id=signal.id, mode="live",
        order_type="market", side="buy", price=Decimal("110"),
        size=Decimal("3"), status="submission_unknown", reduce_only=False,
        cloid="prior-addition", raw_response={"the_bull_addition": True},
    ))
    db.commit()
    trading = _AdditionTrading()

    with pytest.raises(PermissionError, match="unresolved outcome"):
        executor.execute_live_addition(db, signal, position, trading_client=trading)

    assert trading.market_orders == []


def test_buy_the_dip_adds_to_open_the_bull_position_even_when_chart_ocr_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    market = _seed_market_data(db)
    existing = Position(
        market_id=market.id, mode="live", side="long", status="open",
        entry_price=Decimal("100"), size=Decimal("10"), stop_loss=Decimal("95"),
        metadata_json={"external_source": {"provider": "the_bull"}, "initial_size": "10"},
    )
    db.add(existing)
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    executor = RecordingExecutor()
    now = datetime(2026, 10, 12, 13, 30, tzinfo=UTC)
    monkeypatch.setattr(
        the_bull_signals, "analyze_the_bull_chart",
        lambda *_args: {"status": "failed", "error": "OCR timeout"},
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, "The $XPL dip is for buying!", now, has_media=True)],
        config={
            **DEFAULT_THE_BULL_SETTINGS,
            "enabled": True,
            "execution_mode": "live",
            "max_open_positions": 1,
        },
        mids={"XPL": "110"},
        now=now,
    )

    assert result["chart_failed"] == 1
    assert result["failed"] == 0
    assert result["opened"] == 0
    assert result["added"] == 1
    assert len(executor.additions) == 1
    assert executor.additions[0][1:] == (existing.id, 2, Decimal("0.3"))


def test_hashtag_only_chart_post_opens_when_expanded_calls_are_enabled() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "expanded_entry_execution": True,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(101, "#XPL", now, has_media=True)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    signal = db.query(Signal).one()
    assert result["opened"] == 1
    assert executor.paper_signals == [signal.id]
    assert signal.strategy_breakdown["_external_source"]["entry_intent"] == "tagged_chart_entry"


def test_hashtag_only_chart_post_remains_manual_when_expanded_calls_are_disabled() -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "expanded_entry_execution": False,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(101, "#XPL", now, has_media=True)],
        config=config,
        now=now,
    )

    assert result["opened"] == 0
    assert executor.paper_signals == []
    assert db.query(Signal).count() == 0


def test_spot_fallback_is_opt_in_and_uses_usdc_spot_when_no_perpetual_exists() -> None:
    db = _session()
    market = _seed_market_data(db)
    market.market_type = "spot"
    market.symbol = "XPL/USDC"
    market.quote = "USDC"
    market.metadata_json = {"hl_coin": "@7", "raw": {"szDecimals": 2}}
    db.add(market)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    disabled = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(101, "Buying #XPL here!", now)],
        config={**DEFAULT_THE_BULL_SETTINGS, "enabled": True, "spot_fallback_enabled": False},
        mids={"@7": "110"},
        now=now,
    )

    assert disabled["opened"] == 0
    assert db.query(Signal).count() == 0

    db.get(AppSetting, "the_bull_state").value = {"last_message_id": 101}
    db.commit()
    enabled = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(102, "Buying #XPL here!", now)],
        config={**DEFAULT_THE_BULL_SETTINGS, "enabled": True, "spot_fallback_enabled": True},
        mids={"@7": "110"},
        now=now,
    )

    signal = db.query(Signal).one()
    assert enabled["opened"] == 1
    assert signal.market_id == market.id
    assert signal.strategy_breakdown["_external_source"]["instrument_type"] == "spot"
    assert signal.strategy_breakdown["_external_source"]["spot_fallback"] is True


def test_chart_zone_is_recorded_as_audit_candidate_without_creating_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "chart_analysis_enabled": True,
        "chart_zone_execution": False,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {
            "status": "reviewed",
            "symbol": "XPL",
            "buy_zones": [{"lower": "98", "upper": "101", "label": "accumulation"}],
            "has_actionable_buy_zone": True,
            "confidence": "high",
            "summary": "Highlighted accumulation range.",
            "image_url": "https://cdn5.telesco.pe/file/chart.png",
        },
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [
            _message(
                101,
                "$XPL",
                now,
                has_media=True,
                media_urls=("https://cdn5.telesco.pe/file/chart.png",),
            )
        ],
        config=config,
        now=now,
    )

    chart_log = db.query(DecisionLog).filter_by(event_type="the_bull_chart_candidate").one()
    assert result["chart_reviewed"] == 1
    assert result["chart_candidates"] == 1
    assert result["opened"] == 0
    assert db.query(Signal).count() == 0
    assert executor.paper_signals == []
    assert chart_log.data["execution"] == "audit_only"
    assert db.get(AppSetting, "the_bull_state").value["last_chart_candidate"]["symbol"] == "XPL"


def test_chart_zone_inside_current_midpoint_opens_through_normal_risk_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "chart_analysis_enabled": True,
        "chart_zone_execution": True,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {
            "status": "reviewed",
            "symbol": "XPL",
            "buy_zones": [{"lower": "98", "upper": "101", "label": "BUY ZONE"}],
            "has_actionable_buy_zone": True,
            "confidence": "high",
            "summary": "Printed range.",
            "image_url": "https://cdn5.telesco.pe/file/chart.png",
        },
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [
            _message(
                101,
                "$XPL",
                now,
                has_media=True,
                media_urls=("https://cdn5.telesco.pe/file/chart.png",),
            )
        ],
        config=config,
        mids={"XPL": "100"},
        now=now,
    )

    chart_log = db.query(DecisionLog).filter_by(event_type="the_bull_chart_candidate").one()
    signal = db.query(Signal).one()
    assert result["opened"] == 1
    assert executor.paper_signals == [signal.id]
    assert signal.strategy_breakdown["_external_source"]["entry_intent"] == "chart_zone"
    assert chart_log.data["execution"] == "eligible"


@pytest.mark.parametrize("ocr_status", ["failed", "reviewed"])
def test_chart_without_buy_zone_falls_back_to_ticker_only_caption_through_risk_path(
    monkeypatch: pytest.MonkeyPatch,
    ocr_status: str,
) -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "chart_analysis_enabled": True,
        "chart_zone_execution": True,
        "chart_ocr_fallback_execution": True,
        "max_signal_age_minutes": 60,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {
            "status": ocr_status,
            "reason": "local OCR unavailable" if ocr_status == "failed" else None,
            "has_actionable_buy_zone": False,
        },
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [
            _message(
                101,
                "$XPL",
                now - timedelta(minutes=59),
                has_media=True,
                media_urls=("https://cdn5.telesco.pe/file/chart.png",),
            )
        ],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    signal = db.query(Signal).one()
    assert result["opened"] == 1
    assert result["chart_ocr_fallbacks"] == 1
    assert executor.paper_signals == [signal.id]
    assert signal.strategy_breakdown["_external_source"]["entry_intent"] == "chart_ocr_fallback"
    assert signal.stop_loss < signal.entry_price < signal.take_profit_1


@pytest.mark.parametrize(
    ("caption", "market_symbol", "expected_classification"),
    [
        ("$BTC roadmap", "BTC", "context_review"),
        ("$ROBO is starting to take off!", "ROBO", "context_review"),
        (
            "$DOT is edging ever closer to having its own native stablecoin. Few!",
            "DOT",
            "context_review",
        ),
        (
            "Weekly spending on stablecoin cards hit a new record of $189 million",
            "XPL",
            "neutral",
        ),
    ],
)
@pytest.mark.parametrize("ocr_status", ["failed", "reviewed"])
def test_chart_ocr_fallback_cannot_turn_commentary_or_amount_into_live_entry(
    monkeypatch: pytest.MonkeyPatch,
    caption: str,
    market_symbol: str,
    expected_classification: str,
    ocr_status: str,
) -> None:
    db = _session()
    market = _seed_market_data(db)
    market.symbol = market_symbol
    market.base = market_symbol
    market.metadata_json = {"hl_coin": market_symbol, "raw": {"szDecimals": 2}}
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    executor = RecordingExecutor()
    now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {
            "status": ocr_status,
            "reason": "local OCR unavailable" if ocr_status == "failed" else None,
            "has_actionable_buy_zone": False,
        },
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, caption, now, has_media=True)],
        config={
            **DEFAULT_THE_BULL_SETTINGS,
            "enabled": True,
            "execution_mode": "live",
            "chart_analysis_enabled": True,
            "chart_zone_execution": True,
            "chart_ocr_fallback_execution": True,
        },
        mids={market_symbol: "110"},
        now=now,
    )

    assert result["opened"] == 0
    assert result["chart_ocr_fallbacks"] == 0
    assert db.query(Signal).count() == 0
    assert executor.live_signals == []
    observed = db.query(DecisionLog).filter_by(event_type="the_bull_message_observed").one()
    assert observed.data["classification"] == expected_classification
    assert observed.data["execution"] != "eligible_ocr_fallback"


def test_explicit_buy_still_opens_when_chart_ocr_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    _seed_market_data(db)
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    executor = RecordingExecutor()
    now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {"status": "failed", "reason": "local OCR unavailable"},
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, "Buy $XPL", now, has_media=True)],
        config={
            **DEFAULT_THE_BULL_SETTINGS,
            "enabled": True,
            "execution_mode": "live",
            "chart_analysis_enabled": True,
            "chart_zone_execution": True,
            "chart_ocr_fallback_execution": True,
        },
        mids={"XPL": "110"},
        now=now,
    )

    assert result["opened"] == 1
    assert result["chart_ocr_fallbacks"] == 0
    assert executor.live_signals == [db.query(Signal).one().id]


def test_buy_the_dip_opens_without_chart_when_no_position_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    _seed_market_data(db)
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    executor = RecordingExecutor()
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    def unexpected_chart_analysis(*_args):  # noqa: ANN002, ANN202
        raise AssertionError("A text-only dip entry must not depend on OCR")

    monkeypatch.setattr(the_bull_signals, "analyze_the_bull_chart", unexpected_chart_analysis)
    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, "The $XPL dip is for buying!", now)],
        config={
            **DEFAULT_THE_BULL_SETTINGS,
            "enabled": True,
            "execution_mode": "live",
            "chart_analysis_enabled": True,
            "expanded_entry_execution": True,
        },
        mids={"XPL": "110"},
        now=now,
    )

    assert result["opened"] == 1
    signal = db.query(Signal).one()
    assert signal.strategy_breakdown["_external_source"]["entry_intent"] == "buy_the_dip"
    assert executor.live_signals == [signal.id]


@pytest.mark.parametrize(
    ("caption", "symbol", "reply_to"),
    [
        ("$SEI +90%", "SEI", 19424),
        ("$INIT +84% Patience is the key.", "INIT", 19322),
        ("$VIRTUAL is outperforming #BTC.", "VIRTUAL", 19480),
    ],
)
def test_performance_update_cannot_open_live_via_chart_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caption: str,
    symbol: str,
    reply_to: int,
) -> None:
    db = _session()
    market = _seed_market_data(db)
    market.symbol = symbol
    market.base = symbol
    market.metadata_json = {"hl_coin": symbol, "raw": {"szDecimals": 2}}
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    executor = RecordingExecutor()
    now = datetime(2026, 9, 28, 7, 34, tzinfo=UTC)

    def unexpected_chart_analysis(*_args):  # noqa: ANN002, ANN202
        raise AssertionError("A performance update must not enter the chart execution path")

    monkeypatch.setattr(the_bull_signals, "analyze_the_bull_chart", unexpected_chart_analysis)
    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, caption, now, reply_to=reply_to, has_media=True)],
        config={
            **DEFAULT_THE_BULL_SETTINGS,
            "enabled": True,
            "execution_mode": "live",
            "chart_analysis_enabled": True,
            "chart_zone_execution": True,
            "chart_ocr_fallback_execution": True,
            "close_on_profit_update": False,
        },
        mids={symbol: "110"},
        now=now,
    )

    assert result["opened"] == 0
    assert result["chart_ocr_fallbacks"] == 0
    assert result["ignored"] == 1
    assert db.query(Signal).count() == 0
    assert executor.live_signals == []
    observed = db.query(DecisionLog).filter_by(event_type="the_bull_message_observed").one()
    assert observed.data["classification"] == "performance_update"
    assert observed.data["execution"] == "ignored_non_entry"


def test_explicit_buy_reply_still_opens_after_performance_update_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    _seed_market_data(db)
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    executor = RecordingExecutor()
    now = datetime(2026, 9, 28, 7, 34, tzinfo=UTC)
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {"status": "failed", "reason": "local OCR unavailable"},
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, "The $XPL dip is for buying!", now, reply_to=99, has_media=True)],
        config={
            **DEFAULT_THE_BULL_SETTINGS,
            "enabled": True,
            "execution_mode": "live",
            "chart_analysis_enabled": True,
            "chart_zone_execution": True,
            "chart_ocr_fallback_execution": True,
        },
        mids={"XPL": "110"},
        now=now,
    )

    assert result["opened"] == 1
    assert executor.live_signals == [db.query(Signal).one().id]


def test_chart_ocr_fallback_does_not_override_a_read_zone_outside_current_midpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "chart_analysis_enabled": True,
        "chart_zone_execution": True,
        "chart_ocr_fallback_execution": True,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {
            "status": "reviewed",
            "symbol": "XPL",
            "buy_zones": [{"lower": "98", "upper": "101", "label": "BUY ZONE"}],
            "has_actionable_buy_zone": True,
            "confidence": "high",
            "summary": "Printed range.",
        },
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [
            _message(
                101,
                "$XPL",
                now,
                has_media=True,
                media_urls=("https://cdn5.telesco.pe/file/chart.png",),
            )
        ],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    assert result["opened"] == 0
    assert result["chart_ocr_fallbacks"] == 0
    assert db.query(Signal).count() == 0
    assert executor.paper_signals == []


def test_chart_zone_does_not_promote_an_averaging_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "chart_analysis_enabled": True,
        "chart_zone_execution": True,
        "chart_ocr_fallback_execution": True,
    }
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()
    monkeypatch.setattr(
        the_bull_signals,
        "analyze_the_bull_chart",
        lambda *_args: {
            "status": "reviewed",
            "symbol": "XPL",
            "buy_zones": [{"lower": "98", "upper": "101", "label": "BUY ZONE"}],
            "has_actionable_buy_zone": True,
            "confidence": "high",
            "summary": "Printed range.",
        },
    )

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [
            _message(
                101,
                "$XPL buying more!",
                now,
                has_media=True,
                media_urls=("https://cdn5.telesco.pe/file/chart.png",),
            )
        ],
        config=config,
        mids={"XPL": "100"},
        now=now,
    )

    assert result["opened"] == 0
    assert db.query(Signal).count() == 0
    assert executor.paper_signals == []


def test_live_the_bull_entry_is_not_blocked_when_its_paper_companion_cannot_open() -> None:
    class LiveOnlyExecutor(RecordingExecutor):
        def execute_paper(self, _db: Session, _signal: Signal) -> None:
            return None

    db = _session()
    _seed_market_data(db)
    executor = LiveOnlyExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {**DEFAULT_THE_BULL_SETTINGS, "enabled": True, "execution_mode": "live"}
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, "Buying $XPL here!", now)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    signal = db.query(Signal).one()
    assert result["opened"] == 1
    assert executor.paper_signals == []
    assert executor.live_signals == [signal.id]


def test_live_the_bull_entry_is_not_blocked_by_an_existing_paper_position() -> None:
    db = _session()
    market = _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    db.add(
        Position(
            market_id=market.id,
            mode="paper",
            side="long",
            entry_price=Decimal("100"),
            size=Decimal("1"),
            status="open",
            metadata_json={"source_signal_id": 99},
        )
    )
    db.add(AppSetting(key="the_bull_state", value={"last_message_id": 100}))
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.AUTO_TRADE),
        executor,  # type: ignore[arg-type]
        [_message(101, "Buying $XPL here!", now, has_media=True)],
        config={
            **DEFAULT_THE_BULL_SETTINGS,
            "enabled": True,
            "execution_mode": "live",
            "chart_analysis_enabled": False,
        },
        mids={"XPL": "110"},
        now=now,
    )

    signal = db.query(Signal).one()
    assert result["opened"] == 1
    assert executor.live_signals == [signal.id]


def test_local_vision_inference_uses_prices_printed_with_zone_label() -> None:
    response = the_bull_signals._infer_local_chart_zones(
        [
            {"text": "BUY ZONE 98.5 - 101.0", "x": 0.35, "y": 0.45, "height": 0.04},
        ]
    )

    assert response["has_actionable_buy_zone"] is True
    assert response["confidence"] == "high"
    assert response["buy_zones"] == [
        {"lower": "98.5", "upper": "101", "label": "BUY ZONE 98.5 - 101.0"}
    ]


def test_local_vision_inference_estimates_zone_from_price_axis() -> None:
    response = the_bull_signals._infer_local_chart_zones(
        [
            {"text": "100", "x": 0.90, "y": 0.10, "height": 0.02},
            {"text": "110", "x": 0.90, "y": 0.50, "height": 0.02},
            {"text": "120", "x": 0.90, "y": 0.90, "height": 0.02},
            {"text": "Accumulation", "x": 0.40, "y": 0.48, "height": 0.04},
        ]
    )

    assert response["has_actionable_buy_zone"] is True
    assert response["confidence"] == "medium"
    assert response["buy_zones"] == [
        {"lower": "106.75", "upper": "112.75", "label": "Accumulation"}
    ]


def test_red_price_tag_is_found_only_on_right_chart_axis() -> None:
    image = Image.new("RGB", (800, 424), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((775, 208, 793, 215), fill=(220, 40, 55))
    draw.line((140, 130, 330, 80), fill=(220, 40, 55), width=3)
    output = BytesIO()
    image.save(output, format="PNG")

    marker = the_bull_signals._red_axis_price_marker(output.getvalue())

    assert marker is not None
    assert marker["x_min"] == 775
    assert marker["y_center"] == 211


def test_high_low_axis_tags_are_context_only() -> None:
    image = Image.new("RGB", (800, 424), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((752, 76, 762, 79), fill=(90, 140, 210))
    draw.rectangle((752, 312, 762, 315), fill=(90, 140, 210))
    output = BytesIO()
    image.save(output, format="PNG")

    assert the_bull_signals._blue_axis_range_labels(output.getvalue()) == {
        "high": True,
        "low": True,
    }
    draw.rectangle((752, 312, 762, 315), fill="white")
    output = BytesIO()
    image.save(output, format="PNG")
    assert the_bull_signals._blue_axis_range_labels(output.getvalue()) == {
        "high": False,
        "low": False,
    }


def test_red_axis_interest_is_estimated_but_never_auto_executable() -> None:
    marker = {"y_center": 211, "image_width": 800}
    tokens = [
        {"text": "1,300", "x_pixel": 779, "y_pixel": 195.5},
        {"text": "1.000", "x_pixel": 779, "y_pixel": 229.5},
        {"text": "1.300", "x_pixel": 779, "y_pixel": 229.5},
        {"text": "LOW 0.721", "x_pixel": 750, "y_pixel": 315},
    ]

    zone = the_bull_signals._interest_zone_from_axis_tokens(marker, tokens)

    assert zone is not None
    assert Decimal(zone["lower"]) < Decimal("1.16") < Decimal(zone["upper"])
    assert zone["automatic_execution"] is False
    assert the_bull_signals._chart_annotation_labels(tokens)["low"] is True
    assert the_bull_signals._infer_local_chart_zones(tokens)["has_actionable_buy_zone"] is False


def test_red_axis_interest_rejects_implausible_or_missing_scale() -> None:
    marker = {"y_center": 217, "image_width": 800}
    bad_tokens = [
        {"text": "0.0740", "x_pixel": 773, "y_pixel": 199},
        {"text": "1.0000", "x_pixel": 773, "y_pixel": 230},
    ]
    assert the_bull_signals._interest_zone_from_axis_tokens(marker, bad_tokens) is None
    good_tokens = bad_tokens + [{"text": "8/0643", "x_pixel": 773, "y_pixel": 230}]
    zone = the_bull_signals._interest_zone_from_axis_tokens(marker, good_tokens)
    assert zone is not None
    assert Decimal("0.066") < Decimal(zone["center"]) < Decimal("0.070")


def test_long_position_labels_are_context_not_a_buy_signal() -> None:
    result = the_bull_signals._infer_local_chart_zones(
        [
            {"text": "Long Position", "x": 0.4, "y": 0.5},
            {"text": "Target 1.50", "x": 0.7, "y": 0.7},
            {"text": "Stop 1.00", "x": 0.7, "y": 0.3},
            {"text": "High 1.80", "x": 0.9, "y": 0.8},
            {"text": "Low 0.80", "x": 0.9, "y": 0.2},
        ]
    )
    assert result["chart_labels"] == {
        "low": True,
        "high": True,
        "long_position": True,
        "target": True,
        "stop": True,
    }
    assert result["has_actionable_buy_zone"] is False


def test_red_axis_interest_is_reported_without_becoming_a_live_buy_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = Image.new("RGB", (800, 424), "white")
    ImageDraw.Draw(image).rectangle((770, 214, 790, 220), fill=(220, 40, 55))
    output = BytesIO()
    image.save(output, format="PNG")
    monkeypatch.setattr(
        the_bull_signals,
        "_download_public_chart",
        lambda _url: (output.getvalue(), "image/png"),
    )
    monkeypatch.setattr(the_bull_signals, "_run_macos_vision_ocr", lambda *_args: [])
    monkeypatch.setattr(
        the_bull_signals,
        "_ocr_near_red_axis_marker",
        lambda *_args: [
            {"text": "0.0740", "x_pixel": 773, "y_pixel": 199},
            {"text": "8/0643", "x_pixel": 773, "y_pixel": 230},
            {"text": "LOW 0.0433", "x_pixel": 755, "y_pixel": 310},
        ],
    )

    result = the_bull_signals.analyze_the_bull_chart(
        Settings(),
        _message(
            101,
            "Buying more $LA",
            datetime(2026, 9, 29, 8, 51, tzinfo=UTC),
            has_media=True,
            media_urls=("https://cdn5.telesco.pe/file/chart.png",),
        ),
        DEFAULT_THE_BULL_SETTINGS,
    )

    assert result["has_actionable_buy_zone"] is False
    assert result["buy_zones"] == []
    assert result["chart_labels"]["low"] is True
    assert result["interest_zones"][0]["automatic_execution"] is False
    assert Decimal("0.066") < Decimal(result["interest_zones"][0]["center"]) < Decimal("0.070")


def test_chart_media_host_is_restricted_to_telegram_cdn() -> None:
    assert the_bull_signals._is_supported_media_url("https://cdn5.telesco.pe/file/chart.png")
    assert not the_bull_signals._is_supported_media_url("https://example.com/chart.png")
    assert not the_bull_signals._is_supported_media_url("http://cdn5.telesco.pe/file/chart.png")


def test_source_specific_open_position_cap_blocks_new_entry() -> None:
    db = _session()
    market = _seed_market_data(db)
    executor = RecordingExecutor()
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    config = {
        **DEFAULT_THE_BULL_SETTINGS,
        "enabled": True,
        "execution_mode": "paper",
        "max_open_positions": 1,
    }
    db.add(
        Position(
            market_id=market.id,
            mode="paper",
            side="long",
            entry_price=Decimal("100"),
            size=Decimal("1"),
            status="open",
            metadata_json={"external_source": {"provider": "the_bull"}},
        )
    )
    db.add(
        AppSetting(
            key="the_bull_state",
            value={"last_message_id": 100, "tracked_messages": {}},
        )
    )
    db.commit()

    result = process_the_bull_messages(
        db,
        Settings(operation_mode=OperationMode.PAPER_TRADING),
        executor,  # type: ignore[arg-type]
        [_message(101, "Buying $XPL here!", now)],
        config=config,
        mids={"XPL": "110"},
        now=now,
    )

    assert result["opened"] == 0
    assert executor.paper_signals == []
    assert db.query(Signal).count() == 0


def test_invalid_live_risk_controls_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TheBullSettings.model_validate(
            {
                "execution_mode": "live",
                "risk_multiplier": 1.5,
                "max_signal_age_minutes": 0,
            }
        )
    with pytest.raises(ValidationError):
        TheBullSettings.model_validate({"max_additions_per_ticker": 3})
    with pytest.raises(ValidationError):
        TheBullSettings.model_validate({"max_addition_fraction": 0.31})


def test_external_risk_multiplier_can_only_reduce_global_risk() -> None:
    signal = Signal(strategy_breakdown={"_external_source": {"risk_multiplier": 0.5}})
    excessive = Signal(strategy_breakdown={"_external_source": {"risk_multiplier": 3}})
    regular = Signal(strategy_breakdown={})

    assert _signal_risk_multiplier(signal) == 0.5
    assert _signal_risk_multiplier(excessive) == 1.0
    assert _signal_risk_multiplier(regular) == 1.0

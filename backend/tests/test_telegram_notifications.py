from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.models import AppSetting, Market, Order, Position, Signal
from app.notifications import telegram as telegram_module
from app.notifications.telegram import (
    _DAILY_RECAP_STATE_KEY,
    TelegramNotifier,
    _build_channel_strategy_message,
    _build_daily_recap_message,
    _build_live_close_message,
    _build_message,
    _daily_recap_summary,
)


class RecordingNotifier(TelegramNotifier):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.photo_calls: list[dict[str, object]] = []
        self.text_calls: list[dict[str, object]] = []
        self.text_result_calls: list[dict[str, object]] = []
        self.pin_calls: list[dict[str, object]] = []
        self.unpin_calls: list[dict[str, object]] = []

    async def _send_photo(
        self,
        png_bytes: bytes,
        caption: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        self.photo_calls.append(
            {
                "png_bytes": png_bytes,
                "caption": caption,
                "chat_id": chat_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return True

    async def _send_text(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        self.text_calls.append(
            {
                "text": text,
                "chat_id": chat_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return True

    async def _send_text_result(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, object] | None:
        self.text_result_calls.append(
            {
                "text": text,
                "chat_id": chat_id,
                "reply_to_message_id": reply_to_message_id,
                "disable_web_page_preview": disable_web_page_preview,
            }
        )
        return {"message_id": 777}

    async def _pin_message(
        self,
        *,
        chat_id: str,
        message_id: int,
        disable_notification: bool = True,
    ) -> bool:
        self.pin_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": disable_notification,
            }
        )
        return True

    async def _unpin_message(
        self,
        *,
        chat_id: str,
        message_id: int,
    ) -> bool:
        self.unpin_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
            }
        )
        return True

    def _signal_channel_reply_id(self, db, signal_id: int | None) -> int | None:  # noqa: ANN001
        return 321 if signal_id is not None else None


def _build_notifier() -> RecordingNotifier:
    return RecordingNotifier(
        Settings(
            telegram_bot_token="admin-token",
            telegram_chat_id="admin-chat",
            telegram_signal_chat_id="signal-chat",
            landing_bot_token="public-token",
            public_api_base_url="https://signals.trado.test",
        )
    )


class _FakeQuery:
    def __init__(self, rows=None, scalar_value=None) -> None:
        self.rows = rows or []
        self.scalar_value = scalar_value

    def filter(self, *_args, **_kwargs) -> "_FakeQuery":
        return self

    def order_by(self, *_args, **_kwargs) -> "_FakeQuery":
        return self

    def all(self):
        return list(self.rows)

    def scalar(self):
        return self.scalar_value


class FakeDailyRecapDb:
    def __init__(
        self,
        *,
        open_trades: int,
        closed_positions: list[Position],
        orders: list[Order],
        state: AppSetting | None = None,
    ) -> None:
        self.open_trades = open_trades
        self.closed_positions = closed_positions
        self.orders = orders
        self.state = state
        self.added: list[object] = []
        self.commits = 0

    def get(self, model, key):  # noqa: ANN001
        if model is AppSetting and key == _DAILY_RECAP_STATE_KEY:
            return self.state
        return None

    def add(self, row: object) -> None:
        self.added.append(row)
        if isinstance(row, AppSetting) and row.key == _DAILY_RECAP_STATE_KEY:
            self.state = row

    def commit(self) -> None:
        self.commits += 1

    def query(self, model):  # noqa: ANN001
        model_str = str(model)
        if "count" in model_str:
            return _FakeQuery(scalar_value=self.open_trades)
        if model is Position:
            return _FakeQuery(rows=self.closed_positions)
        if model is Order:
            return _FakeQuery(rows=self.orders)
        raise AssertionError(f"unexpected query model: {model!r}")


def _closed_position(
    *,
    position_id: int,
    side: str,
    entry_price: str,
    size: str,
    realized_pnl: str,
    initial_stop: str,
) -> Position:
    return Position(
        id=position_id,
        mode="live",
        status="closed",
        side=side,
        entry_price=Decimal(entry_price),
        size=Decimal(size),
        realized_pnl=Decimal(realized_pnl),
        closed_at=datetime(2026, 6, 5, 8, 0, tzinfo=UTC),
        metadata_json={
            "initial_size": size,
            "initial_stop_loss": initial_stop,
        },
    )


def test_build_live_close_message_restores_full_close_usd_pnl() -> None:
    message = _build_live_close_message(
        symbol="BTC",
        side="LONG",
        reason="trailing_stop",
        entry_price=Decimal("100"),
        exit_price=Decimal("112.5"),
        remaining_size=None,
        step_pnl=None,
        total_pnl=Decimal("150"),
        is_partial=False,
        pnl_pct=12.5,
    )

    assert "PnL: <code>+150.00 $" in message
    assert "PnL %: <code>+12.5%</code>" in message


def test_build_channel_strategy_message_contains_both_languages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        telegram_module,
        "get_settings",
        lambda: Settings(public_api_base_url="https://signals.trado.test"),
    )
    message = _build_channel_strategy_message()

    assert "🇮🇹 <b>Italiano</b>" in message
    assert "🇬🇧 <b>English</b>" in message
    assert "TP1 (30%) e TP2 (30%)" in message
    assert "size restante (40%)" in message
    assert "TP1 (30%) and TP2 (30%)" in message
    assert "remaining size (40%)" in message
    assert "Non inviamo tutti gli aggiornamenti intermedi di stop" in message
    assert "We do not broadcast every stop update" in message
    assert 'href="https://signals.trado.test/api/learn/trade-management#trailing-stop"' in message
    assert 'href="https://signals.trado.test/api/learn/trade-management#risk-reward"' in message
    assert 'href="https://signals.trado.test/api/learn/trade-management#breakeven"' in message
    assert 'href="https://signals.trado.test/api/learn/trade-management#martingale"' in message
    assert 'href="https://signals.trado.test/api/learn/trade-management#averaging-down"' in message


def test_build_channel_strategy_message_falls_back_to_public_urls_when_base_is_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        telegram_module,
        "get_settings",
        lambda: Settings(),
    )
    message = _build_channel_strategy_message()

    assert 'href="https://www.investopedia.com/terms/t/trailingstop.asp"' in message
    assert 'href="https://www.investopedia.com/terms/r/riskrewardratio.asp"' in message


def test_build_signal_message_shows_tp1_tp2_and_runner_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        telegram_module,
        "get_settings",
        lambda: Settings(public_api_base_url="https://signals.trado.test"),
    )
    signal = Signal(
        market=Market(symbol="BTC"),
        timeframe="1h",
        direction="LONG",
        score=82,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit_1=Decimal("105"),
        take_profit_2=Decimal("110"),
        take_profit_3=Decimal("115"),
        risk_reward=Decimal("2.4"),
    )

    message = _build_message(signal)

    assert "TP1 (30%)" in message
    assert "TP2 (30%)" in message
    assert "Runner (40%)" in message
    assert "Move remaining size to" in message
    assert "TP3" not in message
    assert "Stop Loss" in message
    assert "trailing stop" in message
    assert "R/R" in message
    assert "href=" not in message


def test_daily_recap_summary_and_message() -> None:
    positions = [
        _closed_position(
            position_id=1,
            side="long",
            entry_price="100",
            size="1",
            realized_pnl="10",
            initial_stop="90",
        ),
        _closed_position(
            position_id=2,
            side="long",
            entry_price="200",
            size="1",
            realized_pnl="-20",
            initial_stop="180",
        ),
    ]

    summary = _daily_recap_summary(positions, {})

    assert summary["closed_trades"] == 2
    assert summary["total_pnl"] == Decimal("-10")
    assert summary["win_rate"] == 0.5
    assert summary["avg_r_multiple"] == Decimal("0")
    assert summary["total_return_pct"] == Decimal("-3.333333333333333333333333333")

    message = _build_daily_recap_message(
        opened_trades=3,
        total_return_pct=summary["total_return_pct"],
        win_rate=summary["win_rate"],
        avg_r_multiple=summary["avg_r_multiple"],
        window_start=datetime(2026, 6, 4, 8, 0, tzinfo=UTC),
        window_end=datetime(2026, 6, 5, 8, 0, tzinfo=UTC),
    )
    assert "Opened trades: <b>3</b>" in message
    assert "Total P&amp;L 24h: <code>-3.33%</code>" in message
    assert "Win rate: <b>50.0%</b>" in message
    assert "Medium R-Multiple: <b>+0.00R</b>" in message


@pytest.mark.asyncio
async def test_profitable_final_close_sends_photo_to_signal_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = _build_notifier()
    monkeypatch.setattr(telegram_module, "render_close_card", lambda **_: b"png-bytes")

    await notifier.send_live_position_closed(
        symbol="BTC",
        side="LONG",
        reason="trailing_stop",
        entry_price=Decimal("100"),
        exit_price=Decimal("112"),
        remaining_size=None,
        step_pnl=None,
        total_pnl=Decimal("150"),
        is_partial=False,
        leverage=Decimal("3"),
        pnl_pct=12.5,
        duration_seconds=3600,
        signal_id=42,
        db=object(),
    )

    assert len(notifier.photo_calls) == 2
    assert notifier.text_calls == []

    admin_photo, channel_photo = notifier.photo_calls
    assert admin_photo["chat_id"] is None
    assert "+150.00 $" in str(admin_photo["caption"])
    assert "+12.5%" in str(admin_photo["caption"])

    assert channel_photo["chat_id"] == "signal-chat"
    assert channel_photo["reply_to_message_id"] == 321
    assert "12.5%" in str(channel_photo["caption"])
    assert "$" not in str(channel_photo["caption"])


@pytest.mark.asyncio
async def test_losing_final_close_keeps_text_only_on_signal_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = _build_notifier()
    monkeypatch.setattr(telegram_module, "render_close_card", lambda **_: b"png-bytes")

    await notifier.send_live_position_closed(
        symbol="BTC",
        side="LONG",
        reason="stop_loss",
        entry_price=Decimal("100"),
        exit_price=Decimal("95"),
        remaining_size=None,
        step_pnl=None,
        total_pnl=Decimal("-50"),
        is_partial=False,
        leverage=Decimal("3"),
        pnl_pct=-5.0,
        duration_seconds=3600,
        signal_id=42,
        db=object(),
    )

    assert len(notifier.photo_calls) == 1
    assert len(notifier.text_calls) == 1
    assert notifier.text_calls[0]["chat_id"] == "signal-chat"
    assert "Stop Loss" in str(notifier.text_calls[0]["text"])


@pytest.mark.asyncio
async def test_send_channel_strategy_message_sends_and_pins() -> None:
    notifier = _build_notifier()

    result = await notifier.send_channel_strategy_message(pin=True)

    assert result == {
        "status": "sent",
        "message_id": 777,
        "pinned": True,
        "pin_hint": None,
    }
    assert len(notifier.text_result_calls) == 1
    assert notifier.text_result_calls[0]["chat_id"] == "signal-chat"
    assert "How We Trade" in str(notifier.text_result_calls[0]["text"])
    assert "not a mathematical probability of success" in str(notifier.text_result_calls[0]["text"])
    assert "skipping some trades can lead to personal results" in str(notifier.text_result_calls[0]["text"])
    assert notifier.pin_calls == [
        {
            "chat_id": "signal-chat",
            "message_id": 777,
            "disable_notification": True,
        }
    ]


@pytest.mark.asyncio
async def test_send_daily_recap_posts_to_public_channel_and_replaces_pin() -> None:
    notifier = _build_notifier()
    db = FakeDailyRecapDb(
        open_trades=2,
        closed_positions=[
            _closed_position(
                position_id=1,
                side="long",
                entry_price="100",
                size="1",
                realized_pnl="10",
                initial_stop="90",
            )
        ],
        orders=[],
        state=AppSetting(key=_DAILY_RECAP_STATE_KEY, value={"message_id": 555}),
    )

    result = await notifier.send_daily_recap(
        db,
        window_start=datetime(2026, 6, 4, 8, 0, tzinfo=UTC),
        window_end=datetime(2026, 6, 5, 8, 0, tzinfo=UTC),
    )

    assert result == {
        "status": "sent",
        "message_id": 777,
        "pinned": True,
        "unpinned_previous": True,
    }
    assert notifier.text_result_calls[-1]["chat_id"] == "signal-chat"
    assert "Total P&amp;L 24h: <code>+10.00%</code>" in str(notifier.text_result_calls[-1]["text"])
    assert notifier.unpin_calls == [{"chat_id": "signal-chat", "message_id": 555}]
    assert notifier.pin_calls[-1] == {
        "chat_id": "signal-chat",
        "message_id": 777,
        "disable_notification": True,
    }
    assert db.state is not None
    assert db.state.value["message_id"] == 777
    assert db.commits == 1

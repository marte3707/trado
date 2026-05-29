from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from app.core.config import Settings
from app.db.models import AppSetting, Candle, DecisionLog, Market, Position, Signal
from app.engine.btc_macro_protection import BtcMacroProtectionEngine
from app.engine.position_management import StopCandidate


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Query:
    rows: list[Any]

    def filter(self, *args: Any, **_kwargs: Any) -> "_Query":
        # Riconosce filtri "Candle.market_id == X" e applica il filtraggio sui rows.
        # Necessario perché `_is_position_in_profit` interroga le candele del
        # market specifico della posizione, non un dataset globale.
        for arg in args:
            if hasattr(arg, "left") and hasattr(arg, "right"):
                column_name = getattr(arg.left, "key", None)
                target = getattr(arg.right, "value", None)
                if column_name == "market_id" and target is not None:
                    filtered = [
                        row for row in self.rows if getattr(row, "market_id", None) == target
                    ]
                    return _Query(filtered)
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> "_Query":
        return self

    def limit(self, n: int) -> "_Query":
        return _Query(self.rows[:n])

    def all(self) -> list[Any]:
        return list(self.rows)

    def first(self) -> Any | None:
        return self.rows[0] if self.rows else None


@dataclass
class FakeDb:
    settings: dict[str, AppSetting] = field(default_factory=dict)
    btc_market: Market | None = None
    candles: list[Candle] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    markets_by_id: dict[int, Market] = field(default_factory=dict)
    added: list[Any] = field(default_factory=list)
    committed: int = 0
    flushed: int = 0
    rolled_back: int = 0

    def get(self, model: type[Any], key: Any) -> Any | None:
        if model is AppSetting:
            return self.settings.get(str(key))
        if model is Market:
            return self.markets_by_id.get(int(key))
        return None

    def query(self, model: type[Any]) -> _Query:
        if model is Market:
            return _Query([self.btc_market] if self.btc_market else [])
        if model is Candle:
            # Simula ORDER BY open_time DESC restituendo le candele dal più recente
            return _Query(list(reversed(self.candles)))
        if model is Position:
            return _Query(self.positions)
        if model is Signal:
            return _Query(list(self.signals))
        return _Query([])

    def add(self, row: Any) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushed += 1

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


class FakeExecutor:
    def __init__(self) -> None:
        self.closed: list[dict[str, Any]] = []
        self.paper_closed: list[dict[str, Any]] = []
        self.paper_opened: list[Signal] = []
        self.live_opened: list[Signal] = []
        self.next_paper_position_id = 100
        self.next_live_position_id = 200

    def close_live_position(
        self,
        db: Any,
        position: Position,
        reason: str = "manual",
        trading_client: Any = None,
    ) -> None:
        self.closed.append({"position_id": position.id, "reason": reason})

    def close_position(
        self,
        db: Any,
        position: Position,
        exit_price: Any,
        reason: str,
        commit: bool = True,
    ) -> None:
        position.status = "closed"
        self.paper_closed.append(
            {"position_id": position.id, "reason": reason, "exit_price": exit_price}
        )

    def execute_paper(self, db: Any, signal: Signal) -> Position:
        self.paper_opened.append(signal)
        new_position = Position(
            id=self.next_paper_position_id,
            market_id=signal.market_id,
            mode="paper",
            side="long" if str(signal.direction).endswith("LONG") else "short",
            entry_price=signal.entry_price,
            size=Decimal("1"),
            stop_loss=signal.stop_loss,
            status="open",
            metadata_json={"source_signal_id": signal.id},
        )
        self.next_paper_position_id += 1
        return new_position

    def execute_live(
        self,
        db: Any,
        signal: Signal,
        trading_client: Any = None,
    ) -> Position:
        self.live_opened.append(signal)
        new_position = Position(
            id=self.next_live_position_id,
            market_id=signal.market_id,
            mode="live",
            side="long" if str(signal.direction).endswith("LONG") else "short",
            entry_price=signal.entry_price,
            size=Decimal("1"),
            stop_loss=signal.stop_loss,
            status="open",
            metadata_json={"source_signal_id": signal.id},
        )
        self.next_live_position_id += 1
        return new_position


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://x:x@localhost/x",
        hyperliquid_network="testnet",
    )


def _config_setting(
    enabled: bool = True,
    last_trend: str = "NEUTRAL",
    timeframe: str = "5m",
    lookback: int = 3,
    pct_threshold: float = 1.5,
    vol_multiplier: float = 1.2,
    buffer_pct: float = 0.3,
    loss_exit: bool = True,
) -> AppSetting:
    return AppSetting(
        key="btc_macro_protection",
        value={
            "enabled": enabled,
            "timeframe": timeframe,
            "lookback_candles": lookback,
            "reversal_pct_threshold": pct_threshold,
            "volume_multiplier": vol_multiplier,
            "breakeven_buffer_pct": buffer_pct,
            "loss_exit_enabled": loss_exit,
            "_last_btc_trend": last_trend,
            "_last_reversal_at": None,
        },
    )


def _btc_market() -> Market:
    return Market(
        id=1,
        exchange="hyperliquid",
        symbol="BTC:USDC",
        base="BTC",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "BTC", "raw": {"szDecimals": 4}},
    )


def _candles(
    n: int,
    base_close: float = 100.0,
    last_close: float | None = None,
    base_volume: float = 1000.0,
    last_volume: float | None = None,
) -> list[Candle]:
    """Crea N candele con close uniforme, opzionalmente l'ultima con close/volume diverso."""
    rows = []
    for i in range(n):
        is_last = i == n - 1
        close = last_close if (is_last and last_close is not None) else base_close
        volume = last_volume if (is_last and last_volume is not None) else base_volume
        rows.append(
            Candle(
                id=i + 1,
                market_id=1,
                timeframe="5m",
                open_time=datetime(2026, 1, 1, 0, i * 5, tzinfo=timezone.utc),
                open=Decimal(str(close)),
                high=Decimal(str(close * 1.001)),
                low=Decimal(str(close * 0.999)),
                close=Decimal(str(close)),
                volume=Decimal(str(volume)),
            )
        )
    return rows


def _position(
    side: str = "short",
    entry: str = "100",
    stop: str | None = "105",
    unrealized_pnl: float = 5.0,
    mode: str = "live",
    market_id: int = 2,
) -> Position:
    # Position su un market non-BTC (market_id=2) di default, così
    # `_is_position_in_profit` non trova candele in cache e usa unrealized_pnl.
    return Position(
        id=10,
        market_id=market_id,
        mode=mode,
        side=side,
        entry_price=Decimal(entry),
        size=Decimal("1"),
        stop_loss=Decimal(stop) if stop else None,
        status="open",
        unrealized_pnl=Decimal(str(unrealized_pnl)),
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata_json={},
    )


def _alt_market(market_id: int = 2) -> Market:
    return Market(
        id=market_id,
        exchange="hyperliquid",
        symbol="ETH:USDC",
        base="ETH",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "ETH", "raw": {"szDecimals": 4}},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_action_when_disabled() -> None:
    engine = BtcMacroProtectionEngine(_settings())
    db = FakeDb(settings={"btc_macro_protection": _config_setting(enabled=False)})
    result = engine.run(db, FakeExecutor())
    assert result == 0
    assert db.committed == 0


def test_no_action_when_btc_market_missing() -> None:
    engine = BtcMacroProtectionEngine(_settings())
    db = FakeDb(
        settings={"btc_macro_protection": _config_setting()},
        btc_market=None,
    )
    result = engine.run(db, FakeExecutor())
    assert result == 0


def test_no_action_when_insufficient_candles() -> None:
    engine = BtcMacroProtectionEngine(_settings())
    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=6)},
        btc_market=_btc_market(),
        candles=_candles(3),  # meno di lookback + 1
    )
    result = engine.run(db, FakeExecutor())
    assert result == 0


def test_no_reversal_below_price_threshold() -> None:
    # delta = 0.5% < 1.5% → NEUTRAL, nessuna azione
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=100.5, last_volume=9000.0)
    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=3)},
        btc_market=_btc_market(),
        candles=candles,
    )
    result = engine.run(db, FakeExecutor())
    assert result == 0


def test_no_reversal_volume_insufficient() -> None:
    # delta = 2% ma volume troppo basso → NEUTRAL
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=800.0)
    # avg volume = 1000, last = 800 → ratio 0.8 < 1.2
    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=3)},
        btc_market=_btc_market(),
        candles=candles,
    )
    result = engine.run(db, FakeExecutor())
    assert result == 0


def test_no_reversal_neutral_to_bullish() -> None:
    # Cambio da NEUTRAL a BULLISH non è un reversal
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)
    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="NEUTRAL", lookback=3)},
        btc_market=_btc_market(),
        candles=candles,
    )
    result = engine.run(db, FakeExecutor())
    assert result == 0


def test_reversal_bearish_to_bullish_short_in_profit() -> None:
    # BEARISH → BULLISH con SHORT in profitto → SL aggiornato a breakeven
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="100", stop="105", unrealized_pnl=5.0)
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    with patch.object(engine._position_manager, "replace_live_stop_order", return_value=True) as mock_replace:
        result = engine.run(db, executor)

    assert result == 1
    mock_replace.assert_called_once()
    candidate: StopCandidate = mock_replace.call_args[0][3]
    # Breakeven per SHORT = entry + buffer (100 + 0.3% = 100.30)
    assert candidate.reason == "btc_macro_protection_breakeven"
    assert candidate.price > Decimal("100")
    assert candidate.price < Decimal("101")
    assert executor.closed == []


def test_reversal_bearish_to_bullish_short_in_loss() -> None:
    # BEARISH → BULLISH con SHORT in perdita → chiusura immediata
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="100", stop="105", unrealized_pnl=-3.0)
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    with patch.object(engine._position_manager, "replace_live_stop_order") as mock_replace:
        result = engine.run(db, executor)

    assert result == 1
    mock_replace.assert_not_called()
    assert len(executor.closed) == 1
    assert executor.closed[0]["reason"] == "btc_macro_protection_loss_exit"


def test_reversal_bullish_to_bearish_long_in_profit() -> None:
    # BULLISH → BEARISH con LONG in profitto → SL aggiornato a breakeven
    engine = BtcMacroProtectionEngine(_settings())
    # BTC scende del 2%
    candles = _candles(10, base_close=100.0, last_close=98.0, last_volume=2000.0)

    pos = _position(side="long", entry="100", stop="92", unrealized_pnl=4.0)
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BULLISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    with patch.object(engine._position_manager, "replace_live_stop_order", return_value=True) as mock_replace:
        result = engine.run(db, executor)

    assert result == 1
    mock_replace.assert_called_once()
    candidate: StopCandidate = mock_replace.call_args[0][3]
    # Breakeven per LONG = entry - buffer (100 - 0.3% = 99.70)
    assert candidate.reason == "btc_macro_protection_breakeven"
    assert candidate.price < Decimal("100")
    assert candidate.price > Decimal("99")


def test_reversal_bullish_to_bearish_long_in_loss() -> None:
    # BULLISH → BEARISH con LONG in perdita → chiusura
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=98.0, last_volume=2000.0)

    pos = _position(side="long", entry="100", stop="92", unrealized_pnl=-2.0)
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BULLISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    with patch.object(engine._position_manager, "replace_live_stop_order") as mock_replace:
        result = engine.run(db, executor)

    assert result == 1
    mock_replace.assert_not_called()
    assert len(executor.closed) == 1


def test_no_degradation_if_trailing_already_better_short() -> None:
    # SHORT con trailing già più basso di breakeven → non aggiornare
    # entry=100, buffer=0.3% → new_sl=100.30
    # current_sl=102 → new_sl(100.30) < current_sl(102) → dovrebbe aggiornare
    # current_sl=99 (trailing già più basso) → new_sl(100.30) > current_sl(99)... aspetta

    # Per SHORT: lower SL = more protective.
    # current_sl = 99 (già sotto entry, trailing ottimo)
    # new_sl = 100.30 (breakeven, sopra entry)
    # 100.30 > 99 → new_sl NON è più basso del corrente → NON aggiornare ✓
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    # SL già a 99 (trailing l'ha mosso sotto entry per una short in profitto)
    pos = _position(side="short", entry="100", stop="99", unrealized_pnl=5.0)
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    with patch.object(engine._position_manager, "replace_live_stop_order") as mock_replace:
        result = engine.run(db, executor)

    # Nessun aggiornamento: il trailing è già meglio del breakeven
    assert result == 0
    mock_replace.assert_not_called()


def test_no_action_for_longs_on_bullish_reversal() -> None:
    # BULLISH reversal non deve toccare le posizioni LONG
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="long", entry="100", stop="92", unrealized_pnl=2.0)
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    with patch.object(engine._position_manager, "replace_live_stop_order") as mock_replace:
        result = engine.run(db, executor)

    assert result == 0
    mock_replace.assert_not_called()
    assert executor.closed == []


def test_same_trend_no_reversal() -> None:
    # BULLISH → BULLISH non è un reversal
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="100", stop="105", unrealized_pnl=2.0)
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BULLISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    with patch.object(engine._position_manager, "replace_live_stop_order") as mock_replace:
        result = engine.run(db, executor)

    assert result == 0
    mock_replace.assert_not_called()


def test_loss_exit_disabled_does_not_close() -> None:
    # SHORT in perdita ma loss_exit_enabled=False → nessuna chiusura
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="100", stop="105", unrealized_pnl=-3.0)
    market = _btc_market()

    db = FakeDb(
        settings={
            "btc_macro_protection": _config_setting(
                last_trend="BEARISH", lookback=3, loss_exit=False
            )
        },
        btc_market=market,
        candles=candles,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()

    result = engine.run(db, executor)
    assert result == 0
    assert executor.closed == []


def test_state_updated_after_run() -> None:
    # Verifica che _last_btc_trend venga aggiornato anche senza reversal
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    setting = _config_setting(last_trend="NEUTRAL", lookback=3)
    db = FakeDb(
        settings={"btc_macro_protection": setting},
        btc_market=_btc_market(),
        candles=candles,
    )
    engine.run(db, FakeExecutor())
    # Il valore di _last_btc_trend deve essere aggiornato a BULLISH
    assert setting.value["_last_btc_trend"] == "BULLISH"


def test_reversal_through_neutral_state_preserved() -> None:
    """F-01: BULLISH → NEUTRAL → BEARISH deve essere rilevato come reversal.

    Bug originale: `_last_btc_trend` veniva sovrascritto a NEUTRAL durante una
    candela neutra, perdendo il directional precedente. Il fix introduce
    `_last_directional_trend` che si aggiorna solo per trend non-NEUTRAL.
    """
    engine = BtcMacroProtectionEngine(_settings())

    # Stato iniziale: _last_directional_trend = BULLISH
    setting = _config_setting(last_trend="NEUTRAL", lookback=3)
    setting.value["_last_directional_trend"] = "BULLISH"
    market = _btc_market()

    # Ciclo 1: candela con delta > soglia ma volume insufficiente → NEUTRAL
    candles_neutral = _candles(10, base_close=100.0, last_close=102.0, last_volume=800.0)
    db1 = FakeDb(
        settings={"btc_macro_protection": setting},
        btc_market=market,
        candles=candles_neutral,
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    engine.run(db1, FakeExecutor())

    # NEUTRAL osservato non deve sovrascrivere il directional
    assert setting.value["_last_btc_trend"] == "NEUTRAL"
    assert setting.value["_last_directional_trend"] == "BULLISH"

    # Ciclo 2: trend BEARISH (delta -2%, volume sufficiente)
    candles_bearish = _candles(10, base_close=100.0, last_close=98.0, last_volume=2000.0)
    pos = _position(side="long", entry="100", stop="92", unrealized_pnl=4.0)
    db2 = FakeDb(
        settings={"btc_macro_protection": setting},
        btc_market=market,
        candles=candles_bearish,
        positions=[pos],
        markets_by_id={1: market, 2: _alt_market(2)},
    )

    with patch.object(
        engine._position_manager, "replace_live_stop_order", return_value=True
    ) as mock_replace:
        result = engine.run(db2, FakeExecutor())

    # Reversal BULLISH → BEARISH rilevato correttamente
    assert result == 1
    mock_replace.assert_called_once()
    assert setting.value["_last_directional_trend"] == "BEARISH"


def test_decision_log_emitted_on_reversal() -> None:
    # Verifica che venga emesso un DecisionLog btc_macro_reversal_detected
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    market = _btc_market()
    db = FakeDb(
        settings={"btc_macro_protection": _config_setting(last_trend="BEARISH", lookback=3)},
        btc_market=market,
        candles=candles,
        positions=[],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    engine.run(db, FakeExecutor())

    decision_logs = [x for x in db.added if isinstance(x, DecisionLog)]
    assert any(d.event_type == "btc_macro_reversal_detected" for d in decision_logs)


# ---------------------------------------------------------------------------
# Auto-reverse mode tests
# ---------------------------------------------------------------------------


def _reverse_config(
    last_trend: str = "BEARISH",
    cooldown_minutes: int = 240,
    max_per_day: int = 3,
    min_score: int = 50,
    max_age_minutes: int = 60,
    reverse_history: dict[str, Any] | None = None,
) -> AppSetting:
    return AppSetting(
        key="btc_macro_protection",
        value={
            "enabled": True,
            "timeframe": "5m",
            "lookback_candles": 3,
            "reversal_pct_threshold": 1.5,
            "volume_multiplier": 1.2,
            "breakeven_buffer_pct": 0.3,
            "loss_exit_enabled": True,
            "_last_btc_trend": last_trend,
            "_last_directional_trend": last_trend,
            "reverse_mode_enabled": True,
            "reverse_cooldown_minutes": cooldown_minutes,
            "reverse_max_per_day": max_per_day,
            "reverse_signal_flip_max_age_minutes": max_age_minutes,
            "reverse_signal_flip_min_score": min_score,
            "_reverse_history": reverse_history or {},
        },
    )


def _signal(
    *,
    direction: str = "LONG",
    score: int = 75,
    market_id: int = 2,
    signal_id: int = 500,
    created_at: datetime | None = None,
    status: str = "new",
    entry: str = "200",
    stop: str | None = "190",
) -> Signal:
    return Signal(
        id=signal_id,
        market_id=market_id,
        timeframe="5m",
        direction=direction,
        score=score,
        confidence_label="forte",
        entry_price=Decimal(entry),
        stop_loss=Decimal(stop) if stop else None,
        reason="test",
        strategy_breakdown={},
        status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )


def test_reverse_mode_triggers_on_combo_paper() -> None:
    """BEARISH→BULLISH + position SHORT paper + signal LONG recente → close + open opposite."""
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="200", stop="210", unrealized_pnl=-5.0, mode="paper")
    market = _btc_market()
    flip = _signal(direction="LONG", score=75, market_id=pos.market_id, signal_id=500)

    db = FakeDb(
        settings={"btc_macro_protection": _reverse_config(last_trend="BEARISH")},
        btc_market=market,
        candles=candles,
        positions=[pos],
        signals=[flip],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()
    result = engine.run(db, executor)

    assert result == 1
    assert len(executor.paper_closed) == 1
    assert executor.paper_closed[0]["reason"] == "btc_auto_reverse"
    assert len(executor.paper_opened) == 1
    assert executor.paper_opened[0].id == flip.id
    assert executor.closed == []

    decision_logs = [x for x in db.added if isinstance(x, DecisionLog)]
    triggered = [d for d in decision_logs if d.event_type == "btc_auto_reverse_triggered"]
    assert len(triggered) == 1
    assert triggered[0].data["from_side"] == "short"
    assert triggered[0].data["to_side"] == "long"
    assert triggered[0].data["mode"] == "paper"


def test_reverse_mode_triggers_on_combo_live() -> None:
    """BULLISH→BEARISH + position LONG live + signal SHORT recente → close live + open opposite."""
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=98.0, last_volume=2000.0)

    pos = _position(side="long", entry="200", stop="190", unrealized_pnl=-5.0, mode="live")
    market = _btc_market()
    flip = _signal(direction="SHORT", score=80, market_id=pos.market_id, signal_id=501)

    db = FakeDb(
        settings={"btc_macro_protection": _reverse_config(last_trend="BULLISH")},
        btc_market=market,
        candles=candles,
        positions=[pos],
        signals=[flip],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()
    result = engine.run(db, executor)

    assert result == 1
    assert len(executor.closed) == 1
    assert executor.closed[0]["reason"] == "btc_auto_reverse"
    assert len(executor.live_opened) == 1
    assert executor.live_opened[0].id == flip.id
    assert executor.paper_closed == []


def test_reverse_skipped_without_signal_flip() -> None:
    """Reversal + posizione opposta ma NESSUN signal flip → skipped, no close/open."""
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="200", mode="paper")
    market = _btc_market()

    db = FakeDb(
        settings={"btc_macro_protection": _reverse_config(last_trend="BEARISH")},
        btc_market=market,
        candles=candles,
        positions=[pos],
        signals=[],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()
    result = engine.run(db, executor)

    assert result == 0
    assert executor.paper_closed == []
    assert executor.paper_opened == []
    decision_logs = [x for x in db.added if isinstance(x, DecisionLog)]
    assert any(d.event_type == "btc_auto_reverse_skipped_no_signal_flip" for d in decision_logs)


def test_reverse_respects_cooldown() -> None:
    """Reverse recente (entro cooldown) → skipped."""
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="200", mode="paper")
    market = _btc_market()
    flip = _signal(direction="LONG", market_id=pos.market_id, signal_id=510)
    # Reverse fatto 10 minuti fa, cooldown 240 minuti → bloccato
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    history = {str(pos.market_id): [{"reversed_at": recent, "from_side": "long", "to_side": "short"}]}

    db = FakeDb(
        settings={
            "btc_macro_protection": _reverse_config(
                last_trend="BEARISH", reverse_history=history
            )
        },
        btc_market=market,
        candles=candles,
        positions=[pos],
        signals=[flip],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()
    result = engine.run(db, executor)

    assert result == 0
    assert executor.paper_opened == []
    assert any(
        d.event_type == "btc_auto_reverse_skipped_cooldown"
        for d in db.added
        if isinstance(d, DecisionLog)
    )


def test_reverse_respects_max_per_day() -> None:
    """3 reverse nelle ultime 24h con cap=3 → 4° skipped."""
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="200", mode="paper")
    market = _btc_market()
    flip = _signal(direction="LONG", market_id=pos.market_id, signal_id=520)
    now = datetime.now(timezone.utc)
    # 3 reverse nelle ultime 24h, ma il più recente è oltre il cooldown (>4h fa)
    # così il blocco scatta solo per max_per_day, non per cooldown.
    history = {
        str(pos.market_id): [
            {"reversed_at": (now - timedelta(hours=20)).isoformat(), "from_side": "long", "to_side": "short"},
            {"reversed_at": (now - timedelta(hours=15)).isoformat(), "from_side": "short", "to_side": "long"},
            {"reversed_at": (now - timedelta(hours=5)).isoformat(), "from_side": "long", "to_side": "short"},
        ]
    }

    db = FakeDb(
        settings={
            "btc_macro_protection": _reverse_config(
                last_trend="BEARISH", reverse_history=history, max_per_day=3, cooldown_minutes=60
            )
        },
        btc_market=market,
        candles=candles,
        positions=[pos],
        signals=[flip],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()
    result = engine.run(db, executor)

    assert result == 0
    assert executor.paper_opened == []
    assert any(
        d.event_type == "btc_auto_reverse_skipped_max_per_day"
        for d in db.added
        if isinstance(d, DecisionLog)
    )


def test_reverse_history_persisted_to_appsetting() -> None:
    """Dopo un reverse, _reverse_history nel setting deve contenere la nuova entry."""
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="short", entry="200", mode="paper")
    market = _btc_market()
    flip = _signal(direction="LONG", market_id=pos.market_id, signal_id=530)
    setting = _reverse_config(last_trend="BEARISH")

    db = FakeDb(
        settings={"btc_macro_protection": setting},
        btc_market=market,
        candles=candles,
        positions=[pos],
        signals=[flip],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    engine.run(db, FakeExecutor())

    history = setting.value.get("_reverse_history", {})
    market_key = str(pos.market_id)
    assert market_key in history
    assert len(history[market_key]) == 1
    assert history[market_key][0]["from_side"] == "short"
    assert history[market_key][0]["to_side"] == "long"


def test_reverse_skipped_when_position_already_aligned() -> None:
    """Reversal BULLISH + posizione LONG (già allineata col nuovo regime) → nessun reverse."""
    engine = BtcMacroProtectionEngine(_settings())
    candles = _candles(10, base_close=100.0, last_close=102.0, last_volume=2000.0)

    pos = _position(side="long", entry="200", mode="paper")
    market = _btc_market()
    flip = _signal(direction="SHORT", market_id=pos.market_id, signal_id=540)

    db = FakeDb(
        settings={"btc_macro_protection": _reverse_config(last_trend="BEARISH")},
        btc_market=market,
        candles=candles,
        positions=[pos],
        signals=[flip],
        markets_by_id={1: market, 2: _alt_market(2)},
    )
    executor = FakeExecutor()
    result = engine.run(db, executor)

    # La posizione LONG è già allineata al nuovo regime BULLISH: niente da fare.
    assert result == 0
    assert executor.paper_closed == []
    assert executor.paper_opened == []

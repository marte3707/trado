from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    AccountSnapshot,
    AppSetting,
    Base,
    LeaderboardCopyPaperPosition,
    LeaderboardTrader,
    LeaderboardTraderFill,
    LeaderboardTraderSnapshot,
    Market,
    Position,
    Signal,
)
from app.services import leaderboard_alpha as alpha


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):  # noqa: ANN001, ARG002
        return self

    def order_by(self, *args, **kwargs):  # noqa: ANN001, ARG002
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, fills):
        self._fills = fills
        self.added = []

    def query(self, model):  # noqa: ANN001
        assert model is LeaderboardTraderFill
        return _FakeQuery(self._fills)

    def add(self, row):  # noqa: ANN001
        self.added.append(row)


class _FakeCopyLiveTradingClient:
    def __init__(self) -> None:
        self.mids_max_age_seconds = None

    def all_mids(self, max_age_seconds=None):  # noqa: ANN001, ARG002
        self.mids_max_age_seconds = max_age_seconds
        return {"BTC": "100"}


class _FakeCopyLiveExecutor:
    def __init__(self) -> None:
        self.executed_signals: list[int] = []
        self.closed_positions: list[int] = []

    def execute_live(self, db, signal: Signal, trading_client=None):  # noqa: ANN001, ARG002
        self.executed_signals.append(signal.id)
        position = Position(
            market_id=signal.market_id,
            mode="live",
            side="long" if signal.direction == "LONG" else "short",
            entry_price=signal.entry_price,
            size=Decimal("0.4"),
            stop_loss=signal.stop_loss,
            status="open",
            opened_at=datetime(2026, 7, 9, tzinfo=UTC),
            metadata_json={"source_signal_id": str(signal.id)},
        )
        signal.status = "executed"
        db.add(position)
        db.flush()
        return position

    def close_live_position(self, db, position: Position, reason="manual", trading_client=None):  # noqa: ANN001, ARG002
        self.closed_positions.append(position.id)
        position.status = "closed"
        db.add(position)
        db.flush()


def _fill(
    idx: int,
    *,
    now: datetime,
    intent: str = "open",
    direction: str = "long",
    closed_pnl: Decimal | None = None,
    fee: Decimal = Decimal("0.05"),
    coin: str = "BTC",
) -> LeaderboardTraderFill:
    return LeaderboardTraderFill(
        trader_id=1,
        fill_key=f"fill-{idx}",
        coin=coin,
        side="buy" if direction == "long" else "sell",
        direction=direction,
        intent=intent,
        price=Decimal("100"),
        size=Decimal("1"),
        notional_usd=Decimal("100"),
        closed_pnl=closed_pnl,
        fee=fee,
        crossed=True,
        raw_data={},
        filled_at=now - timedelta(minutes=idx),
    )


def _sqlite_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def test_normalized_hyperliquid_fill_extracts_intent_and_notional() -> None:
    raw = {
        "time": 1_720_000_000_000,
        "coin": "ETH",
        "side": "B",
        "dir": "Open Long",
        "px": "2500.5",
        "sz": "0.4",
        "closedPnl": "0",
        "fee": "0.12",
        "crossed": True,
        "oid": 123,
        "hash": "0xabc",
    }

    payload = alpha._normalized_fill(raw)

    assert payload is not None
    assert payload["coin"] == "ETH"
    assert payload["side"] == "buy"
    assert payload["direction"] == "long"
    assert payload["intent"] == "open"
    assert payload["notional_usd"] == Decimal("1000.20")
    assert payload["filled_at"] == datetime.fromtimestamp(1_720_000_000, tz=UTC)


def test_select_top_leaderboard_traders_filters_week_volume_and_sorts_roi() -> None:
    rows = [
        {
            "ethAddress": "0x" + "1" * 40,
            "displayName": "A",
            "accountValue": "1000",
            "windowPerformances": [["week", {"roi": "0.20", "pnl": "20", "vlm": "100"}]],
        },
        {
            "ethAddress": "0x" + "2" * 40,
            "displayName": "B",
            "accountValue": "2000",
            "windowPerformances": [["week", {"roi": "0.50", "pnl": "50", "vlm": "1"}]],
        },
        {
            "ethAddress": "0x" + "3" * 40,
            "displayName": "No volume",
            "accountValue": "3000",
            "windowPerformances": [["week", {"roi": "9.00", "pnl": "90", "vlm": "0"}]],
        },
        {
            "ethAddress": "not-an-address",
            "windowPerformances": [["week", {"roi": "9.00", "pnl": "90", "vlm": "1000"}]],
        },
    ]

    selected = alpha.select_top_leaderboard_traders(rows, limit=20, window="week")

    assert [row["address"] for row in selected] == ["0x" + "2" * 40, "0x" + "1" * 40]
    assert [row["rank"] for row in selected] == [1, 2]
    assert selected[0]["roi"] == Decimal("0.50")
    assert selected[0]["volume"] == Decimal("1")


@pytest.mark.asyncio
async def test_sync_trader_skips_without_user_fills_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        called = False

        async def user_fills_by_time(self, *args, **kwargs):  # noqa: ANN001, ARG002
            self.called = True
            return []

    monkeypatch.setattr(
        alpha,
        "available_hyperliquid_info_request_capacity",
        lambda *args, **kwargs: 0,  # noqa: ARG005
    )
    trader = LeaderboardTrader(id=10, address="0x" + "a" * 40, enabled=True)
    client = Client()

    result = await alpha.sync_trader(object(), trader, client=client)

    assert result.status == "rate_limited"
    assert result.reason == "ip_weight_budget"
    assert result.fetched == 0
    assert result.inserted == 0
    assert trader.last_sync_status == "rate_limited"
    assert trader.last_sync_at is None
    assert client.called is False


def test_copy_paper_replay_closes_positions_and_is_idempotent() -> None:
    db = _sqlite_db()
    now = datetime(2026, 7, 8, tzinfo=UTC)
    try:
        trader = LeaderboardTrader(
            address="0x" + "b" * 40,
            label="copy candidate",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 1}},
        )
        db.add(trader)
        db.flush()
        db.add_all(
            [
                LeaderboardTraderFill(
                    trader_id=trader.id,
                    fill_key="open-1",
                    coin="BTC",
                    side="buy",
                    direction="long",
                    intent="open",
                    price=Decimal("100"),
                    size=Decimal("10"),
                    notional_usd=Decimal("1000"),
                    oid="order-open",
                    raw_data={},
                    filled_at=now,
                ),
                LeaderboardTraderFill(
                    trader_id=trader.id,
                    fill_key="open-duplicate",
                    coin="BTC",
                    side="buy",
                    direction="long",
                    intent="open",
                    price=Decimal("100.5"),
                    size=Decimal("1"),
                    notional_usd=Decimal("100.5"),
                    oid="order-open",
                    raw_data={},
                    filled_at=now + timedelta(seconds=1),
                ),
                LeaderboardTraderFill(
                    trader_id=trader.id,
                    fill_key="close-1",
                    coin="BTC",
                    side="sell",
                    direction="long",
                    intent="close",
                    price=Decimal("110"),
                    size=Decimal("10"),
                    notional_usd=Decimal("1100"),
                    oid="order-close",
                    raw_data={},
                    filled_at=now + timedelta(minutes=10),
                ),
            ]
        )
        db.commit()

        result = alpha.run_copy_paper_replay(db)
        db.commit()
        position = db.query(LeaderboardCopyPaperPosition).one()

        assert result["opened"] == 1
        assert result["closed"] == 1
        assert position.status == "closed"
        assert position.copy_notional_usd == Decimal("100")
        assert position.copy_size == Decimal("1.0000000000")
        assert position.gross_pnl_usdc == Decimal("10.0000000000")
        assert position.net_pnl_usdc == Decimal("9.905500000000000")
        assert position.pnl_pct == Decimal("0.09905500")

        second = alpha.run_copy_paper_replay(db)
        db.commit()

        assert second["fills_seen"] == 0
        assert second["opened"] == 0
        assert second["closed"] == 0
        assert db.query(LeaderboardCopyPaperPosition).count() == 1
    finally:
        db.close()


def _add_snapshot(
    db,
    trader: LeaderboardTrader,
    *,
    classification: str,
    quality_score: int,
) -> None:
    db.add(
        LeaderboardTraderSnapshot(
            trader_id=trader.id,
            lookback_days=30,
            fills_count=40,
            closing_fills_count=20,
            open_fills_count=20,
            net_pnl_usdc=Decimal("100"),
            closed_pnl_usdc=Decimal("110"),
            fees_usdc=Decimal("10"),
            volume_usdc=Decimal("10000"),
            win_rate=Decimal("0.60"),
            profit_factor=Decimal("2.0"),
            quality_score=quality_score,
            classification=classification,
            metrics={},
        )
    )


def _add_copy_position(
    db,
    trader: LeaderboardTrader,
    *,
    idx: int,
    now: datetime,
    status: str = "closed",
    net_pnl: Decimal = Decimal("1"),
    coin: str = "BTC",
) -> None:
    fill = LeaderboardTraderFill(
        trader_id=trader.id,
        fill_key=f"{trader.id}-open-{idx}",
        coin=coin,
        side="buy",
        direction="long",
        intent="open",
        price=Decimal("100"),
        size=Decimal("1"),
        notional_usd=Decimal("100"),
        oid=f"open-{trader.id}-{idx}",
        raw_data={},
        filled_at=now - timedelta(hours=idx + 1),
    )
    db.add(fill)
    db.flush()
    position = LeaderboardCopyPaperPosition(
        trader_id=trader.id,
        open_fill_id=fill.id,
        coin=coin,
        direction="long",
        status=status,
        source_open_order_id=fill.oid,
        entry_price=Decimal("100"),
        copy_size=Decimal("1"),
        copy_notional_usd=Decimal("100"),
        original_notional_usd=Decimal("100"),
        entry_fee_usdc=Decimal("0.045"),
        opened_at=fill.filled_at,
        metadata_json={},
    )
    if status == "closed":
        position.exit_price = Decimal("101") if net_pnl >= 0 else Decimal("99")
        position.exit_fee_usdc = Decimal("0.045")
        position.gross_pnl_usdc = net_pnl + Decimal("0.09")
        position.net_pnl_usdc = net_pnl
        position.pnl_pct = net_pnl / Decimal("100")
        position.closed_at = now - timedelta(hours=idx)
    db.add(position)


def test_copy_paper_close_fill_can_close_multiple_positions() -> None:
    db = _sqlite_db()
    now = datetime(2026, 7, 8, tzinfo=UTC)
    try:
        trader = LeaderboardTrader(
            address="0x" + "b" * 40,
            label="copy candidate",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 1}},
        )
        db.add(trader)
        db.flush()
        db.add_all(
            [
                LeaderboardTraderFill(
                    trader_id=trader.id,
                    fill_key="open-1",
                    coin="BTC",
                    side="buy",
                    direction="long",
                    intent="open",
                    price=Decimal("100"),
                    size=Decimal("1"),
                    notional_usd=Decimal("100"),
                    oid="open-1",
                    raw_data={},
                    filled_at=now,
                ),
                LeaderboardTraderFill(
                    trader_id=trader.id,
                    fill_key="open-2",
                    coin="BTC",
                    side="buy",
                    direction="long",
                    intent="open",
                    price=Decimal("101"),
                    size=Decimal("1"),
                    notional_usd=Decimal("101"),
                    oid="open-2",
                    raw_data={},
                    filled_at=now + timedelta(minutes=1),
                ),
                LeaderboardTraderFill(
                    trader_id=trader.id,
                    fill_key="close-large",
                    coin="BTC",
                    side="sell",
                    direction="long",
                    intent="close",
                    price=Decimal("110"),
                    size=Decimal("2.5"),
                    notional_usd=Decimal("275"),
                    oid="close-large",
                    raw_data={},
                    filled_at=now + timedelta(minutes=10),
                ),
            ]
        )
        db.commit()

        result = alpha.run_copy_paper_replay(db)
        db.commit()
        positions = db.query(LeaderboardCopyPaperPosition).order_by(LeaderboardCopyPaperPosition.id).all()

        assert result["opened"] == 2
        assert result["closed"] == 2
        assert [position.status for position in positions] == ["closed", "closed"]
        assert {position.source_close_order_id for position in positions} == {"close-large"}
    finally:
        db.close()


def test_copy_paper_reconciles_open_positions_past_cursor() -> None:
    db = _sqlite_db()
    now = datetime(2026, 7, 8, tzinfo=UTC)
    try:
        trader = LeaderboardTrader(
            address="0x" + "c" * 40,
            label="copy candidate",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 1}},
        )
        db.add(trader)
        db.flush()
        open_fill = LeaderboardTraderFill(
            trader_id=trader.id,
            fill_key="open-1",
            coin="ETH",
            side="buy",
            direction="long",
            intent="open",
            price=Decimal("100"),
            size=Decimal("1"),
            notional_usd=Decimal("100"),
            oid="open-1",
            raw_data={},
            filled_at=now,
        )
        close_fill = LeaderboardTraderFill(
            trader_id=trader.id,
            fill_key="close-1",
            coin="ETH",
            side="sell",
            direction="long",
            intent="close",
            price=Decimal("110"),
            size=Decimal("1"),
            notional_usd=Decimal("110"),
            oid="close-1",
            raw_data={},
            filled_at=now + timedelta(minutes=5),
        )
        db.add_all([open_fill, close_fill])
        db.flush()
        db.add(
            LeaderboardCopyPaperPosition(
                trader_id=trader.id,
                open_fill_id=open_fill.id,
                coin="ETH",
                direction="long",
                status="open",
                source_open_order_id=open_fill.oid,
                entry_price=Decimal("100"),
                copy_size=Decimal("1"),
                copy_notional_usd=Decimal("100"),
                original_notional_usd=Decimal("100"),
                entry_fee_usdc=Decimal("0.045"),
                opened_at=open_fill.filled_at,
                metadata_json={},
            )
        )
        db.add(
            AppSetting(
                key="leaderboard_alpha_state",
                value={
                    "last_copy_paper_fill_at": close_fill.filled_at.isoformat(),
                    "last_copy_paper_fill_id": close_fill.id,
                },
            )
        )
        db.commit()

        result = alpha.run_copy_paper_replay(db)
        db.commit()
        position = db.query(LeaderboardCopyPaperPosition).one()

        assert result["fills_seen"] == 0
        assert result["reconciled_closed"] == 1
        assert position.status == "closed"
        assert position.close_fill_id == close_fill.id
        assert position.close_reason == "source_close_reconcile"
    finally:
        db.close()


def test_copy_live_shadow_plan_selects_best_traders_and_limits_open_positions() -> None:
    db = _sqlite_db()
    now = datetime(2026, 7, 9, tzinfo=UTC)
    try:
        strong = LeaderboardTrader(
            address="0x" + "1" * 40,
            label="strong",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 1, "imported_at": now.isoformat()}},
        )
        weak_quality = LeaderboardTrader(
            address="0x" + "2" * 40,
            label="weak quality",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 2, "imported_at": now.isoformat()}},
        )
        watch = LeaderboardTrader(
            address="0x" + "3" * 40,
            label="watch",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 3, "imported_at": now.isoformat()}},
        )
        db.add_all([strong, weak_quality, watch])
        db.flush()
        db.add(
            Market(
                exchange="hyperliquid",
                symbol="BTC",
                base="BTC",
                quote="USDC",
                market_type="perp",
                active=True,
                metadata_json={"hl_coin": "BTC"},
            )
        )
        db.add(
            AccountSnapshot(
                timestamp=now,
                total_equity=Decimal("1000"),
                effective_account_value=Decimal("1000"),
                available_balance=Decimal("500"),
                margin_used=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                raw_data={},
            )
        )
        _add_snapshot(db, strong, classification="candidate", quality_score=95)
        _add_snapshot(db, weak_quality, classification="candidate", quality_score=70)
        _add_snapshot(db, watch, classification="watch", quality_score=95)
        for idx in range(22):
            _add_copy_position(
                db,
                strong,
                idx=idx,
                now=now,
                net_pnl=Decimal("1") if idx % 5 else Decimal("-0.25"),
            )
            _add_copy_position(db, weak_quality, idx=idx + 100, now=now, net_pnl=Decimal("1"))
            _add_copy_position(db, watch, idx=idx + 200, now=now, net_pnl=Decimal("1"))
        _add_copy_position(db, strong, idx=500, now=now, status="open", coin="BTC")
        _add_copy_position(db, strong, idx=501, now=now, status="open", coin="xyz:DRAM")
        db.commit()

        settings = {
            **alpha.DEFAULT_LEADERBOARD_ALPHA_SETTINGS,
            "copy_live_promotion_min_shortlist_days": 1,
        }
        state: dict = {}
        plan = alpha.build_copy_live_shadow_plan(
            db,
            settings=settings,
            now=now,
            state=state,
            update_promotion_state=True,
        )

        assert plan["orders_enabled"] is False
        assert plan["status"] == "shadow_ready"
        assert plan["readiness"]["ready"] is False
        assert "copy_live_disabled" in plan["readiness"]["blockers"]
        assert "copy_live_mode_shadow" in plan["readiness"]["blockers"]
        assert "live_order_dispatch_not_implemented" not in plan["readiness"]["blockers"]
        assert [row["trader_label"] for row in plan["selected_traders"]] == ["strong"]
        assert plan["selected_traders"][0]["promoted"] is True
        assert plan["selected_traders"][0]["promotion"]["shortlist_days"] == 1
        assert plan["summary"]["promoted_traders"] == 1
        assert plan["summary"]["planned_open_positions"] == 1
        assert plan["summary"]["estimated_open_notional_usd"] == "40"
        assert plan["summary"]["dry_run_would_submit"] == 1
        assert plan["dry_run_orders"][0]["status"] == "would_submit"
        assert plan["dry_run_orders"][0]["max_copy_live_open_positions"] == 3
        assert plan["dry_run_orders"][0]["max_live_open_positions"] >= 3
        assert plan["summary"]["skipped_open_positions"] == {"excluded_coin_prefix": 1}
        live_plan = alpha.build_copy_live_shadow_plan(
            db,
            settings={**settings, "copy_live_enabled": True, "copy_live_mode": "live"},
            now=now,
            state=state,
            update_promotion_state=False,
        )
        assert live_plan["orders_enabled"] is True
        assert live_plan["status"] == "live_ready"
        assert live_plan["readiness"]["ready"] is True
        assert live_plan["readiness"]["blockers"] == []
        rejected = {row["trader_label"]: row["reasons"] for row in plan["rejected_traders"]}
        assert "quality_below_live_min" in rejected["weak quality"]
        assert "classification_not_allowed" in rejected["watch"]
    finally:
        db.close()


def test_copy_live_dry_run_uses_effective_margin_and_risk_based_exposure() -> None:
    db = _sqlite_db()
    now = datetime(2026, 7, 9, tzinfo=UTC)
    try:
        trader = LeaderboardTrader(
            address="0x" + "1" * 40,
            label="strong",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 1, "imported_at": now.isoformat()}},
        )
        btc = Market(
            exchange="hyperliquid",
            symbol="BTC",
            base="BTC",
            quote="USDC",
            market_type="perp",
            active=True,
            metadata_json={"hl_coin": "BTC"},
        )
        eth = Market(
            exchange="hyperliquid",
            symbol="ETH",
            base="ETH",
            quote="USDC",
            market_type="perp",
            active=True,
            metadata_json={"hl_coin": "ETH"},
        )
        db.add_all([trader, btc, eth])
        db.flush()
        db.add(
            AppSetting(
                key="risk",
                value={
                    "max_leverage": 10,
                    "max_total_exposure_pct": 90,
                    "max_open_positions": 25,
                },
            )
        )
        db.add(
            AccountSnapshot(
                timestamp=now,
                total_equity=Decimal("100"),
                effective_account_value=Decimal("1000"),
                available_balance=Decimal("0"),
                margin_used=Decimal("900"),
                unrealized_pnl=Decimal("0"),
                raw_data={},
            )
        )
        db.add(
            Position(
                market_id=eth.id,
                mode="live",
                side="long",
                entry_price=Decimal("100"),
                size=Decimal("9"),
                stop_loss=Decimal("99"),
                status="open",
                opened_at=now,
                metadata_json={},
            )
        )
        _add_snapshot(db, trader, classification="candidate", quality_score=95)
        for idx in range(22):
            _add_copy_position(
                db,
                trader,
                idx=idx,
                now=now,
                net_pnl=Decimal("1") if idx % 5 else Decimal("-0.25"),
            )
        _add_copy_position(db, trader, idx=500, now=now, status="open", coin="BTC")
        db.commit()

        settings = {
            **alpha.DEFAULT_LEADERBOARD_ALPHA_SETTINGS,
            "copy_live_promotion_min_shortlist_days": 1,
        }
        plan = alpha.build_copy_live_shadow_plan(
            db,
            settings=settings,
            now=now,
            state={},
            update_promotion_state=True,
        )

        order = plan["dry_run_orders"][0]
        assert order["status"] == "would_submit"
        assert "available_margin_insufficient" not in order["reasons"]
        assert "max_total_exposure_exceeded" not in order["reasons"]
        assert order["available_margin_usd"] == "100"
        assert order["required_margin_usd"] == "4"
        assert order["existing_live_notional_usd"] == "900"
        assert order["existing_live_risk_usd"] == "9"
        assert order["risk_after_order_usd"] == "13"
        assert order["max_total_risk_usd"] == "900"
    finally:
        db.close()


def test_copy_live_activation_blockers_ignore_configuration_flags() -> None:
    plan = {
        "readiness": {
            "blockers": [
                "copy_live_disabled",
                "copy_live_mode_shadow",
                "available_margin_insufficient",
            ]
        }
    }

    assert alpha.copy_live_activation_blockers(plan) == ["available_margin_insufficient"]
    assert (
        alpha.copy_live_activation_blockers(
            {"readiness": {"blockers": ["copy_live_disabled", "copy_live_mode_shadow"]}}
        )
        == []
    )


def test_copy_live_candidate_requires_non_negative_recent_net_pnl() -> None:
    now = datetime(2026, 7, 9, tzinfo=UTC)
    trader = LeaderboardTrader(
        id=1,
        address="0x" + "1" * 40,
        label="stale winner",
        enabled=True,
        metadata_json={"leaderboard": {"rank": 1, "imported_at": now.isoformat()}},
    )
    snapshot = LeaderboardTraderSnapshot(
        trader_id=trader.id,
        lookback_days=30,
        fills_count=40,
        closing_fills_count=20,
        open_fills_count=20,
        net_pnl_usdc=Decimal("100"),
        closed_pnl_usdc=Decimal("110"),
        fees_usdc=Decimal("10"),
        volume_usdc=Decimal("10000"),
        win_rate=Decimal("0.60"),
        profit_factor=Decimal("2.0"),
        quality_score=95,
        classification="candidate",
        metrics={},
    )
    stats = {
        "closed_positions": 30,
        "open_positions": 1,
        "net_pnl_usdc": Decimal("20"),
        "avg_trade_pnl_usdc": Decimal("0.67"),
        "profit_factor": Decimal("2"),
        "win_rate_pct": Decimal("60"),
        "recent_closed_positions": 3,
        "recent_net_pnl_usdc": Decimal("-0.50"),
    }

    row = alpha._copy_live_candidate_row(
        trader,
        snapshot=snapshot,
        stats=stats,
        settings=alpha.DEFAULT_LEADERBOARD_ALPHA_SETTINGS,
        now=now,
    )

    assert row["eligible"] is False
    assert "recent_net_pnl_below_live_min" in row["reasons"]

    trader.metadata_json = {
        "leaderboard": {
            "rank": 1,
            "imported_at": (now - timedelta(hours=9)).isoformat(),
        }
    }
    stale_row = alpha._copy_live_candidate_row(
        trader,
        snapshot=snapshot,
        stats={**stats, "recent_net_pnl_usdc": Decimal("1")},
        settings=alpha.DEFAULT_LEADERBOARD_ALPHA_SETTINGS,
        now=now,
    )

    assert stale_row["eligible"] is False
    assert "leaderboard_rank_stale" in stale_row["reasons"]


def test_enable_copy_live_mode_blocks_when_readiness_has_hard_blockers() -> None:
    db = _sqlite_db()
    try:
        result = alpha.enable_copy_live_mode(db)

        assert result["status"] == "blocked"
        assert "no_dry_run_orders" in result["blockers"]
        assert "no_dry_run_would_submit" in result["blockers"]
        assert db.get(AppSetting, "leaderboard_alpha") is None
    finally:
        db.close()


def test_run_copy_live_execution_submits_once_and_links_position() -> None:
    db = _sqlite_db()
    now = datetime(2026, 7, 9, tzinfo=UTC)
    try:
        trader = LeaderboardTrader(
            address="0x" + "4" * 40,
            label="strong",
            enabled=True,
            metadata_json={"leaderboard": {"rank": 1, "imported_at": now.isoformat()}},
        )
        market = Market(
            exchange="hyperliquid",
            symbol="BTC",
            base="BTC",
            quote="USDC",
            market_type="perp",
            active=True,
            metadata_json={"hl_coin": "BTC"},
        )
        db.add_all([trader, market])
        db.flush()
        db.add(
            AppSetting(
                key="risk",
                value={
                    "min_signal_score": 70,
                    "min_risk_reward": 2,
                    "max_leverage": 10,
                    "max_total_exposure_pct": 95,
                    "max_open_positions": 25,
                },
            )
        )
        db.add(
            AccountSnapshot(
                timestamp=now,
                total_equity=Decimal("1000"),
                effective_account_value=Decimal("1000"),
                available_balance=Decimal("500"),
                margin_used=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                raw_data={},
            )
        )
        db.add(
            AppSetting(
                key=alpha.LEADERBOARD_ALPHA_STATE_KEY,
                value={
                    "copy_live_promotion": {
                        "traders": {
                            str(trader.id): {
                                "shortlist_dates": [now.date().isoformat()],
                                "first_seen_at": now.isoformat(),
                                "last_seen_at": now.isoformat(),
                            }
                        }
                    }
                },
            )
        )
        _add_snapshot(db, trader, classification="candidate", quality_score=95)
        for idx in range(22):
            _add_copy_position(
                db,
                trader,
                idx=idx,
                now=now,
                net_pnl=Decimal("1") if idx % 5 else Decimal("-0.25"),
            )
        _add_copy_position(db, trader, idx=500, now=now, status="open", coin="BTC")
        db.commit()

        settings = {
            **alpha.DEFAULT_LEADERBOARD_ALPHA_SETTINGS,
            "copy_live_enabled": True,
            "copy_live_mode": "live",
            "copy_live_promotion_min_shortlist_days": 1,
        }
        executor = _FakeCopyLiveExecutor()
        trading_client = _FakeCopyLiveTradingClient()

        result = alpha.run_copy_live_execution(
            db,
            settings=settings,
            now=now,
            trading_client=trading_client,
            executor=executor,
        )
        second = alpha.run_copy_live_execution(
            db,
            settings=settings,
            now=now,
            trading_client=_FakeCopyLiveTradingClient(),
            executor=executor,
        )

        live_positions = db.query(Position).filter(Position.mode == "live").all()
        signals = db.query(Signal).filter(Signal.timeframe == alpha.COPY_LIVE_SIGNAL_TIMEFRAME).all()
        metadata = live_positions[0].metadata_json["leaderboard_copy_live"]

        assert result["submitted"] == 1
        assert result["blocked"] == 0
        assert trading_client.mids_max_age_seconds == 60
        assert second["submitted"] == 0
        assert second["reason"] == "no_would_submit_orders"
        second_plan = alpha.build_copy_live_shadow_plan(
            db,
            settings=settings,
            now=now,
        )
        assert second_plan["status"] == "no_new_eligible_source"
        assert second_plan["reason"] == "all_eligible_sources_already_copied"
        assert second_plan["dry_run_orders"][0]["status"] == "blocked"
        assert "source_position_already_copied" in second_plan["dry_run_orders"][0]["reasons"]
        assert len(executor.executed_signals) == 1
        assert len(live_positions) == 1
        assert len(signals) == 1
        assert metadata["paper_position_id"] is not None
        assert metadata["trader_id"] == trader.id
        assert metadata["coin"] == "BTC"
    finally:
        db.close()


def test_compute_snapshot_classifies_profitable_sample_as_candidate() -> None:
    now = datetime(2026, 7, 6, tzinfo=UTC)
    fills = [
        _fill(1, now=now, intent="open", direction="long"),
        _fill(2, now=now, intent="open", direction="long"),
        _fill(3, now=now, intent="open", direction="short"),
        _fill(4, now=now, intent="open", direction="long"),
        _fill(5, now=now, intent="close", direction="long", closed_pnl=Decimal("12")),
        _fill(6, now=now, intent="close", direction="short", closed_pnl=Decimal("8")),
        _fill(7, now=now, intent="close", direction="long", closed_pnl=Decimal("6")),
        _fill(8, now=now, intent="close", direction="short", closed_pnl=Decimal("-2")),
    ]
    db = _FakeDb(fills)

    snapshot = alpha.compute_trader_snapshot(db, 1, now=now)

    assert db.added == [snapshot]
    assert snapshot.fills_count == 8
    assert snapshot.closing_fills_count == 4
    assert snapshot.net_pnl_usdc == Decimal("23.60")
    assert snapshot.win_rate == Decimal("0.75")
    assert snapshot.profit_factor == Decimal("13")
    assert snapshot.classification == "candidate"
    assert snapshot.quality_score >= 70


def test_compute_snapshot_quarantines_negative_sample() -> None:
    now = datetime(2026, 7, 6, tzinfo=UTC)
    fills = [
        _fill(1, now=now, intent="open", direction="long"),
        _fill(2, now=now, intent="open", direction="long"),
        _fill(3, now=now, intent="open", direction="short"),
        _fill(4, now=now, intent="open", direction="short"),
        _fill(5, now=now, intent="close", direction="long", closed_pnl=Decimal("-6")),
        _fill(6, now=now, intent="close", direction="short", closed_pnl=Decimal("-4")),
        _fill(7, now=now, intent="close", direction="long", closed_pnl=Decimal("1")),
        _fill(8, now=now, intent="close", direction="short", closed_pnl=Decimal("-3")),
    ]
    db = _FakeDb(fills)

    snapshot = alpha.compute_trader_snapshot(db, 1, now=now)

    assert snapshot.net_pnl_usdc == Decimal("-12.40")
    assert snapshot.classification == "quarantine"
    assert snapshot.quality_score < 55

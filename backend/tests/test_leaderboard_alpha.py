from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    LeaderboardCopyPaperPosition,
    LeaderboardTrader,
    LeaderboardTraderFill,
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

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes
from app.db.models import Base, Candle, DecisionLog, Indicator, Market, Position, Signal
from app.db.session import get_db


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


def _client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _market() -> Market:
    return Market(
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USD",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "BTC"},
    )


def test_signal_snapshot_returns_source_context_in_chronological_order() -> None:
    db = _session()
    market = _market()
    db.add(market)
    db.flush()

    base = datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)
    source_time = base + timedelta(minutes=10)
    for index in range(6):
        open_time = base + timedelta(minutes=5 * index)
        db.add(
            Candle(
                market_id=market.id,
                timeframe="5m",
                open_time=open_time,
                open=Decimal("100") + index,
                high=Decimal("101") + index,
                low=Decimal("99") + index,
                close=Decimal("100.5") + index,
                volume=Decimal("10") + index,
            )
        )

    db.add(
        Indicator(
            market_id=market.id,
            timeframe="5m",
            timestamp=source_time,
            values={
                "rsi": {"rsi": 34, "_direction": "LONG"},
                "macd": {"histogram": "0.12", "_direction": "LONG"},
            },
        )
    )
    signal = Signal(
        market_id=market.id,
        timeframe="5m",
        direction="LONG",
        score=72,
        confidence_label="buono",
        entry_price=Decimal("102"),
        stop_loss=Decimal("99"),
        take_profit_1=Decimal("105"),
        take_profit_2=Decimal("108"),
        risk_reward=Decimal("2.0"),
        reason="RSI recovery",
        strategy_breakdown={
            "rsi": {
                "direction": "LONG",
                "confidence": 70,
                "weight": 10,
                "contribution": 7,
                "reason": "RSI recovery",
                "indicator_values": {"rsi": 34},
            },
            "_score": {"long_raw": 7, "short_raw": 0, "all_weight": 10, "voting_weight": 10},
        },
        source_candle_open_time=source_time,
        status="new",
    )
    db.add(signal)
    db.flush()
    db.add(
        Position(
            market_id=market.id,
            mode="paper",
            side="long",
            entry_price=Decimal("102"),
            size=Decimal("1"),
            stop_loss=Decimal("99"),
            status="open",
            opened_at=source_time,
            metadata_json={"source_signal_id": signal.id},
        )
    )
    db.add(
        DecisionLog(
            event_type="signal_generated",
            market_id=market.id,
            signal_id=signal.id,
            message="Signal generated",
            data={"score": 72},
        )
    )
    db.commit()

    response = _client(db).get(f"/api/signals/{signal.id}/snapshot")
    assert response.status_code == 200
    payload = response.json()
    assert payload["signal"]["id"] == signal.id
    assert payload["source_candle_open_time"].startswith("2026-06-09T10:10:00")
    assert [row["open_time"] for row in payload["candles"]] == sorted(row["open_time"] for row in payload["candles"])
    assert any(row["open_time"].startswith("2026-06-09T10:10:00") for row in payload["candles"])
    assert payload["indicators"][0]["indicator_name"] == "macd"
    assert payload["indicators"][1]["indicator_name"] == "rsi"
    assert payload["positions"][0]["metadata_json"]["source_signal_id"] == signal.id
    assert payload["decision_logs"][0]["event_type"] == "signal_generated"


def test_signal_snapshot_handles_missing_context_and_404() -> None:
    db = _session()
    market = _market()
    db.add(market)
    db.flush()
    signal = Signal(
        market_id=market.id,
        timeframe="5m",
        direction="SHORT",
        score=61,
        confidence_label="medio",
        entry_price=Decimal("100"),
        stop_loss=Decimal("103"),
        take_profit_1=Decimal("97"),
        take_profit_2=Decimal("94"),
        risk_reward=Decimal("2.0"),
        reason="Breakdown",
        strategy_breakdown={},
        source_candle_open_time=None,
        status="new",
    )
    db.add(signal)
    db.commit()

    client = _client(db)
    response = client.get(f"/api/signals/{signal.id}/snapshot")
    assert response.status_code == 200
    payload = response.json()
    assert payload["candles"] == []
    assert payload["indicators"] == []
    assert payload["positions"] == []
    assert payload["decision_logs"] == []

    missing = client.get("/api/signals/999999/snapshot")
    assert missing.status_code == 404

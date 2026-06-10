from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes
from app.db.models import Base, Market, Order, Position
from app.db.session import get_db


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


def _client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _position(
    market: Market,
    idx: int,
    *,
    mode: str = "paper",
    status: str = "closed",
    closed_at: datetime | None,
) -> Position:
    opened_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=idx)
    return Position(
        market_id=market.id,
        mode=mode,
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("95"),
        status=status,
        opened_at=opened_at,
        closed_at=closed_at,
        realized_pnl=Decimal("1") if status == "closed" else None,
        metadata_json={"source_timeframe": "5m", "initial_size": "1"},
    )


def test_position_history_returns_full_history_without_realtime_limit() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    market = Market(
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USD",
        market_type="perp",
        active=True,
        metadata_json={},
    )
    db.add(market)
    db.flush()
    base_closed = datetime(2026, 2, 1, tzinfo=UTC)
    positions = [
        _position(market, idx, closed_at=base_closed + timedelta(minutes=idx))
        for idx in range(routes.POSITION_HISTORY_LIMIT + 10)
    ]
    db.add_all(positions)
    db.flush()
    db.add(
        Order(
            position_id=positions[0].id,
            mode="paper",
            order_type="stop_loss",
            side="sell",
            price=Decimal("95"),
            size=Decimal("1"),
            status="filled",
            reduce_only=True,
            fee=Decimal("0.01"),
        )
    )
    db.commit()

    client = _client(db)
    realtime = client.get("/api/positions")
    assert realtime.status_code == 200
    assert len(realtime.json()) == routes.POSITION_HISTORY_LIMIT

    history = client.get("/api/positions/history?period=all&mode=paper&limit=5000")
    assert history.status_code == 200
    payload = history.json()
    assert payload["total"] == routes.POSITION_HISTORY_LIMIT + 10
    assert len(payload["positions"]) == routes.POSITION_HISTORY_LIMIT + 10
    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["order_type"] == "stop_loss"


def test_position_history_filters_mode_market_type_and_limit() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    perp = Market(
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USD",
        market_type="perp",
        active=True,
        metadata_json={},
    )
    spot = Market(
        exchange="hyperliquid",
        symbol="UBTC/USDC",
        base="UBTC",
        quote="USDC",
        market_type="spot",
        active=True,
        metadata_json={},
    )
    db.add_all([perp, spot])
    db.flush()
    closed_at = datetime.now(UTC)
    db.add_all(
        [
            _position(perp, 1, mode="paper", closed_at=closed_at),
            _position(perp, 2, mode="live", closed_at=closed_at),
            _position(spot, 3, mode="paper", closed_at=closed_at),
        ]
    )
    db.commit()

    client = _client(db)
    response = client.get("/api/positions/history?period=30d&mode=paper&market_type=perp&limit=1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["positions"]) == 1
    assert payload["positions"][0]["mode"] == "paper"
    assert payload["positions"][0]["market"]["market_type"] == "perp"

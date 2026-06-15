from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes
from app.db.models import Base, Market, Signal
from app.db.session import get_db
from app.notifications.telegram import TelegramNotifier


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
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "BTC"},
    )


def _signal(market_id: int) -> Signal:
    return Signal(
        market_id=market_id,
        timeframe="1h",
        direction="LONG",
        score=80,
        confidence_label="high",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit_1=Decimal("105"),
        take_profit_2=Decimal("110"),
        risk_reward=Decimal("2.0"),
        reason="test",
        strategy_breakdown={},
        status="new",
    )


def test_ignore_signal_marks_status_and_triggers_telegram_cleanup(monkeypatch) -> None:  # noqa: ANN001
    db = _session()
    market = _market()
    db.add(market)
    db.flush()
    signal = _signal(market.id)
    db.add(signal)
    db.commit()

    calls: list[int] = []

    async def fake_delete(self, db_session: Session, signal_id: int) -> dict:  # noqa: ANN001
        assert db_session is db
        calls.append(signal_id)
        return {"status": "deleted"}

    monkeypatch.setattr(TelegramNotifier, "delete_signal_message", fake_delete)

    response = _client(db).post(f"/api/signals/{signal.id}/ignore")

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert db.get(Signal, signal.id).status == "ignored"
    assert calls == [signal.id]


def test_cleanup_ignored_signals_endpoint_delegates_to_notifier(monkeypatch) -> None:  # noqa: ANN001
    db = _session()

    async def fake_cleanup(self, db_session: Session) -> dict:  # noqa: ANN001
        assert db_session is db
        return {"status": "completed", "deleted": 3}

    monkeypatch.setattr(TelegramNotifier, "cleanup_ignored_signal_messages", fake_cleanup)

    response = _client(db).post("/api/telegram/cleanup-ignored-signals")

    assert response.status_code == 200
    assert response.json() == {"status": "completed", "deleted": 3}

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
from app.api.routers.analytics import _beta, _pearson
from app.db.init_db import MONITORING_MARKETS, seed_monitoring_markets
from app.db.models import Base, Candle, Market
from app.db.session import get_db


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _market(db: Session, symbol: str, *, monitoring_only: bool = False) -> Market:
    metadata = {"hl_coin": symbol}
    if monitoring_only:
        metadata["monitoring_only"] = True
    market = Market(
        exchange="hyperliquid",
        symbol=symbol,
        base=symbol,
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json=metadata,
    )
    db.add(market)
    db.flush()
    return market


def _add_candles(db: Session, market: Market, closes: list[float], *, start: datetime, timeframe: str = "1h") -> None:
    for i, close in enumerate(closes):
        value = Decimal(str(close))
        db.add(
            Candle(
                market_id=market.id,
                timeframe=timeframe,
                open_time=start + timedelta(hours=i),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=Decimal("1"),
            )
        )
    db.flush()


# ── Helper statistici ──────────────────────────────────────────────────────


def test_pearson_perfect_and_anti_correlation() -> None:
    assert _pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0
    assert _pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0


def test_pearson_fail_soft() -> None:
    assert _pearson([1.0], [2.0]) is None          # < 2 campioni
    assert _pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None  # varianza nulla
    assert _pearson([1.0, 2.0], [1.0, 2.0, 3.0]) is None       # lunghezze diverse


def test_beta_scaling() -> None:
    # dependent = 2 * independent → beta = 2.0
    assert _beta([0.02, 0.04, 0.06], [0.01, 0.02, 0.03]) == 2.0
    assert _beta([0.01, 0.02, 0.03], [0.01, 0.02, 0.03]) == 1.0
    assert _beta([0.01, 0.02], [0.0, 0.0]) is None  # varianza independent nulla


# ── Endpoint /macro/correlation ─────────────────────────────────────────────


def test_macro_correlation_returns_based() -> None:
    db = _session()
    start = datetime(2026, 6, 1, tzinfo=UTC)
    xyz = _market(db, "XYZ100", monitoring_only=True)
    btc = _market(db, "BTC")
    # rendimenti XYZ = [0.01, 0.02, 0.03]; BTC = [0.02, 0.04, 0.06] = 2× XYZ
    _add_candles(db, xyz, [100.0, 101.0, 103.02, 106.1106], start=start)
    _add_candles(db, btc, [100.0, 102.0, 106.08, 112.4448], start=start)
    db.commit()

    resp = _client(db).get("/api/macro/correlation?base=XYZ100&ref=BTC&timeframe=1h")
    assert resp.status_code == 200
    body = resp.json()
    assert body["samples"] == 3
    assert body["correlation"] == 1.0
    assert body["beta"] == 2.0  # quanto si muove BTC per unità di XYZ100
    assert body["base_last_close"] == 106.1106
    assert body["ref_last_close"] == 112.4448


def test_macro_correlation_missing_market_404() -> None:
    db = _session()
    _market(db, "BTC")
    db.commit()
    resp = _client(db).get("/api/macro/correlation?base=XYZ100&ref=BTC")
    assert resp.status_code == 404


def test_macro_correlation_insufficient_overlap_is_null() -> None:
    db = _session()
    start = datetime(2026, 6, 1, tzinfo=UTC)
    xyz = _market(db, "XYZ100", monitoring_only=True)
    btc = _market(db, "BTC")
    # un solo timestamp in comune (t1) → 0 rendimenti calcolabili
    _add_candles(db, xyz, [100.0, 101.0], start=start)
    _add_candles(db, btc, [102.0, 103.0], start=start + timedelta(hours=1))
    db.commit()

    body = _client(db).get("/api/macro/correlation?base=XYZ100&ref=BTC").json()
    assert body["samples"] == 0
    assert body["correlation"] is None
    assert body["beta"] is None


# ── Seed monitor-only ────────────────────────────────────────────────────────


def test_seed_monitoring_markets_idempotent() -> None:
    db = _session()
    seed_monitoring_markets(db)
    seed_monitoring_markets(db)  # secondo giro: nessun duplicato
    db.commit()

    rows = db.query(Market).filter(Market.symbol == "XYZ100").all()
    assert len(rows) == 1
    market = rows[0]
    assert market.active is True
    assert market.metadata_json.get("monitoring_only") is True
    assert market.metadata_json.get("hl_coin") == "xyz:XYZ100"
    assert any(entry["symbol"] == "XYZ100" for entry in MONITORING_MARKETS)


def test_monitoring_guard_predicate() -> None:
    # Replica il guard usato dal worker: i market monitor-only saltano
    # generazione segnali/posizioni, gli altri no.
    db = _session()
    xyz = _market(db, "XYZ100", monitoring_only=True)
    btc = _market(db, "BTC")

    assert (xyz.metadata_json or {}).get("monitoring_only")
    assert not (btc.metadata_json or {}).get("monitoring_only")

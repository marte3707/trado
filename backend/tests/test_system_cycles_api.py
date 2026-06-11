from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes
from app.db.models import AppSetting, Base, DecisionLog
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


def _log(event_type: str, timestamp: datetime) -> DecisionLog:
    return DecisionLog(
        timestamp=timestamp,
        event_type=event_type,
        message=event_type.replace("_", " "),
        data={},
    )


def test_logs_excludes_system_cycles_by_default() -> None:
    db = _session()
    now = datetime.now(UTC)
    db.add_all(
        [
            _log("fast_risk_cycle", now),
            _log("fast_risk_cycle_failed", now + timedelta(seconds=1)),
            _log("worker_cycle", now + timedelta(seconds=2)),
            _log("signal_generated", now + timedelta(seconds=3)),
        ]
    )
    db.commit()
    client = _client(db)

    default_events = [row["event_type"] for row in client.get("/api/logs").json()]
    included_events = [
        row["event_type"] for row in client.get("/api/logs?include_system_cycles=true").json()
    ]
    filtered_events = [
        row["event_type"] for row in client.get("/api/logs?event_type=fast_risk_cycle").json()
    ]

    assert default_events == ["signal_generated"]
    assert "fast_risk_cycle" in included_events
    assert "fast_risk_cycle_failed" in included_events
    assert filtered_events == ["fast_risk_cycle"]


def test_system_cycles_marks_service_failed_after_failure_threshold() -> None:
    db = _session()
    now = datetime.now(UTC)
    db.add(
        AppSetting(
            key=routes.SYSTEM_CYCLE_STATUS_SETTING_KEY,
            value={
                "cycles": {
                    "fast_risk_cycle": {
                        "id": "fast_risk_cycle",
                        "label": "Fast Risk Cycle",
                        "status": "ok",
                        "message": "Fast risk cycle completed",
                        "last_seen_at": now.isoformat(),
                        "last_ok_at": now.isoformat(),
                        "consecutive_failures": 0,
                        "expected_interval_seconds": 15,
                        "data": {},
                    }
                }
            },
        )
    )
    db.add_all(
        [
            _log("fast_risk_cycle_failed", now - timedelta(minutes=1)),
            _log("fast_risk_cycle_failed", now - timedelta(minutes=2)),
            _log("fast_risk_cycle_failed", now - timedelta(minutes=3)),
        ]
    )
    db.commit()
    client = _client(db)

    payload = client.get("/api/analytics/system-cycles").json()
    fast_risk = next(item for item in payload["services"] if item["id"] == "fast_risk_cycle")

    assert payload["status"] == "degraded"
    assert fast_risk["status"] == "failed"
    assert fast_risk["state"] == "failing"
    assert fast_risk["failure_count"] == fast_risk["failure_threshold"] == 3

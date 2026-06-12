from typing import Any

import pytest

from app.core.config import Settings
from app.core.enums import OperationMode
from app.db.models import AppSetting, DecisionLog
from app.worker import fast_risk as worker


class FastRiskDb:
    def __init__(self, *, mode: OperationMode, live_enabled: bool) -> None:
        self.settings = {
            "operation_mode": AppSetting(
                key="operation_mode",
                value={"mode": mode.value, "enable_live_trading": live_enabled},
            )
        }
        self.added: list[Any] = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def get(self, model, key):
        if model is AppSetting:
            return self.settings.get(key)
        return None

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


class FakePositionManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.audit_calls = 0

    def audit_live_positions_against_liquidation(self, _db):
        self.audit_calls += 1
        if self.fail:
            raise RuntimeError("liquidation guard exploded")
        return {"checked": 1, "failed": 0, "skipped": 0}


class FakeExecutor:
    def __init__(self, settings: Settings, position_manager: FakePositionManager) -> None:
        self.settings = settings
        self.position_manager = position_manager


@pytest.mark.asyncio
async def test_fast_risk_skips_when_live_not_enabled(monkeypatch) -> None:
    db = FastRiskDb(mode=OperationMode.SIGNAL_ONLY, live_enabled=False)
    manager = FakePositionManager()
    executor = FakeExecutor(Settings(), manager)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)

    result = await worker.run_fast_risk_once(executor, worker._FastRiskState(), now=100)

    assert result["status"] == "skipped"
    assert result["reason"] == "live_not_enabled"
    assert manager.audit_calls == 0
    assert db.committed == 1
    status_settings = [
        row for row in db.added if isinstance(row, AppSetting) and row.key == worker.SYSTEM_CYCLE_STATUS_SETTING_KEY
    ]
    assert status_settings
    assert status_settings[-1].value["cycles"]["fast_risk_cycle"]["status"] == "skipped"
    assert db.closed is True


@pytest.mark.asyncio
async def test_fast_risk_live_mode_runs_due_actions(monkeypatch) -> None:
    db = FastRiskDb(mode=OperationMode.AUTO_TRADE, live_enabled=True)
    manager = FakePositionManager()
    executor = FakeExecutor(
        Settings(
            fast_risk_liquidation_interval_seconds=60,
            fast_risk_reconcile_interval_seconds=15,
            fast_risk_uar_interval_seconds=30,
        ),
        manager,
    )
    calls: dict[str, int] = {"reconcile": 0, "uar": 0}

    def reconcile(_db):
        calls["reconcile"] += 1
        return 2

    def uar(_db, _executor):
        calls["uar"] += 1
        return {"status": "ok"}

    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "reconcile_live_positions", reconcile)
    monkeypatch.setattr(worker, "audit_unified_account_ratio_guard", uar)
    state = worker._FastRiskState()

    first = await worker.run_fast_risk_once(executor, state, now=100)
    second = await worker.run_fast_risk_once(executor, state, now=110)

    assert first["status"] == "ok"
    assert first["actions"]["liquidation_guard"]["checked"] == 1
    assert first["actions"]["live_reconciled"] == 2
    assert first["actions"]["unified_account_ratio"] == {"status": "ok"}
    assert second["status"] == "ok"
    assert "liquidation_guard" not in second["actions"]
    assert "live_reconciled" not in second["actions"]
    assert "unified_account_ratio" not in second["actions"]
    assert manager.audit_calls == 1
    assert calls == {"reconcile": 1, "uar": 1}
    assert not any(isinstance(row, DecisionLog) and row.event_type == "fast_risk_cycle" for row in db.added)
    status_settings = [
        row for row in db.added if isinstance(row, AppSetting) and row.key == worker.SYSTEM_CYCLE_STATUS_SETTING_KEY
    ]
    assert status_settings
    assert status_settings[-1].value["cycles"]["fast_risk_cycle"]["status"] == "ok"


@pytest.mark.asyncio
async def test_fast_risk_liquidation_guard_uses_own_interval(monkeypatch) -> None:
    db = FastRiskDb(mode=OperationMode.AUTO_TRADE, live_enabled=True)
    manager = FakePositionManager()
    executor = FakeExecutor(
        Settings(
            fast_risk_liquidation_interval_seconds=60,
            fast_risk_reconcile_interval_seconds=999,
            fast_risk_uar_interval_seconds=999,
        ),
        manager,
    )
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "reconcile_live_positions", lambda _db: 0)
    monkeypatch.setattr(worker, "audit_unified_account_ratio_guard", lambda _db, _executor: {})
    state = worker._FastRiskState()

    await worker.run_fast_risk_once(executor, state, now=100)
    middle = await worker.run_fast_risk_once(executor, state, now=150)
    due = await worker.run_fast_risk_once(executor, state, now=160)

    assert "liquidation_guard" not in middle["actions"]
    assert due["actions"]["liquidation_guard"]["checked"] == 1
    assert manager.audit_calls == 2


@pytest.mark.asyncio
async def test_fast_risk_failure_rolls_back_and_logs(monkeypatch) -> None:
    db = FastRiskDb(mode=OperationMode.AUTO_TRADE, live_enabled=True)
    manager = FakePositionManager(fail=True)
    executor = FakeExecutor(
        Settings(
            fast_risk_liquidation_interval_seconds=60,
            fast_risk_reconcile_interval_seconds=999,
            fast_risk_uar_interval_seconds=999,
        ),
        manager,
    )
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)

    result = await worker.run_fast_risk_once(executor, worker._FastRiskState(), now=100)

    assert result["status"] == "failed"
    assert result["type"] == "RuntimeError"
    assert db.rolled_back == 1
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "fast_risk_cycle_failed"
        for row in db.added
    )

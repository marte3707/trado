from app.api.routes import reset_calibration_baseline
from app.db.models import AppSetting, DecisionLog


class FakeQuery:
    def __init__(self, store: dict, model) -> None:
        self.store = store
        self.model = model
        self._filter_key: str | None = None

    def filter(self, condition) -> "FakeQuery":
        self._filter_key = condition
        return self

    def delete(self) -> int:
        if self._filter_key is None:
            return 0
        existed = self._filter_key in self.store
        if existed:
            del self.store[self._filter_key]
        return 1 if existed else 0


class FakeDb:
    def __init__(self) -> None:
        self.app_settings: dict[str, AppSetting] = {
            "auto_calibration_baseline": AppSetting(
                key="auto_calibration_baseline",
                value={"risk": {"min_signal_score": 60}},
            ),
        }
        self.decision_logs: list[DecisionLog] = []
        self.committed = False

    def query(self, model):
        store = self.app_settings if model is AppSetting else {}
        return FakeQuery(store, model)

    def add(self, row) -> None:
        if isinstance(row, DecisionLog):
            self.decision_logs.append(row)
        elif isinstance(row, AppSetting):
            self.app_settings[row.key] = row

    def commit(self) -> None:
        self.committed = True


def _monkeypatch_filter(monkeypatch) -> None:
    # FakeQuery.filter usa l'oggetto BinaryExpression come chiave;
    # gli iniettiamo un comportamento che riconosce confronti su AppSetting.key.
    def fake_filter(self, condition):
        # Per i nostri test la condition è sempre AppSetting.key == 'auto_calibration_baseline'.
        self._filter_key = "auto_calibration_baseline"
        return self

    monkeypatch.setattr(FakeQuery, "filter", fake_filter)


def test_reset_baseline_deletes_setting_and_logs_event(monkeypatch) -> None:
    _monkeypatch_filter(monkeypatch)
    db = FakeDb()
    assert "auto_calibration_baseline" in db.app_settings

    result = reset_calibration_baseline(db=db)  # type: ignore[arg-type]

    assert result == {"ok": True, "deleted": 1}
    assert "auto_calibration_baseline" not in db.app_settings
    assert len(db.decision_logs) == 1
    log = db.decision_logs[0]
    assert log.event_type == "auto_calibration_baseline_reset"
    assert log.data == {"deleted": 1}
    assert db.committed is True


def test_reset_baseline_idempotent_when_already_absent(monkeypatch) -> None:
    _monkeypatch_filter(monkeypatch)
    db = FakeDb()
    db.app_settings.clear()

    result = reset_calibration_baseline(db=db)  # type: ignore[arg-type]

    assert result == {"ok": True, "deleted": 0}
    assert db.decision_logs[0].data == {"deleted": 0}

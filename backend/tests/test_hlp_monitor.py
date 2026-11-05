import asyncio
from datetime import UTC, datetime

from app.db.models import AppSetting
from app.services.hlp_monitor import (
    HLP_VAULT_ADDRESS,
    STATE_KEY,
    check_hlp_vault,
    vault_metrics,
    yield_band,
)
from app.worker.scheduler import HLP_MONITOR_INTERVAL_SECONDS, PeriodicScheduler

DAY_MS = 86_400_000
T0 = 1_790_000_000_000
AV = 100_000_000.0


class _FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, AppSetting] = {}

    def get(self, model, key):
        assert model is AppSetting
        return self.rows.get(key)

    def add(self, row: AppSetting) -> None:
        self.rows[row.key] = row


def _details(pnl_path: list[float], av: float = AV, follower: dict | None = None) -> dict:
    """One sample per day for the month series; the week series reuses the last 8 samples."""
    month_av = [[T0 + i * DAY_MS, str(av)] for i in range(len(pnl_path))]
    month_pnl = [[T0 + i * DAY_MS, str(p)] for i, p in enumerate(pnl_path)]
    week = {"accountValueHistory": month_av[-8:], "pnlHistory": month_pnl[-8:]}
    return {
        "name": "Hyperliquidity Provider (HLP)",
        "portfolio": [["month", {"accountValueHistory": month_av, "pnlHistory": month_pnl}], ["week", week]],
        "followerState": follower,
    }


def _steady(annual: float, days: int = 31) -> list[float]:
    daily = AV * annual / 365
    return [daily * i for i in range(days)]


class _Fixture:
    def __init__(self, details: dict, send_ok: bool = True) -> None:
        self.details = details
        self.send_ok = send_ok
        self.sent: list[str] = []
        self.calls: list[tuple[str, str | None]] = []

    async def fetch(self, vault: str, user: str | None) -> dict:
        self.calls.append((vault, user))
        return self.details

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.send_ok


def _check(db, fx: _Fixture, user: str | None = None) -> dict:
    return asyncio.run(check_hlp_vault(db, fetch_vault_details=fx.fetch, send_message=fx.send,
                                       user_address=user, now=datetime(2026, 11, 5, tzinfo=UTC)))


def test_metrics_steady_yield() -> None:
    m = vault_metrics(_details(_steady(0.12)))
    assert m is not None
    assert abs(m["return_30d"] - 0.12 * 30 / 365) < 1e-9
    assert 0.12 < m["annualised_30d"] < 0.13
    assert m["drawdown_from_30d_peak"] == 0
    assert m["worst_step_7d"] == 0


def test_metrics_flow_neutral_against_deposits() -> None:
    d = _details(_steady(0.0))
    # a large deposit doubles account value but adds no PnL: return must stay 0
    d["portfolio"][0][1]["accountValueHistory"][-1][1] = str(2 * AV)
    assert vault_metrics(d)["return_30d"] == 0


def test_metrics_missing_history() -> None:
    assert vault_metrics({"portfolio": []}) is None
    assert vault_metrics(_details([0.0])) is None


def test_yield_bands() -> None:
    assert yield_band(-0.01, 0.10) == "negative"
    assert yield_band(0.05, 0.10) == "low"
    assert yield_band(0.10, 0.10) == "good"


def test_first_check_records_band_without_alert() -> None:
    db, fx = _FakeDb(), _Fixture(_details(_steady(0.03)))
    result = _check(db, fx, user="0xabc")
    assert result["alerts"] == [] and result["yield_band"] == "low"
    assert fx.calls == [(HLP_VAULT_ADDRESS, "0xabc")]


def test_yield_band_changes_alert_once_each() -> None:
    db, fx = _FakeDb(), _Fixture(_details(_steady(0.03)))
    _check(db, fx)
    fx.details = _details(_steady(-0.02))
    assert _check(db, fx)["alerts"] == ["yield_negative"]
    assert _check(db, fx)["alerts"] == []
    fx.details = _details(_steady(0.15))
    assert _check(db, fx)["alerts"] == ["yield_good"]
    assert len(fx.sent) == 2 and "tornato buono" in fx.sent[1]


def test_incident_on_drawdown_then_rearm() -> None:
    db = _FakeDb()
    path = _steady(0.10)
    path[-1] = path[-2] - 0.04 * AV            # 4% loss in the last sample
    fx = _Fixture(_details(path))
    result = _check(db, fx)
    assert "incident" in result["alerts"]
    assert "possibile incidente" in fx.sent[0]
    assert _check(db, fx)["alerts"] == []       # no repeat while still in drawdown

    fx.details = _details(_steady(0.10))
    assert _check(db, fx).get("incident_rearmed") is True
    assert db.get(AppSetting, STATE_KEY).value["incident_armed"] is True


def test_incident_on_single_step_loss() -> None:
    path = _steady(0.20)
    path[-3] = path[-4] - 0.015 * AV            # -1.5% step, then recovery above the old peak
    path[-2] = path[-1] = path[-4] + 0.001 * AV
    fx = _Fixture(_details(path))
    assert "incident" in _check(_FakeDb(), fx)["alerts"]


def test_follower_position_in_message() -> None:
    lock = int(datetime(2030, 1, 1, tzinfo=UTC).timestamp() * 1000)
    path = _steady(0.10)
    path[-1] = path[-2] - 0.05 * AV
    fx = _Fixture(_details(path, follower={"vaultEquity": "1234.5", "allTimePnl": "34.5", "lockupUntil": lock}))
    _check(_FakeDb(), fx, user="0xabc")
    assert "1,234.50 USDC" in fx.sent[0] and "+34.50" in fx.sent[0] and "prelievo possibile" in fx.sent[0]


def test_failed_send_keeps_state_for_retry() -> None:
    db, fx = _FakeDb(), _Fixture(_details(_steady(0.03)))
    _check(db, fx)
    fx.details, fx.send_ok = _details(_steady(-0.05)), False
    assert _check(db, fx)["alerts"] == []
    assert db.get(AppSetting, STATE_KEY).value["yield_band"] == "low"
    fx.send_ok = True
    assert _check(db, fx)["alerts"] == ["yield_negative"]


def test_user_address_setting_overrides_trading_account() -> None:
    db = _FakeDb()
    db.add(AppSetting(key="hlp_monitor", value={"user_address": "0xsavings"}))
    fx = _Fixture(_details(_steady(0.05)))
    _check(db, fx, user="0xtrading")
    assert fx.calls[0][1] == "0xsavings"


def test_disabled_and_no_history() -> None:
    db = _FakeDb()
    db.add(AppSetting(key="hlp_monitor", value={"enabled": False}))
    assert _check(db, _Fixture(_details(_steady(0.1))))["status"] == "disabled"
    assert _check(_FakeDb(), _Fixture({"portfolio": []}))["status"] == "skipped"


def test_scheduler_hlp_every_six_hours() -> None:
    now = 1_700_000_000.0
    s = PeriodicScheduler(now)
    assert s.due_hlp_monitor(now) is False
    first = s.next_hlp_monitor
    assert s.due_hlp_monitor(first) is True
    assert s.due_hlp_monitor(first + HLP_MONITOR_INTERVAL_SECONDS - 1) is False
    assert s.due_hlp_monitor(first + HLP_MONITOR_INTERVAL_SECONDS) is True

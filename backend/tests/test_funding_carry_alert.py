import asyncio
from datetime import UTC, datetime

from app.db.models import AppSetting
from app.services.funding_carry_alert import (
    STATE_KEY,
    check_funding_carry_regime,
    trailing_apr,
)
from app.worker.scheduler import (
    DAILY_JOB_STARTUP_DELAY_SECONDS,
    FUNDING_CARRY_ALERT_INTERVAL_SECONDS,
    PeriodicScheduler,
)

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


class _FakeDb:
    """Minimal session: the alert only uses get/add on AppSetting rows."""

    def __init__(self) -> None:
        self.rows: dict[str, AppSetting] = {}

    def get(self, model, key):
        assert model is AppSetting
        return self.rows.get(key)

    def add(self, row: AppSetting) -> None:
        self.rows[row.key] = row

    def commit(self) -> None:
        pass


def _db() -> _FakeDb:
    return _FakeDb()


def _hourly(apr: float) -> list[dict]:
    rate = apr / (24 * 365)
    return [{"coin": "X", "fundingRate": str(rate), "time": i} for i in range(168)]


class _Fixture:
    def __init__(self, aprs: dict[str, float], send_ok: bool = True) -> None:
        self.aprs = aprs
        self.send_ok = send_ok
        self.sent: list[str] = []
        self.calls: list[tuple[str, int, int]] = []

    async def fetch(self, coin: str, start_ms: int, end_ms: int) -> list[dict]:
        self.calls.append((coin, start_ms, end_ms))
        return _hourly(self.aprs[coin]) if coin in self.aprs else []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.send_ok


def _check(db, fx: _Fixture) -> dict:
    result = asyncio.run(
        check_funding_carry_regime(db, fetch_funding_history=fx.fetch, send_message=fx.send, now=NOW)
    )
    db.commit()
    return result


def test_trailing_apr_annualises_hourly_mean_and_skips_bad_rows() -> None:
    rows = _hourly(0.20) + [{"fundingRate": "bad"}, {}]
    assert abs(trailing_apr(rows) - 0.20) < 1e-9
    assert trailing_apr([]) is None


def test_no_alert_when_funding_is_low() -> None:
    db = _db()
    fx = _Fixture({"BTC": 0.06, "ETH": 0.08})
    result = _check(db, fx)
    assert result["triggered"] is False
    assert fx.sent == []
    assert db.get(AppSetting, STATE_KEY).value["armed"] is True
    # 7-day window ending now
    coin, start_ms, end_ms = fx.calls[0]
    assert coin == "BTC" and end_ms - start_ms == 7 * 86_400_000


def test_requires_every_watched_coin_above_entry() -> None:
    db = _db()
    fx = _Fixture({"BTC": 0.30, "ETH": 0.12})
    assert _check(db, fx)["triggered"] is False
    assert fx.sent == []


def test_alerts_once_then_rearms_after_funding_cools() -> None:
    db = _db()
    fx = _Fixture({"BTC": 0.22, "ETH": 0.18})
    first = _check(db, fx)
    assert first["alert_sent"] is True
    assert len(fx.sent) == 1 and "BTC: 22.0%" in fx.sent[0]

    second = _check(db, fx)
    assert second["alert_sent"] is False and second["armed"] is False
    assert len(fx.sent) == 1

    fx.aprs = {"BTC": 0.12, "ETH": 0.11}          # between rearm (10%) and entry (15%)
    assert _check(db, fx)["armed"] is False

    fx.aprs = {"BTC": 0.05, "ETH": 0.06}
    assert _check(db, fx)["rearmed"] is True

    fx.aprs = {"BTC": 0.25, "ETH": 0.20}
    assert _check(db, fx)["alert_sent"] is True
    assert len(fx.sent) == 2


def test_failed_send_keeps_alert_armed() -> None:
    db = _db()
    fx = _Fixture({"BTC": 0.30, "ETH": 0.30}, send_ok=False)
    result = _check(db, fx)
    assert result["status"] == "send_failed"
    assert db.get(AppSetting, STATE_KEY).value["armed"] is True


def test_disabled_and_missing_data() -> None:
    db = _db()
    db.add(AppSetting(key="funding_carry_alert", value={"enabled": False}))
    db.commit()
    assert _check(db, _Fixture({"BTC": 0.3, "ETH": 0.3}))["status"] == "disabled"

    db2 = _db()
    result = _check(db2, _Fixture({"BTC": 0.3}))
    assert result == {"status": "skipped", "reason": "no_funding_data:ETH"}


def test_custom_coins_and_thresholds_from_settings() -> None:
    db = _db()
    db.add(AppSetting(key="funding_carry_alert", value={"coins": ["SOL"], "entry_apr": 0.25}))
    db.commit()
    fx = _Fixture({"SOL": 0.26})
    assert _check(db, fx)["alert_sent"] is True
    assert [c for c, _, _ in fx.calls] == ["SOL"]


def test_scheduler_runs_funding_alert_every_six_hours() -> None:
    now = 1_700_000_000.0
    s = PeriodicScheduler(now)
    assert s.due_funding_carry_alert(now) is False
    first = now + DAILY_JOB_STARTUP_DELAY_SECONDS
    assert s.due_funding_carry_alert(first) is True
    assert s.due_funding_carry_alert(first + FUNDING_CARRY_ALERT_INTERVAL_SECONDS - 1) is False
    assert s.due_funding_carry_alert(first + FUNDING_CARRY_ALERT_INTERVAL_SECONDS) is True

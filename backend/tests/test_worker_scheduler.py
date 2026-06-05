from app.worker.scheduler import (
    DATA_RETENTION_INTERVAL_SECONDS,
    ERROR_NOTIFY_COOLDOWN_SECONDS,
    RECAP_INTERVAL_SECONDS,
    PeriodicScheduler,
)


def test_recap_not_due_before_interval() -> None:
    now = 1000.0
    s = PeriodicScheduler(now)
    assert s.due_recap(now) is False
    assert s.due_recap(now + RECAP_INTERVAL_SECONDS - 1) is False


def test_recap_due_and_reschedules() -> None:
    now = 1000.0
    s = PeriodicScheduler(now)
    fire = now + RECAP_INTERVAL_SECONDS
    assert s.due_recap(fire) is True
    # Subito dopo non è più dovuto: è stato riprogrammato.
    assert s.due_recap(fire) is False
    assert s.due_recap(fire + RECAP_INTERVAL_SECONDS) is True


def test_guards_due_on_first_tick() -> None:
    # I guardrail partono da 0.0 → dovuti già al primo tick (comportamento storico).
    s = PeriodicScheduler(1000.0)
    assert s.due_live_liquidation_guard(1000.0) is True
    assert s.due_unified_account_ratio_guard(1000.0) is True
    # Riprogrammati: non più dovuti subito.
    assert s.due_live_liquidation_guard(1000.0) is False
    assert s.due_unified_account_ratio_guard(1000.0) is False


def test_error_notify_respects_cooldown() -> None:
    # now realistico (epoch) ≫ cooldown: come a runtime con time.time().
    now = 1_700_000_000.0
    s = PeriodicScheduler(now)
    # last_error_notify parte a 0.0 → la prima notifica è permessa.
    assert s.due_error_notify(now) is True
    assert s.due_error_notify(now) is False
    assert s.due_error_notify(now + ERROR_NOTIFY_COOLDOWN_SECONDS) is True


def test_data_retention_interval() -> None:
    now = 5000.0
    s = PeriodicScheduler(now)
    assert s.due_data_retention(now) is False
    assert s.due_data_retention(now + DATA_RETENTION_INTERVAL_SECONDS) is True


def test_daily_recap_reschedules_to_future() -> None:
    s = PeriodicScheduler(1000.0)
    first = s.next_daily_recap
    # Forziamo il tempo oltre la prossima esecuzione giornaliera.
    assert s.due_daily_recap(first) is True
    assert s.next_daily_recap > first
    assert s.due_daily_recap(first) is False

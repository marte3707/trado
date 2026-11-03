from app.watchdog.main import ServiceWatchdog, WatchdogConfig, service_health


def _health(*, worker: bool = True, marketdata: bool = True) -> dict:
    return {
        "status": "ok",
        "worker": {"healthy": worker},
        "market_data": {"healthy": marketdata},
    }


def _config() -> WatchdogConfig:
    return WatchdogConfig(
        failure_threshold=3,
        flap_threshold=4,
        flap_window_seconds=600,
        restart_cooldown_seconds=300,
    )


def test_unreachable_backend_does_not_guess_downstream_health() -> None:
    assert service_health(None) == {
        "backend": False,
        "worker": None,
        "marketdata": None,
    }


def test_marketdata_restarts_after_three_consecutive_failures() -> None:
    restarted: list[str] = []
    watchdog = ServiceWatchdog(_config(), restart=lambda service: not restarted.append(service))

    assert watchdog.observe(_health(marketdata=False), now=0) == []
    assert watchdog.observe(_health(marketdata=False), now=30) == []
    assert watchdog.observe(_health(marketdata=False), now=60) == ["marketdata"]
    assert restarted == ["marketdata"]
    assert watchdog.failures["marketdata"] == 0


def test_recovery_resets_consecutive_failure_count() -> None:
    watchdog = ServiceWatchdog(_config(), restart=lambda _service: True)

    watchdog.observe(_health(worker=False), now=0)
    watchdog.observe(_health(worker=False), now=30)
    watchdog.observe(_health(worker=True), now=60)
    watchdog.observe(_health(worker=False), now=90)

    assert watchdog.failures["worker"] == 1


def test_marketdata_flapping_restarts_within_rolling_window() -> None:
    restarted: list[str] = []
    watchdog = ServiceWatchdog(_config(), restart=lambda service: not restarted.append(service))

    for now, healthy in (
        (0, False),
        (30, True),
        (60, False),
        (90, True),
        (120, False),
        (150, True),
    ):
        assert watchdog.observe(_health(marketdata=healthy), now=now) == []

    assert watchdog.observe(_health(marketdata=False), now=180) == ["marketdata"]
    assert restarted == ["marketdata"]
    assert list(watchdog.recent_unhealthy_samples["marketdata"]) == []


def test_old_unhealthy_samples_expire_from_flap_window() -> None:
    restarted: list[str] = []
    watchdog = ServiceWatchdog(_config(), restart=lambda service: not restarted.append(service))

    for now in (0, 30, 60):
        watchdog.observe(_health(marketdata=False), now=now)
        watchdog.observe(_health(marketdata=True), now=now + 1)

    assert watchdog.observe(_health(marketdata=False), now=700) == []
    assert restarted == []
    assert list(watchdog.recent_unhealthy_samples["marketdata"]) == [700]


def test_restart_cooldown_prevents_recovery_loop() -> None:
    restarted: list[str] = []
    watchdog = ServiceWatchdog(_config(), restart=lambda service: not restarted.append(service))

    for now in (0, 30, 60):
        watchdog.observe(_health(worker=False), now=now)
    for now in (90, 120, 150):
        watchdog.observe(_health(worker=False), now=now)

    assert restarted == ["worker"]


def test_backend_failure_only_restarts_backend() -> None:
    restarted: list[str] = []
    watchdog = ServiceWatchdog(_config(), restart=lambda service: not restarted.append(service))

    for now in (0, 30, 60):
        watchdog.observe(None, now=now)

    assert restarted == ["backend"]
    assert watchdog.failures["worker"] == 0
    assert watchdog.failures["marketdata"] == 0

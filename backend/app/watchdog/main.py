"""Recover unhealthy local services that are still alive at process level.

launchd already restarts crashed processes. This watchdog covers the different
case where a process is running but `/api/health` reports a stale worker or an
unready market-data stream. It never changes trading settings or database data.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("trado.watchdog")
MONITORED_SERVICES = ("backend", "worker", "marketdata")
SERVICE_LABELS = {service: f"com.trado.{service}" for service in MONITORED_SERVICES}


@dataclass(frozen=True)
class WatchdogConfig:
    health_url: str = "http://127.0.0.1:8000/api/health"
    interval_seconds: float = 30.0
    request_timeout_seconds: float = 5.0
    failure_threshold: int = 3
    flap_threshold: int = 4
    flap_window_seconds: float = 600.0
    restart_cooldown_seconds: float = 300.0
    pause_file: Path = Path(__file__).resolve().parents[3] / ".local" / "watchdog.pause"

    @classmethod
    def from_env(cls) -> WatchdogConfig:
        return cls(
            health_url=os.getenv("TRADO_WATCHDOG_HEALTH_URL", cls.health_url),
            interval_seconds=max(
                5.0, float(os.getenv("TRADO_WATCHDOG_INTERVAL_SECONDS", "30"))
            ),
            request_timeout_seconds=max(
                1.0, float(os.getenv("TRADO_WATCHDOG_REQUEST_TIMEOUT_SECONDS", "5"))
            ),
            failure_threshold=max(
                2, int(os.getenv("TRADO_WATCHDOG_FAILURE_THRESHOLD", "3"))
            ),
            flap_threshold=max(
                2, int(os.getenv("TRADO_WATCHDOG_FLAP_THRESHOLD", "4"))
            ),
            flap_window_seconds=max(
                60.0, float(os.getenv("TRADO_WATCHDOG_FLAP_WINDOW_SECONDS", "600"))
            ),
            restart_cooldown_seconds=max(
                60.0, float(os.getenv("TRADO_WATCHDOG_RESTART_COOLDOWN_SECONDS", "300"))
            ),
            pause_file=Path(
                os.getenv("TRADO_WATCHDOG_PAUSE_FILE", str(cls.pause_file))
            ),
        )


def fetch_health(url: str, *, timeout: float) -> dict[str, Any] | None:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("health probe failed: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


def service_health(payload: dict[str, Any] | None) -> dict[str, bool | None]:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return {"backend": False, "worker": None, "marketdata": None}
    worker = payload.get("worker")
    market_data = payload.get("market_data")
    return {
        "backend": True,
        "worker": isinstance(worker, dict) and worker.get("healthy") is True,
        "marketdata": (
            isinstance(market_data, dict) and market_data.get("healthy") is True
        ),
    }


def restart_launchd_service(service: str) -> bool:
    label = SERVICE_LABELS[service]
    target = f"gui/{os.getuid()}/{label}"
    try:
        result = subprocess.run(  # noqa: S603
            ["/bin/launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.error("restart failed service=%s error=%s", service, exc)
        return False
    if result.returncode != 0:
        LOGGER.error(
            "restart failed service=%s code=%s stderr=%s",
            service,
            result.returncode,
            result.stderr.strip()[:500],
        )
        return False
    LOGGER.warning("service restarted service=%s target=%s", service, target)
    return True


@dataclass
class ServiceWatchdog:
    config: WatchdogConfig
    restart: Callable[[str], bool] = restart_launchd_service
    failures: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(MONITORED_SERVICES, 0)
    )
    recent_unhealthy_samples: dict[str, deque[float]] = field(
        default_factory=lambda: {
            service: deque() for service in MONITORED_SERVICES
        }
    )
    last_restart_attempt: dict[str, float] = field(default_factory=dict)

    def observe(
        self,
        payload: dict[str, Any] | None,
        *,
        now: float,
    ) -> list[str]:
        restarted: list[str] = []
        for service, healthy in service_health(payload).items():
            if healthy is None:
                continue
            recent_unhealthy = self.recent_unhealthy_samples[service]
            cutoff = now - self.config.flap_window_seconds
            while recent_unhealthy and recent_unhealthy[0] < cutoff:
                recent_unhealthy.popleft()
            if healthy:
                if self.failures[service]:
                    LOGGER.info("service recovered service=%s", service)
                self.failures[service] = 0
                continue
            self.failures[service] += 1
            recent_unhealthy.append(now)
            flapping = len(recent_unhealthy) >= self.config.flap_threshold
            LOGGER.warning(
                "service unhealthy service=%s consecutive_failures=%s threshold=%s "
                "recent_unhealthy=%s flap_threshold=%s flap_window=%ss",
                service,
                self.failures[service],
                self.config.failure_threshold,
                len(recent_unhealthy),
                self.config.flap_threshold,
                self.config.flap_window_seconds,
            )
            if (
                self.failures[service] < self.config.failure_threshold
                and not flapping
            ):
                continue
            last_attempt = self.last_restart_attempt.get(service)
            if (
                last_attempt is not None
                and now - last_attempt < self.config.restart_cooldown_seconds
            ):
                continue
            self.last_restart_attempt[service] = now
            if self.restart(service):
                restarted.append(service)
                self.failures[service] = 0
                recent_unhealthy.clear()
        return restarted

    def reset_failures(self) -> None:
        self.failures = dict.fromkeys(MONITORED_SERVICES, 0)
        self.recent_unhealthy_samples = {
            service: deque() for service in MONITORED_SERVICES
        }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = WatchdogConfig.from_env()
    watchdog = ServiceWatchdog(config)
    LOGGER.info(
        "watchdog started interval=%ss threshold=%s flap_threshold=%s "
        "flap_window=%ss cooldown=%ss pause_file=%s",
        config.interval_seconds,
        config.failure_threshold,
        config.flap_threshold,
        config.flap_window_seconds,
        config.restart_cooldown_seconds,
        config.pause_file,
    )
    paused = False
    while True:
        if config.pause_file.exists():
            if not paused:
                LOGGER.warning("watchdog paused pause_file=%s", config.pause_file)
            paused = True
            watchdog.reset_failures()
        else:
            if paused:
                LOGGER.info("watchdog resumed")
            paused = False
            payload = fetch_health(
                config.health_url,
                timeout=config.request_timeout_seconds,
            )
            watchdog.observe(payload, now=time.monotonic())
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()

"""Health check, stato/azioni servizi launchd, diagnostica cicli di sistema."""
import os
import shlex
import subprocess
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path as FilePath
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.api.routers.common import (
    _coerce_utc_datetime,
    _decision_log_count_since,
    _iso_or_none,
    _latest_decision_log,
    _log_and_redact,
    _max_datetime,
)
from app.core.config import get_settings
from app.core.runtime_settings import (
    effective_live_execution_settings,
    effective_live_trading_enabled,
    effective_operation_mode,
)
from app.db.models import (
    AppSetting,
    DecisionLog,
)
from app.db.session import get_db
from app.worker.scheduler import (
    DATA_RETENTION_INTERVAL_SECONDS,
    DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
    LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS,
    UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
)

_logger = structlog.get_logger()

router = APIRouter()


SYSTEM_CYCLE_STATUS_SETTING_KEY = "system_cycle_status"
WORKER_CYCLE_INTERVAL_SECONDS = 60
SYSTEM_CYCLE_STALE_FACTOR = 3
SYSTEM_CYCLE_MIN_STALE_GRACE_SECONDS = 120
SYSTEM_CYCLE_EVENTS = frozenset(
    {
        "worker_cycle",
        "worker_cycle_failed",
        "fast_risk_cycle",
        "fast_risk_cycle_failed",
        "live_liquidation_guard_cycle",
        "live_liquidation_guard_cycle_failed",
        "unified_account_ratio_guard_cycle",
        "unified_account_ratio_guard_cycle_failed",
        "data_retention_cycle",
        "data_retention_cycle_failed",
        "diagnostic_report_cycle",
        "diagnostic_report_cycle_failed",
    }
)
@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    mode = effective_operation_mode(db, settings)
    enable_live_trading = effective_live_trading_enabled(db, settings)
    live_settings = effective_live_execution_settings(db, settings)
    checks = []
    if not settings.hyperliquid_account_address:
        checks.append("HYPERLIQUID_ACCOUNT_ADDRESS missing")
    if not settings.hyperliquid_private_key:
        checks.append("HYPERLIQUID_PRIVATE_KEY missing")
    if settings.hyperliquid_network == "mainnet" and not live_settings["enable_mainnet_live_trading"]:
        checks.append("mainnet live trading not enabled")
    return {
        "status": "ok",
        "mode": mode.value,
        "network": settings.hyperliquid_network,
        "enable_live_trading": enable_live_trading,
        "live_trading_allowed": mode.value == "AUTO_TRADE" and enable_live_trading and not checks,
        "live_readiness": {
            "account_address_configured": bool(settings.hyperliquid_account_address),
            "private_key_configured": bool(settings.hyperliquid_private_key),
            "mainnet_live_enabled": bool(live_settings["enable_mainnet_live_trading"]),
            "checks": checks,
        },
        "worker": _worker_status(db),
        "market_data": _market_data_status(db),
        "bot": _bot_status(),
    }


# Worker tick cadence is 60s (worker/main.py). Beyond ~3x interval we treat the
# worker as unhealthy — gives one missed cycle of grace before alarming.
WORKER_HEALTHY_THRESHOLD_SECONDS = 180
SYSTEM_SERVICES = {
    "backend": {
        "label": "com.trado.backend",
        "plist": FilePath.home() / "Library/LaunchAgents/com.trado.backend.plist",
        "error_log": FilePath.home() / "Library/Logs/trado-backend.err.log",
    },
    "worker": {
        "label": "com.trado.worker",
        "plist": FilePath.home() / "Library/LaunchAgents/com.trado.worker.plist",
        "error_log": FilePath.home() / "Library/Logs/trado-worker.err.log",
    },
    "marketdata": {
        "label": "com.trado.marketdata",
        "plist": FilePath.home() / "Library/LaunchAgents/com.trado.marketdata.plist",
        "error_log": FilePath.home() / "Library/Logs/trado-marketdata.err.log",
    },
    "bot": {
        "label": "com.trado.bot",
        "plist": FilePath.home() / "Library/LaunchAgents/com.trado.bot.plist",
        "error_log": FilePath.home() / "Library/Logs/trado-bot.err.log",
    },
}
SYSTEM_ACTIONS = {"start", "stop", "reboot"}


def _worker_status(db: Session) -> dict:
    # Heartbeat is written at the start of each tick (independent of cycle
    # duration), so a long first tick after restart still surfaces as alive.
    heartbeat = db.get(AppSetting, "worker_heartbeat")
    heartbeat_value = heartbeat.value if heartbeat and isinstance(heartbeat.value, dict) else {}
    heartbeat_ts = heartbeat_value.get("timestamp") if isinstance(heartbeat_value, dict) else None
    last_heartbeat_at: datetime | None = None
    if isinstance(heartbeat_ts, str):
        try:
            last_heartbeat_at = datetime.fromisoformat(heartbeat_ts)
        except ValueError:
            last_heartbeat_at = None
    last_cycle = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "worker_cycle")
        .order_by(DecisionLog.timestamp.desc())
        .first()
    )
    last_error = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "worker_cycle_failed")
        .order_by(DecisionLog.timestamp.desc())
        .first()
    )
    now = datetime.now(UTC)
    last_run_at = last_heartbeat_at or (last_cycle.timestamp if last_cycle else None)
    seconds_since = (now - last_run_at).total_seconds() if last_run_at else None
    return {
        "last_run_at": last_run_at.isoformat() if last_run_at else None,
        "seconds_since_last_run": seconds_since,
        "healthy": seconds_since is not None and seconds_since < WORKER_HEALTHY_THRESHOLD_SECONDS,
        "phase": heartbeat_value.get("phase") if isinstance(heartbeat_value, dict) else None,
        "last_cycle_at": last_cycle.timestamp.isoformat() if last_cycle else None,
        "last_cycle": dict(last_cycle.data) if last_cycle and last_cycle.data else None,
        "last_error_at": last_error.timestamp.isoformat() if last_error else None,
        "last_error_message": (last_error.data or {}).get("error") if last_error else None,
    }


def _launchd_status(service: str) -> dict[str, Any]:
    label = str(SYSTEM_SERVICES[service]["label"])
    target = f"gui/{os.getuid()}/{label}"
    running = False
    pid: int | None = None
    try:
        result = subprocess.run(  # noqa: S603
            ["/bin/launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"running": False, "pid": None, "label": label, "source": "launchctl_unavailable"}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                try:
                    pid = int(stripped.removeprefix("pid = ").strip())
                except ValueError:
                    pid = None
            elif stripped.startswith("state = ") and "running" in stripped:
                running = True
        running = running or pid is not None
    return {"running": running, "pid": pid, "label": label, "source": "launchctl"}


def _bot_status() -> dict:
    """Stato del bot Telegram letto da launchd."""
    return _launchd_status("bot")


def _market_data_status(db: Session) -> dict[str, Any]:
    config_row = db.get(AppSetting, "market_data_ws")
    config = config_row.value if config_row and isinstance(config_row.value, dict) else {}
    state_row = db.get(AppSetting, "market_data_ws_state")
    state = state_row.value if state_row and isinstance(state_row.value, dict) else {}
    launchd = _launchd_status("marketdata")
    last_message_at = _coerce_utc_datetime(state.get("last_message_at"))
    seconds_since_message = (
        (datetime.now(UTC) - last_message_at).total_seconds() if last_message_at else None
    )
    enabled = bool(config.get("enabled", True))
    healthy = bool(
        enabled
        and launchd["running"]
        and state.get("connected")
        and state.get("ready")
        and seconds_since_message is not None
        and seconds_since_message < 120
    )
    return {
        **state,
        **launchd,
        "enabled": enabled,
        "healthy": healthy,
        "seconds_since_last_message": seconds_since_message,
    }


def _service_script(service: str, action: str) -> str:
    config = SYSTEM_SERVICES[service]
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{config['label']}"
    plist = FilePath(str(config["plist"]))
    if action in {"start", "reboot"} and not plist.exists():
        raise HTTPException(status_code=404, detail=f"LaunchAgent plist not found: {plist}")

    quoted_domain = shlex.quote(domain)
    quoted_target = shlex.quote(target)
    quoted_plist = shlex.quote(str(plist))

    if action == "stop":
        command = f"/bin/launchctl bootout {quoted_target}"
    elif action == "start":
        command = (
            f"/bin/launchctl bootstrap {quoted_domain} {quoted_plist}; "
            f"/bin/launchctl kickstart {quoted_target}"
        )
    else:
        command = (
            f"/bin/launchctl kickstart -k {quoted_target} || "
            f"/bin/launchctl bootstrap {quoted_domain} {quoted_plist}"
        )
    return f"sleep 0.2; {command}"


def _schedule_service_action(service: str, action: str) -> None:
    script = _service_script(service, action)
    subprocess.Popen(
        ["/bin/sh", "-lc", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _tail_error_log(path: FilePath, limit: int) -> dict:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "updated_at": None,
            "lines": [],
        }
    try:
        with path.open("rb") as handle:
            lines = deque(handle, maxlen=limit)
    except OSError as exc:
        error_id = _log_and_redact("system_error_log_read_failed", exc)
        raise HTTPException(status_code=500, detail=f"Failed to read log, error_id={error_id}") from exc

    return {
        "path": str(path),
        "exists": True,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        "lines": [line.decode("utf-8", errors="replace").rstrip("\n") for line in lines],
    }


def _system_cycle_status_entry(db: Session, cycle_id: str) -> dict[str, Any]:
    setting = db.get(AppSetting, SYSTEM_CYCLE_STATUS_SETTING_KEY)
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    cycles = value.get("cycles") if isinstance(value, dict) else {}
    if not isinstance(cycles, dict):
        return {}
    entry = cycles.get(cycle_id)
    return dict(entry) if isinstance(entry, dict) else {}


def _service_cycle_diagnostic(
    db: Session,
    *,
    cycle_id: str,
    label: str,
    success_events: Sequence[str],
    failure_events: Sequence[str],
    expected_interval_seconds: int,
    failure_window_seconds: int,
    failure_threshold: int,
    now: datetime,
) -> dict[str, Any]:
    status_entry = _system_cycle_status_entry(db, cycle_id)
    latest_success = _latest_decision_log(db, success_events)
    latest_failure = _latest_decision_log(db, failure_events)
    stored_status = status_entry.get("status") if isinstance(status_entry.get("status"), str) else None

    last_seen_at = _max_datetime(
        _coerce_utc_datetime(status_entry.get("last_seen_at")),
        _coerce_utc_datetime(latest_success.timestamp if latest_success is not None else None),
        _coerce_utc_datetime(latest_failure.timestamp if latest_failure is not None else None),
    )
    last_ok_at = _max_datetime(
        _coerce_utc_datetime(status_entry.get("last_ok_at")),
        _coerce_utc_datetime(latest_success.timestamp if latest_success is not None else None),
    )
    last_failure_at = _max_datetime(
        _coerce_utc_datetime(status_entry.get("last_failure_at")),
        _coerce_utc_datetime(latest_failure.timestamp if latest_failure is not None else None),
    )
    failure_count = _decision_log_count_since(
        db,
        failure_events,
        now - timedelta(seconds=failure_window_seconds),
    )
    stale_after = max(
        expected_interval_seconds * SYSTEM_CYCLE_STALE_FACTOR,
        expected_interval_seconds + SYSTEM_CYCLE_MIN_STALE_GRACE_SECONDS,
    )
    is_stale = (
        last_seen_at is not None
        and stored_status not in {"skipped", "disabled"}
        and (now - last_seen_at).total_seconds() > stale_after
    )
    status = "unknown"
    state = "unknown"
    if failure_count >= failure_threshold:
        status = "failed"
        state = "failing"
    elif is_stale:
        status = "stale"
        state = "stale"
    elif stored_status in {"skipped", "disabled"}:
        status = "skipped"
        state = "skipped"
    elif (
        last_failure_at is not None
        and (last_ok_at is None or last_failure_at > last_ok_at)
    ):
        status = "warn"
        state = "degraded"
    elif last_seen_at is not None:
        status = "ok"
        state = "running"

    message = status_entry.get("message")
    if not isinstance(message, str):
        message = (
            latest_failure.message
            if latest_failure is not None and status in {"failed", "warn"}
            else latest_success.message if latest_success is not None else None
        )
    data = status_entry.get("data") if isinstance(status_entry.get("data"), dict) else {}
    if not data and latest_success is not None and isinstance(latest_success.data, dict):
        data = latest_success.data
    if status in {"failed", "warn"} and latest_failure is not None and isinstance(latest_failure.data, dict):
        data = latest_failure.data

    return {
        "id": cycle_id,
        "label": label,
        "status": status,
        "state": state,
        "message": message,
        "last_seen_at": _iso_or_none(last_seen_at),
        "last_ok_at": _iso_or_none(last_ok_at),
        "last_failure_at": _iso_or_none(last_failure_at),
        "expected_interval_seconds": expected_interval_seconds,
        "stale_after_seconds": stale_after,
        "failure_count": failure_count,
        "failure_threshold": failure_threshold,
        "failure_window_seconds": failure_window_seconds,
        "consecutive_failures": status_entry.get("consecutive_failures", 0),
        "data": data,
    }


def _system_cycle_configs(settings: Any) -> list[dict[str, Any]]:
    # Con fast_risk attivo, liquidation guard e UAR sono guidati dal fast_risk
    # loop (vedi worker/fast_risk.py) alle sue cadenze, non da quelle legacy del
    # loop principale: l'intervalo atteso (e quindi la soglia di staleness) deve
    # rispecchiare la cadenza reale, altrimenti la card va 'stale' a torto.
    fast_risk_on = bool(getattr(settings, "fast_risk_loop_enabled", False))
    live_liquidation_interval = (
        int(settings.fast_risk_liquidation_interval_seconds)
        if fast_risk_on
        else LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS
    )
    uar_interval = (
        int(settings.fast_risk_uar_interval_seconds)
        if fast_risk_on
        else UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS
    )
    return [
        {
            "cycle_id": "fast_risk_cycle",
            "label": "Fast Risk Cycle",
            "success_events": ("fast_risk_cycle",),
            "failure_events": ("fast_risk_cycle_failed",),
            "expected_interval_seconds": int(settings.fast_risk_loop_interval_seconds),
            "failure_window_seconds": 600,
            "failure_threshold": 3,
        },
        {
            "cycle_id": "worker_cycle",
            "label": "Worker Cycle",
            "success_events": ("worker_cycle",),
            "failure_events": ("worker_cycle_failed",),
            "expected_interval_seconds": WORKER_CYCLE_INTERVAL_SECONDS,
            "failure_window_seconds": 900,
            "failure_threshold": 3,
        },
        {
            "cycle_id": "live_liquidation_guard_cycle",
            "label": "Live Liquidation Guard Cycle",
            "success_events": ("live_liquidation_guard_cycle",),
            "failure_events": ("live_liquidation_guard_cycle_failed",),
            "expected_interval_seconds": live_liquidation_interval,
            "failure_window_seconds": 86400,
            "failure_threshold": 2,
        },
        {
            "cycle_id": "unified_account_ratio_guard_cycle",
            "label": "Unified Account Ratio Guard Cycle",
            "success_events": ("unified_account_ratio_guard_cycle",),
            "failure_events": ("unified_account_ratio_guard_cycle_failed",),
            "expected_interval_seconds": uar_interval,
            "failure_window_seconds": 900,
            "failure_threshold": 3,
        },
        {
            "cycle_id": "data_retention_cycle",
            "label": "Data Retention Cycle",
            "success_events": ("data_retention_cycle",),
            "failure_events": ("data_retention_cycle_failed",),
            "expected_interval_seconds": DATA_RETENTION_INTERVAL_SECONDS,
            "failure_window_seconds": 86400,
            "failure_threshold": 1,
        },
        {
            "cycle_id": "diagnostic_report_cycle",
            "label": "Diagnostic Report Cycle",
            "success_events": ("diagnostic_report_cycle",),
            "failure_events": ("diagnostic_report_cycle_failed",),
            "expected_interval_seconds": DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
            "failure_window_seconds": 86400,
            "failure_threshold": 1,
        },
    ]


@router.get("/analytics/worker-cycles")
def worker_cycles(db: Session = Depends(get_db), limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(["worker_cycle", "worker_cycle_failed"]))
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "data": row.data or {},
        }
        for row in rows
    ]


@router.get("/analytics/system-cycles")
def system_cycles(db: Session = Depends(get_db)) -> dict[str, Any]:
    now = datetime.now(UTC)
    settings = get_settings()
    services = [
        _service_cycle_diagnostic(db, now=now, **config)
        for config in _system_cycle_configs(settings)
    ]
    degraded = [item for item in services if item["status"] in {"failed", "stale", "warn"}]
    return {
        "generated_at": now.isoformat(),
        "status": "degraded" if degraded else "ok",
        "degraded_count": len(degraded),
        "services": services,
    }


@router.post("/system/services/{service}/{action}")
def system_service_action(
    service: str = Path(..., pattern=r"^(backend|worker|marketdata|bot)$"),
    action: str = Path(..., pattern=r"^(start|stop|reboot)$"),
) -> dict:
    if service not in SYSTEM_SERVICES or action not in SYSTEM_ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown service action")
    _schedule_service_action(service, action)
    return {
        "status": "scheduled",
        "service": service,
        "action": action,
        "label": SYSTEM_SERVICES[service]["label"],
    }


@router.get("/system/error-logs")
def system_error_logs(limit: int = Query(20, ge=1, le=200)) -> dict:
    return {
        service: _tail_error_log(FilePath(str(config["error_log"])), limit)
        for service, config in SYSTEM_SERVICES.items()
    }


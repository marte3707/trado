from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

import structlog

from app.db.models import AppSetting, DecisionLog
from app.db.session import SessionLocal

logger = structlog.get_logger()

HYPERLIQUID_RATE_LIMIT_SETTING_KEY = "hyperliquid_rate_limit"
HYPERLIQUID_RATE_LIMIT_STATE_KEY = "hyperliquid_rate_limit_state"
_SKIP_LOG_THROTTLE_SECONDS = 300
_LOCAL_SKIP_LOGS_LOCK = Lock()
_LOCAL_SKIP_LOGS: dict[tuple[str, str], datetime] = {}
_LIVE_RISK_REQUESTS: ContextVar[bool] = ContextVar("hyperliquid_live_risk_requests", default=False)

DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS: dict[str, int] = {
    # HL's documented global REST cap is 1200 request-weight/minute per IP.
    # Stay deliberately below it so browser/API calls and manual checks have headroom.
    "ip_weight_budget_per_minute": 600,
    # Reserve room for live-entry checks and protection of existing positions.
    # Non-critical reads cannot consume this portion of the shared /info budget.
    "live_preflight_reserve_weight": 60,
    "noncritical_cooldown_seconds": 600,
    "address_limited_action_spacing_seconds": 10,
    "reconcile_interval_seconds": 180,
    "uar_interval_seconds": 300,
    "liquidation_interval_seconds": 600,
    "fills_cursor_overlap_minutes": 10,
}

_INFO_WEIGHTS: dict[str, int] = {
    "allMids": 2,
    "clearinghouseState": 2,
    "exchangeStatus": 2,
    # Most other /info requests are documented as weight 20.
    "candleSnapshot": 20,
    "frontendOpenOrders": 20,
    "l2Book": 20,
    "meta": 20,
    "metaAndAssetCtxs": 20,
    "openOrders": 20,
    "portfolio": 20,
    "predictedFundings": 20,
    "spotMeta": 20,
    "spotClearinghouseState": 2,
    "userFills": 20,
    "userFillsByTime": 20,
    "userFunding": 20,
}

_ACTION_WEIGHTS: dict[str, int] = {
    "cancel": 1,
    "close": 1,
    "modify": 1,
    "order": 1,
    "setLeverage": 1,
}


@dataclass(frozen=True)
class HyperliquidRateLimitDecision:
    allowed: bool
    reason: str | None = None
    next_retry_after: str | None = None
    weight: int = 0
    rate_limit_class: str = "unknown"


@contextmanager
def live_risk_request_priority() -> Iterator[None]:
    """Let live risk reads use the reserve, within the total budget and cooldown.

    Context variables propagate through asyncio.to_thread without promoting
    unrelated market-data requests running concurrently in the same process.
    """
    token = _LIVE_RISK_REQUESTS.set(True)
    try:
        yield
    finally:
        _LIVE_RISK_REQUESTS.reset(token)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _settings_from_raw(raw: Any) -> dict[str, int]:
    value = raw if isinstance(raw, dict) else {}
    result = dict(DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS)
    for key, default in DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS.items():
        try:
            minimum = 0 if key == "live_preflight_reserve_weight" else 1
            result[key] = max(minimum, int(value.get(key, default)))
        except (TypeError, ValueError):
            result[key] = default
    return result


def effective_hyperliquid_rate_limit_settings(db: Any | None = None) -> dict[str, int]:
    if db is not None:
        setting = db.get(AppSetting, HYPERLIQUID_RATE_LIMIT_SETTING_KEY)
        return _settings_from_raw(setting.value if setting is not None else None)
    try:
        session = SessionLocal()
    except Exception:
        return dict(DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS)
    try:
        setting = session.get(AppSetting, HYPERLIQUID_RATE_LIMIT_SETTING_KEY)
        return _settings_from_raw(setting.value if setting is not None else None)
    except Exception:
        return dict(DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS)
    finally:
        session.close()


def hyperliquid_rate_limit_cooldown_until(db: Any) -> datetime | None:
    setting = db.get(AppSetting, HYPERLIQUID_RATE_LIMIT_STATE_KEY)
    raw = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    cooldown_until = _parse_dt(raw.get("cooldown_until"))
    if cooldown_until is None or _now() >= cooldown_until:
        return None
    return cooldown_until


def _weight_for(rate_limit_class: str, request_type: str) -> int:
    if rate_limit_class == "info":
        return _INFO_WEIGHTS.get(request_type, 20)
    if rate_limit_class == "exchange":
        return _ACTION_WEIGHTS.get(request_type, 1)
    return 20


def _state_dict(setting: AppSetting | None, now: datetime) -> dict[str, Any]:
    state = dict(setting.value) if setting is not None and isinstance(setting.value, dict) else {}
    window_started_at = _parse_dt(state.get("window_started_at"))
    if window_started_at is None or now - window_started_at >= timedelta(minutes=1):
        state["window_started_at"] = now.isoformat()
        state["window_weight"] = 0
    else:
        try:
            state["window_weight"] = max(0, int(state.get("window_weight", 0)))
        except (TypeError, ValueError):
            state["window_weight"] = 0
    return state


def _store_state(db: Any, state: dict[str, Any]) -> None:
    setting = db.get(AppSetting, HYPERLIQUID_RATE_LIMIT_STATE_KEY)
    if setting is None:
        db.add(AppSetting(key=HYPERLIQUID_RATE_LIMIT_STATE_KEY, value=state))
    else:
        setting.value = state
        db.add(setting)


def _locked_state_setting(db: Any) -> AppSetting | None:
    """Lock the shared governor row before read-modify-write operations."""
    return db.get(
        AppSetting,
        HYPERLIQUID_RATE_LIMIT_STATE_KEY,
        with_for_update=True,
    )


def available_hyperliquid_info_request_capacity(
    db: Any,
    *,
    request_type: str,
) -> int:
    """Return conservative non-live /info capacity in the current minute."""
    now = _now()
    settings = effective_hyperliquid_rate_limit_settings(db)
    state = _state_dict(db.get(AppSetting, HYPERLIQUID_RATE_LIMIT_STATE_KEY), now)
    cooldown_until = _parse_dt(state.get("cooldown_until"))
    if cooldown_until is not None and now < cooldown_until:
        return 0
    budget = settings["ip_weight_budget_per_minute"]
    reserve = min(settings["live_preflight_reserve_weight"], budget)
    available_weight = max(0, budget - reserve - int(state.get("window_weight", 0)))
    weight = max(1, _weight_for("info", request_type))
    return available_weight // weight


def check_hyperliquid_rate_limit(
    *,
    request_type: str,
    rate_limit_class: str,
    safety_critical: bool = False,
    live_preflight: bool = False,
) -> HyperliquidRateLimitDecision:
    """Consume local HL request budget before a network call.

    The governor is intentionally best-effort when no DB is available so unit
    tests and offline tooling do not need a live Postgres connection. In normal
    backend/worker processes it persists state in app_settings, which gives the
    backend API and worker a shared cooldown window.
    """
    weight = _weight_for(rate_limit_class, request_type)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return HyperliquidRateLimitDecision(True, weight=weight, rate_limit_class=rate_limit_class)
    try:
        db = SessionLocal()
    except Exception:
        return HyperliquidRateLimitDecision(True, weight=weight, rate_limit_class=rate_limit_class)
    try:
        now = _now()
        settings = effective_hyperliquid_rate_limit_settings(db)
        setting = _locked_state_setting(db)
        state = _state_dict(setting, now)
        cooldown_until = _parse_dt(state.get("cooldown_until"))
        order_available_at = _parse_dt(state.get("order_action_available_at"))

        if (
            cooldown_until is not None
            and now < cooldown_until
            and not safety_critical
            and rate_limit_class != "exchange"
        ):
            _log_skip(db, state, now, request_type, rate_limit_class, "global_cooldown", cooldown_until)
            _store_state(db, state)
            db.commit()
            return HyperliquidRateLimitDecision(
                False,
                reason="global_cooldown",
                next_retry_after=cooldown_until.isoformat(),
                weight=weight,
                rate_limit_class=rate_limit_class,
            )

        if rate_limit_class == "exchange" and order_available_at is not None and now < order_available_at:
            _log_skip(
                db,
                state,
                now,
                request_type,
                rate_limit_class,
                "address_action_spacing",
                order_available_at,
            )
            _store_state(db, state)
            db.commit()
            return HyperliquidRateLimitDecision(
                False,
                reason="address_action_spacing",
                next_retry_after=order_available_at.isoformat(),
                weight=weight,
                rate_limit_class=rate_limit_class,
            )

        budget = settings["ip_weight_budget_per_minute"]
        reserve = min(settings["live_preflight_reserve_weight"], budget)
        effective_budget = (
            budget
            if live_preflight or _LIVE_RISK_REQUESTS.get() or rate_limit_class != "info"
            else max(0, budget - reserve)
        )
        current_weight = int(state.get("window_weight", 0))
        if current_weight + weight > effective_budget and not (
            safety_critical and rate_limit_class == "exchange"
        ):
            window_started_at = _parse_dt(state.get("window_started_at")) or now
            retry_at = window_started_at + timedelta(minutes=1)
            _log_skip(db, state, now, request_type, rate_limit_class, "ip_weight_budget", retry_at)
            _store_state(db, state)
            db.commit()
            return HyperliquidRateLimitDecision(
                False,
                reason="ip_weight_budget",
                next_retry_after=retry_at.isoformat(),
                weight=weight,
                rate_limit_class=rate_limit_class,
            )

        state["window_weight"] = current_weight + weight
        state["last_request_at"] = now.isoformat()
        state["last_request_type"] = request_type
        state["last_rate_limit_class"] = rate_limit_class
        _store_state(db, state)
        db.commit()
        return HyperliquidRateLimitDecision(True, weight=weight, rate_limit_class=rate_limit_class)
    except Exception as exc:
        with suppress(Exception):
            db.rollback()
        logger.warning("hyperliquid_rate_limit_governor_unavailable", error=str(exc))
        return HyperliquidRateLimitDecision(True, weight=weight, rate_limit_class=rate_limit_class)
    finally:
        db.close()


def record_hyperliquid_rate_limit(
    *,
    request_type: str,
    rate_limit_class: str,
    status_code: int | None = None,
    error: str | None = None,
) -> str | None:
    if status_code != 429 and (error is None or "429" not in str(error)):
        return None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        db = SessionLocal()
    except Exception:
        return None
    try:
        now = _now()
        settings = effective_hyperliquid_rate_limit_settings(db)
        state = _state_dict(_locked_state_setting(db), now)
        cooldown_until = now + timedelta(seconds=settings["noncritical_cooldown_seconds"])
        state["last_429_at"] = now.isoformat()
        state["last_429_request_type"] = request_type
        state["last_429_rate_limit_class"] = rate_limit_class
        state["cooldown_until"] = cooldown_until.isoformat()
        if rate_limit_class == "exchange":
            state["order_action_available_at"] = (
                now + timedelta(seconds=settings["address_limited_action_spacing_seconds"])
            ).isoformat()
        _store_state(db, state)
        db.add(
            DecisionLog(
                event_type="hyperliquid_rate_limit_entered",
                message="Hyperliquid rate limit cooldown entered",
                data={
                    "request_type": request_type,
                    "rate_limit_class": rate_limit_class,
                    "status_code": status_code,
                    "cooldown_until": cooldown_until.isoformat(),
                    "order_action_available_at": state.get("order_action_available_at"),
                },
            )
        )
        db.commit()
        return state.get("order_action_available_at") or cooldown_until.isoformat()
    except Exception as exc:
        with suppress(Exception):
            db.rollback()
        logger.warning("hyperliquid_rate_limit_record_failed", error=str(exc))
        return None
    finally:
        db.close()


def record_hyperliquid_response_weight(
    *,
    request_type: str,
    rate_limit_class: str,
    item_count: int,
) -> None:
    # HL adds 1 weight per 20 returned items for several history endpoints.
    extra = max(0, int(item_count)) // 20
    if extra <= 0:
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        db = SessionLocal()
    except Exception:
        return
    try:
        now = _now()
        state = _state_dict(_locked_state_setting(db), now)
        state["window_weight"] = int(state.get("window_weight", 0)) + extra
        state["last_response_extra_weight"] = extra
        state["last_response_request_type"] = request_type
        state["last_response_rate_limit_class"] = rate_limit_class
        _store_state(db, state)
        db.commit()
    except Exception as exc:
        with suppress(Exception):
            db.rollback()
        logger.warning("hyperliquid_rate_limit_response_weight_failed", error=str(exc))
    finally:
        db.close()


def _log_skip(
    db: Any,
    state: dict[str, Any],
    now: datetime,
    request_type: str,
    rate_limit_class: str,
    reason: str,
    next_retry_at: datetime,
) -> None:
    retry_after = next_retry_at.isoformat()
    skip_key = f"{rate_limit_class}:{request_type}:{reason}"
    local_key = (skip_key, retry_after)
    with _LOCAL_SKIP_LOGS_LOCK:
        logged_at = _LOCAL_SKIP_LOGS.get(local_key)
        if logged_at is not None and now - logged_at < timedelta(seconds=_SKIP_LOG_THROTTLE_SECONDS):
            _increment_suppressed_skip(state, skip_key, retry_after)
            return
        _LOCAL_SKIP_LOGS[local_key] = now
        stale_before = now - timedelta(seconds=_SKIP_LOG_THROTTLE_SECONDS * 2)
        stale_keys = [key for key, value in _LOCAL_SKIP_LOGS.items() if value < stale_before]
        for key in stale_keys:
            _LOCAL_SKIP_LOGS.pop(key, None)

    last_skip_logs = state.get("last_skip_logs")
    if not isinstance(last_skip_logs, dict):
        last_skip_logs = {}

    last_log = last_skip_logs.get(skip_key)
    if isinstance(last_log, dict):
        logged_at = _parse_dt(last_log.get("logged_at"))
        if (
            last_log.get("next_retry_after") == retry_after
            and logged_at is not None
            and now - logged_at < timedelta(seconds=_SKIP_LOG_THROTTLE_SECONDS)
        ):
            _increment_suppressed_skip(state, skip_key, retry_after)
            return

    suppressed_count = 0
    if isinstance(last_log, dict):
        try:
            suppressed_count = int(last_log.get("suppressed_count", 0) or 0)
        except (TypeError, ValueError):
            suppressed_count = 0

    last_skip_logs[skip_key] = {
        "logged_at": now.isoformat(),
        "next_retry_after": retry_after,
        "suppressed_count": 0,
    }
    state["last_skip_logs"] = last_skip_logs
    db.add(
        DecisionLog(
            event_type="hyperliquid_rate_limit_skipped",
            message="Hyperliquid request skipped by local rate-limit governor",
            data={
                "request_type": request_type,
                "rate_limit_class": rate_limit_class,
                "reason": reason,
                "next_retry_after": retry_after,
                "suppressed_since_last_log": suppressed_count,
            },
        )
    )


def _increment_suppressed_skip(state: dict[str, Any], skip_key: str, retry_after: str) -> None:
    last_skip_logs = state.get("last_skip_logs")
    if not isinstance(last_skip_logs, dict):
        last_skip_logs = {}
    last_log = last_skip_logs.get(skip_key)
    if not isinstance(last_log, dict):
        last_log = {"next_retry_after": retry_after, "suppressed_count": 0}
    try:
        suppressed_count = int(last_log.get("suppressed_count", 0) or 0)
    except (TypeError, ValueError):
        suppressed_count = 0
    last_log["next_retry_after"] = retry_after
    last_log["suppressed_count"] = suppressed_count + 1
    last_skip_logs[skip_key] = last_log
    state["last_skip_logs"] = last_skip_logs

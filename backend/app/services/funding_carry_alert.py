"""Alert when Hyperliquid funding makes the spot+perp carry trade worth re-testing.

Research (research/funding_carry) showed the delta-neutral carry only pays well when
funding on the majors is persistently high (2024: ~20-25% APR) and is not worth running
at the ~5-10% seen in 2025-2026. This job does not trade: every few hours it reads the
trailing funding of the watched coins and sends one Telegram message when all of them
sit above ``entry_apr``. It re-arms only after the average falls below ``rearm_apr``,
so a sustained regime produces a single alert rather than one per check.

Settings (AppSetting ``funding_carry_alert``): enabled, coins, lookback_days,
entry_apr, rearm_apr. State (AppSetting ``funding_carry_alert_state``): armed,
last_alert_at, last_check_at, last_aprs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import AppSetting

SETTINGS_KEY = "funding_carry_alert"
STATE_KEY = "funding_carry_alert_state"
HOURS_PER_YEAR = 24 * 365
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "coins": ["BTC", "ETH"],
    "lookback_days": 7,
    "entry_apr": 0.15,
    "rearm_apr": 0.10,
}

FundingHistoryFetcher = Callable[[str, int, int], Awaitable[Sequence[dict[str, Any]]]]
MessageSender = Callable[[str], Awaitable[bool]]


def funding_carry_alert_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, SETTINGS_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    merged = {**DEFAULT_SETTINGS, **value}
    coins = merged.get("coins")
    if not isinstance(coins, list) or not all(isinstance(c, str) and c for c in coins):
        merged["coins"] = list(DEFAULT_SETTINGS["coins"])
    return merged


def _load_state(db: Session) -> tuple[AppSetting, dict[str, Any]]:
    row = db.get(AppSetting, STATE_KEY)
    if row is None:
        row = AppSetting(key=STATE_KEY, value={"armed": True})
        db.add(row)
    state = dict(row.value) if isinstance(row.value, dict) else {"armed": True}
    state.setdefault("armed", True)
    return row, state


def trailing_apr(rows: Sequence[dict[str, Any]]) -> float | None:
    """Mean hourly funding rate, annualised. None when there is no usable data."""
    rates: list[float] = []
    for row in rows:
        try:
            rates.append(float(row["fundingRate"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not rates:
        return None
    return sum(rates) / len(rates) * HOURS_PER_YEAR


def _format_message(aprs: dict[str, float], settings: dict[str, Any]) -> str:
    lines = [
        "📈 <b>Funding carry: regime favorevole</b>",
        f"Funding medio ultimi {settings['lookback_days']} giorni sopra {settings['entry_apr'] * 100:.0f}% annuo:",
    ]
    lines += [f"• {coin}: {apr * 100:.1f}% annuo" for coin, apr in aprs.items()]
    lines += [
        "",
        "Rilancia export e backtest prima di attivare il carry:",
        "<code>cd research/funding_carry && python3 export_hyperliquid_funding.py</code>",
        "<code>python carry_backtest.py --data ./data</code>",
    ]
    return "\n".join(lines)


async def check_funding_carry_regime(
    db: Session,
    *,
    fetch_funding_history: FundingHistoryFetcher,
    send_message: MessageSender,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = funding_carry_alert_settings(db)
    if not settings.get("enabled", True):
        return {"status": "disabled"}

    lookback_days = max(1, int(settings.get("lookback_days", 7)))
    entry_apr = float(settings.get("entry_apr", 0.15))
    rearm_apr = float(settings.get("rearm_apr", 0.10))
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=lookback_days)).timestamp() * 1000)

    aprs: dict[str, float] = {}
    for coin in settings["coins"]:
        apr = trailing_apr(await fetch_funding_history(coin, start_ms, end_ms))
        if apr is None:
            return {"status": "skipped", "reason": f"no_funding_data:{coin}"}
        aprs[coin] = apr

    row, state = _load_state(db)
    average = sum(aprs.values()) / len(aprs)
    triggered = all(apr >= entry_apr for apr in aprs.values())
    result: dict[str, Any] = {
        "status": "ok",
        "aprs": {coin: round(apr, 4) for coin, apr in aprs.items()},
        "average_apr": round(average, 4),
        "triggered": triggered,
        "alert_sent": False,
    }

    if triggered and state.get("armed", True):
        if await send_message(_format_message(aprs, settings)):
            state["armed"] = False
            state["last_alert_at"] = now.isoformat()
            result["alert_sent"] = True
        else:
            result["status"] = "send_failed"
    elif not state.get("armed", True) and average < rearm_apr:
        state["armed"] = True
        result["rearmed"] = True

    state["last_check_at"] = now.isoformat()
    state["last_aprs"] = result["aprs"]
    result["armed"] = state["armed"]
    row.value = state
    flag_modified(row, "value")
    return result

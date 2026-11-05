"""Monitor the Hyperliquidity Provider (HLP) vault for a passive depositor.

Read-only: it never deposits or withdraws. Every few hours it reads ``vaultDetails``
and sends admin Telegram alerts on two kinds of change:

* **Incident** – the vault's PnL fell more than ``incident_drawdown`` (default 3%) from
  its 30-day peak, or a single sample lost more than ``incident_step_loss`` (1%).
  These are the moments past manipulation attacks (JELLY, POPCAT) showed up, and the
  depositor's cue to look at withdrawing (4-day lockup). It re-arms below
  ``incident_rearm`` (1%).
* **Yield regime** – the 30-day annualised depositor return moved into another band:
  ``negative`` (<0), ``low`` (0..good_yield) or ``good`` (>= good_yield, default 10%).
  The first check only records the band; later band changes send one message each.

Returns are flow-neutral: delta PnL divided by the larger of the account values at
the two ends, because deposits and withdrawals move account value but not PnL
(same method as research/hlp_income).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import AppSetting

HLP_VAULT_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
SETTINGS_KEY = "hlp_monitor"
STATE_KEY = "hlp_monitor_state"
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "vault_address": HLP_VAULT_ADDRESS,
    "incident_drawdown": 0.03,
    "incident_step_loss": 0.01,
    "incident_rearm": 0.01,
    "good_yield": 0.10,
    "user_address": None,     # wallet that holds the HLP deposit; defaults to HYPERLIQUID_ACCOUNT_ADDRESS
}

VaultDetailsFetcher = Callable[[str, str | None], Awaitable[dict[str, Any]]]
MessageSender = Callable[[str], Awaitable[bool]]


def hlp_monitor_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, SETTINGS_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {**DEFAULT_SETTINGS, **value}


def _series(details: dict[str, Any], period: str) -> list[tuple[int, float, float]]:
    """[(time_ms, account_value, cumulative_pnl)] for one portfolio period."""
    for name, data in details.get("portfolio") or []:
        if name != period or not isinstance(data, dict):
            continue
        values = {int(t): float(v) for t, v in data.get("accountValueHistory") or []}
        pnls = {int(t): float(v) for t, v in data.get("pnlHistory") or []}
        return [(t, values[t], pnls[t]) for t in sorted(values) if t in pnls and values[t] > 0]
    return []


def vault_metrics(details: dict[str, Any]) -> dict[str, float] | None:
    month = _series(details, "month")
    if len(month) < 2:
        return None
    t0, av0, pnl0 = month[0]
    t1, av1, pnl1 = month[-1]
    days = (t1 - t0) / 86_400_000
    if days <= 0:
        return None
    ret_30d = (pnl1 - pnl0) / max(av0, av1)
    annualised = (1 + ret_30d) ** (365.25 / days) - 1 if ret_30d > -1 else -1.0
    peak_pnl = max(p for _, _, p in month)
    drawdown = (peak_pnl - pnl1) / av1
    fine = _series(details, "week") or month
    worst_step = 0.0
    for (_, a_prev, p_prev), (_, a_cur, p_cur) in zip(fine, fine[1:], strict=False):
        worst_step = min(worst_step, (p_cur - p_prev) / max(a_prev, a_cur))
    return {
        "return_30d": ret_30d,
        "annualised_30d": annualised,
        "drawdown_from_30d_peak": drawdown,
        "worst_step_7d": worst_step,
        "account_value": av1,
        "window_days": days,
    }


def yield_band(annualised: float, good_yield: float) -> str:
    if annualised < 0:
        return "negative"
    return "good" if annualised >= good_yield else "low"


def _follower_line(details: dict[str, Any]) -> str | None:
    state = details.get("followerState")
    if not isinstance(state, dict):
        return None
    try:
        equity = float(state.get("vaultEquity") or 0)
        pnl = float(state.get("allTimePnl") or state.get("pnl") or 0)
    except (TypeError, ValueError):
        return None
    line = f"Tua posizione: {equity:,.2f} USDC (PnL totale {pnl:+,.2f})"
    lockup = state.get("lockupUntil")
    if isinstance(lockup, int | float) and lockup > 0:
        until = datetime.fromtimestamp(lockup / 1000, UTC)
        if until > datetime.now(UTC):
            line += f", prelievo possibile dal {until:%d/%m %H:%M} UTC"
    return line


def _message(title: str, lines: list[str], details: dict[str, Any]) -> str:
    follower = _follower_line(details)
    return "\n".join([title, *lines, *([follower] if follower else [])])


async def check_hlp_vault(
    db: Session,
    *,
    fetch_vault_details: VaultDetailsFetcher,
    send_message: MessageSender,
    user_address: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = hlp_monitor_settings(db)
    if not settings.get("enabled", True):
        return {"status": "disabled"}

    depositor = settings.get("user_address") or user_address
    details = await fetch_vault_details(str(settings["vault_address"]), depositor)
    metrics = vault_metrics(details) if isinstance(details, dict) else None
    if metrics is None:
        return {"status": "skipped", "reason": "no_vault_history"}

    row = db.get(AppSetting, STATE_KEY)
    if row is None:
        row = AppSetting(key=STATE_KEY, value={})
        db.add(row)
    state = dict(row.value) if isinstance(row.value, dict) else {}
    state.setdefault("incident_armed", True)

    result: dict[str, Any] = {"status": "ok", **{k: round(v, 6) for k, v in metrics.items()}, "alerts": []}
    stats = (
        f"Rendimento 30g: {metrics['return_30d'] * 100:+.2f}% "
        f"(annualizzato {metrics['annualised_30d'] * 100:+.1f}%)\n"
        f"Dal picco 30g: -{metrics['drawdown_from_30d_peak'] * 100:.2f}% · "
        f"peggior step 7g: {metrics['worst_step_7d'] * 100:+.2f}%\n"
        f"TVL vault: {metrics['account_value'] / 1e6:,.1f}M USDC"
    )

    incident = (
        metrics["drawdown_from_30d_peak"] >= float(settings["incident_drawdown"])
        or metrics["worst_step_7d"] <= -float(settings["incident_step_loss"])
    )
    if incident and state["incident_armed"]:
        text = _message(
            "🚨 <b>HLP: possibile incidente</b>",
            [stats, "", "Valuta un prelievo (lockup 4 giorni dall'ultimo deposito) e controlla le news."],
            details,
        )
        if await send_message(text):
            state["incident_armed"] = False
            state["last_incident_at"] = now.isoformat()
            result["alerts"].append("incident")
    elif not state["incident_armed"] and (
        metrics["drawdown_from_30d_peak"] < float(settings["incident_rearm"])
        and metrics["worst_step_7d"] > -float(settings["incident_step_loss"])
    ):
        state["incident_armed"] = True
        result["incident_rearmed"] = True

    band = yield_band(metrics["annualised_30d"], float(settings["good_yield"]))
    previous = state.get("yield_band")
    if previous is not None and band != previous:
        titles = {
            "negative": "📉 <b>HLP: rendimento 30g negativo</b>",
            "low": "➖ <b>HLP: rendimento 30g basso</b>",
            "good": "📈 <b>HLP: rendimento 30g tornato buono</b>",
        }
        if await send_message(_message(titles[band], [stats], details)):
            state["yield_band"] = band
            result["alerts"].append(f"yield_{band}")
    else:
        state["yield_band"] = band
    result["yield_band"] = state.get("yield_band")

    state["last_check_at"] = now.isoformat()
    state["last_metrics"] = {k: round(v, 6) for k, v in metrics.items()}
    row.value = state
    flag_modified(row, "value")
    return result

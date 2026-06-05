"""Scheduling dei job periodici del worker.

Incapsula la logica di *timing* (quando è dovuto ciascun job ricorrente) che prima
viveva in una manciata di variabili globali mutabili dentro `worker/main.py`. Il
*lavoro* dei job resta nel loop principale: qui si decide solo se è il momento di
eseguirli, in modo isolato e testabile.

Ogni metodo `due_*` ritorna True quando il job va eseguito e, contestualmente,
riprogramma la prossima esecuzione — così il loop principale resta privo di stato.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

RECAP_INTERVAL_SECONDS = 10800  # ogni 3 ore
DIAGNOSTIC_REPORT_INTERVAL_SECONDS = 86400  # ogni 24h
LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS = 10800  # ogni 3 ore
UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS = 60
DATA_RETENTION_INTERVAL_SECONDS = 86400  # ogni 24h
ERROR_NOTIFY_COOLDOWN_SECONDS = 1800  # max 1 notifica errore ogni 30 min
DAILY_RECAP_HOUR_ROME = 10
ROME_TZ = ZoneInfo("Europe/Rome")


def next_daily_recap_timestamp(now: datetime | None = None) -> float:
    current = now.astimezone(ROME_TZ) if now is not None else datetime.now(ROME_TZ)
    target = current.replace(
        hour=DAILY_RECAP_HOUR_ROME,
        minute=0,
        second=0,
        microsecond=0,
    )
    if current >= target:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).timestamp()


class PeriodicScheduler:
    """Tiene traccia delle prossime esecuzioni dei job ricorrenti del worker.

    `now` è un epoch (`time.time()`). I guardrail di liquidazione e UAR partono a
    `0.0` per essere dovuti già al primo tick (comportamento storico preservato).
    """

    def __init__(self, now: float) -> None:
        self.next_recap = now + RECAP_INTERVAL_SECONDS
        self.next_daily_recap = next_daily_recap_timestamp()
        self.next_diagnostic = now + DIAGNOSTIC_REPORT_INTERVAL_SECONDS
        self.next_live_liquidation_guard = 0.0
        self.next_unified_account_ratio_guard = 0.0
        self.next_data_retention = now + DATA_RETENTION_INTERVAL_SECONDS
        self.last_error_notify = 0.0

    def _due_interval(self, now: float, attr: str, interval: int) -> bool:
        if now >= getattr(self, attr):
            setattr(self, attr, now + interval)
            return True
        return False

    def due_recap(self, now: float) -> bool:
        return self._due_interval(now, "next_recap", RECAP_INTERVAL_SECONDS)

    def due_daily_recap(self, now: float) -> bool:
        if now >= self.next_daily_recap:
            self.next_daily_recap = next_daily_recap_timestamp(
                datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(seconds=1)
            )
            return True
        return False

    def due_diagnostic(self, now: float) -> bool:
        return self._due_interval(now, "next_diagnostic", DIAGNOSTIC_REPORT_INTERVAL_SECONDS)

    def due_data_retention(self, now: float) -> bool:
        return self._due_interval(now, "next_data_retention", DATA_RETENTION_INTERVAL_SECONDS)

    def due_live_liquidation_guard(self, now: float) -> bool:
        return self._due_interval(
            now, "next_live_liquidation_guard", LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS
        )

    def due_unified_account_ratio_guard(self, now: float) -> bool:
        return self._due_interval(
            now,
            "next_unified_account_ratio_guard",
            UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
        )

    def due_error_notify(self, now: float) -> bool:
        """Rate-limit delle notifiche di errore: True al massimo una volta per cooldown."""
        if now - self.last_error_notify >= ERROR_NOTIFY_COOLDOWN_SECONDS:
            self.last_error_notify = now
            return True
        return False

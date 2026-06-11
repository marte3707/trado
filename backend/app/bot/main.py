"""Entry point for the onboarding bot process.

Run with: python -m app.bot.main

Uses Telegram long polling against getUpdates so it works behind NAT without
exposing a webhook. Updates are processed sequentially per cycle to keep DB
sessions cleanly scoped — the volume here is conversational, not high-throughput.
"""
from __future__ import annotations

import asyncio
import signal
import time

import httpx
import structlog

from app.bot.admin_notify import send_bot_fatal, send_bot_heartbeat, send_bot_online
from app.bot.handlers import handle_update
from app.bot.telegram_api import TelegramBotAPI
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal

logger = structlog.get_logger()

# Periodic heartbeat to the admin chat. 1 hour keeps the signal tight enough
# to notice silent crashes early without spamming.
_HEARTBEAT_INTERVAL_SECONDS = 43200


_BOT_COMMANDS = [
    {"command": "start", "description": "Start or restart onboarding"},
    {"command": "help", "description": "List available commands"},
    {"command": "disclaimer", "description": "Review the risk disclaimer"},
    {"command": "privacy", "description": "Privacy notice"},
    {"command": "pause", "description": "Pause notifications"},
    {"command": "resume", "description": "Resume notifications"},
    {"command": "lang", "description": "Change language (it/en)"},
]


async def _process_update(api: TelegramBotAPI, settings, update: dict) -> bool:
    """Returns True if the update was handled cleanly, False if a handler
    raised. Caller increments error counters so heartbeats can report them."""
    db = SessionLocal()
    try:
        await handle_update(api, settings, db, update)
        return True
    except Exception as exc:
        logger.exception("bot_handler_failed", error=str(exc), update_id=update.get("update_id"))
        return False
    finally:
        db.close()


async def _maybe_send_heartbeat(
    settings,
    *,
    started_monotonic: float,
    last_heartbeat: float,
    updates_in_period: int,
    errors_in_period: int,
) -> tuple[float, int, int]:
    """Sends a heartbeat if the interval elapsed. Returns the (possibly
    updated) (last_heartbeat, updates_in_period, errors_in_period) counters."""
    now = time.monotonic()
    if now - last_heartbeat < _HEARTBEAT_INTERVAL_SECONDS:
        return last_heartbeat, updates_in_period, errors_in_period
    db = SessionLocal()
    try:
        await send_bot_heartbeat(
            settings,
            db,
            updates_handled=updates_in_period,
            errors=errors_in_period,
            uptime_seconds=now - started_monotonic,
        )
    except Exception as exc:
        logger.warning("bot_heartbeat_send_failed", error=str(exc))
    finally:
        db.close()
    return now, 0, 0


async def run() -> None:
    configure_logging()
    settings = get_settings()
    token = settings.public_bot_token
    if not token:
        logger.warning(
            "bot_disabled_no_token",
            message="Neither LANDING_BOT_TOKEN nor TELEGRAM_BOT_TOKEN is set, bot will not start",
        )
        return

    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        logger.info("bot_stop_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # add_signal_handler is unavailable on Windows; production target is
            # macOS/Linux, so this fallback path is just for unit-test safety.
            signal.signal(sig, _request_stop)

    timeout = httpx.Timeout(40.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        api = TelegramBotAPI(token, client)
        try:
            await api.set_my_commands(_BOT_COMMANDS)
        except Exception as exc:
            logger.warning("bot_set_commands_failed", error=str(exc))

        logger.info(
            "bot_started",
            invite_chat_id=settings.telegram_signal_chat_id,
            username=settings.landing_bot_username,
            using_dedicated_token=bool(settings.landing_bot_token),
        )

        # Notify the admin chat that we're up. Best-effort: a failure here
        # shouldn't prevent the bot from serving users.
        db = SessionLocal()
        try:
            await send_bot_online(settings, db)
        except Exception as exc:
            logger.warning("bot_online_notify_failed", error=str(exc))
        finally:
            db.close()

        started_monotonic = time.monotonic()
        last_heartbeat = started_monotonic
        updates_in_period = 0
        errors_in_period = 0
        offset = 0
        backoff = 1.0
        while not stop_event.is_set():
            try:
                updates = await api.get_updates(offset=offset, timeout=30)
                backoff = 1.0
            except Exception as exc:
                logger.warning("bot_get_updates_failed", error=str(exc), backoff=backoff)
                errors_in_period += 1
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    break
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
                continue

            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                ok = await _process_update(api, settings, update)
                updates_in_period += 1
                if not ok:
                    errors_in_period += 1

            last_heartbeat, updates_in_period, errors_in_period = await _maybe_send_heartbeat(
                settings,
                started_monotonic=started_monotonic,
                last_heartbeat=last_heartbeat,
                updates_in_period=updates_in_period,
                errors_in_period=errors_in_period,
            )

        logger.info("bot_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as exc:
        # Last-resort: notify the admin chat before the process dies so the
        # silent-failure window between launchd restarts is visible.
        logger.exception("bot_run_crashed", error=str(exc))
        try:
            settings = get_settings()
            asyncio.run(send_bot_fatal(settings, "bot.main.run", exc))
        except Exception:
            pass
        raise

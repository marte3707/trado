from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect

from app.core.config import Settings
from app.db.models import AppSetting
from app.db.session import SessionLocal
from app.services.hyperliquid import HyperliquidClient

logger = structlog.get_logger()

ACCOUNT_DATA_WS_SETTING_KEY = "account_data_ws"
ACCOUNT_DATA_WS_STATE_KEY = "account_data_ws_state"
ACCOUNT_DATA_WS_CHANNELS = ("clearinghouseState", "openOrders", "spotState")
DEFAULT_ACCOUNT_DATA_WS_SETTINGS: dict[str, int | bool] = {
    "enabled": True,
    "heartbeat_seconds": 20,
    "reconnect_max_seconds": 60,
    "writer_flush_seconds": 1,
    "enrichment_refresh_seconds": 60,
    "funding_refresh_seconds": 300,
    "snapshot_stale_seconds": 90,
}


def effective_account_data_ws_settings(db: Session) -> dict[str, int | bool]:
    row = db.get(AppSetting, ACCOUNT_DATA_WS_SETTING_KEY)
    raw = row.value if row is not None and isinstance(row.value, dict) else {}
    result = dict(DEFAULT_ACCOUNT_DATA_WS_SETTINGS)
    result["enabled"] = bool(raw.get("enabled", result["enabled"]))
    for key in DEFAULT_ACCOUNT_DATA_WS_SETTINGS:
        if key == "enabled":
            continue
        with contextlib.suppress(TypeError, ValueError):
            result[key] = max(1, int(raw.get(key, result[key])))
    return result


def account_subscription_payload(channel: str, address: str) -> dict[str, Any]:
    if channel not in ACCOUNT_DATA_WS_CHANNELS:
        raise ValueError(f"Unsupported account websocket channel: {channel}")
    return {
        "method": "subscribe",
        "subscription": {"type": channel, "user": address},
    }


def account_channel_payload(message: dict[str, Any]) -> tuple[str, Any] | None:
    channel = message.get("channel")
    data = message.get("data")
    if channel == "clearinghouseState" and isinstance(data, dict):
        nested = data.get("clearinghouseState")
        return "user_state", nested if isinstance(nested, dict) else data
    if channel == "spotState" and isinstance(data, dict):
        nested = data.get("spotState")
        return "spot_state", nested if isinstance(nested, dict) else data
    if channel == "openOrders":
        if isinstance(data, dict):
            orders = data.get("orders")
            if isinstance(orders, list):
                return "open_orders", orders
        if isinstance(data, list):
            return "open_orders", data
    return None


def websocket_snapshot_is_fresh(
    *,
    ready: bool,
    last_message_at: datetime | None,
    stale_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Whether subscribed account state is fresh enough to skip REST recovery."""
    if not ready or last_message_at is None:
        return False
    checked_at = now or datetime.now(UTC)
    return (checked_at - last_message_at).total_seconds() <= max(1, stale_seconds)


class HyperliquidAccountDataService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.connected = False
        self.ready = False
        self.received_channels: set[str] = set()
        self.reconnect_count = 0
        self.last_error: str | None = None
        self.last_message_at: datetime | None = None
        self.last_enrichment_at: datetime | None = None
        self.snapshot: dict[str, Any] = {}
        self.writer_flush_seconds = 1.0
        self.reconnect_max_seconds = 60.0
        self._dirty = asyncio.Event()

    async def run_forever(self) -> None:
        writer = asyncio.create_task(self._writer_loop())
        enrichment = asyncio.create_task(self._enrichment_loop())
        delay = 1.0
        try:
            while True:
                try:
                    await self._run_connection()
                    delay = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:500]
                    self.reconnect_count += 1
                    logger.exception("account_data_ws_connection_failed", error=self.last_error)
                    self.connected = False
                    self.ready = False
                    self._dirty.set()
                    await asyncio.sleep(delay + random.random())
                    delay = min(delay * 2, self.reconnect_max_seconds)
        finally:
            writer.cancel()
            enrichment.cancel()
            await asyncio.gather(writer, enrichment, return_exceptions=True)
            self.connected = False
            self.ready = False
            with contextlib.suppress(Exception):
                await self._write_state()

    async def _run_connection(self) -> None:
        config = await asyncio.to_thread(self._load_config)
        self.writer_flush_seconds = float(config["writer_flush_seconds"])
        self.reconnect_max_seconds = float(config["reconnect_max_seconds"])
        address = self.settings.hyperliquid_account_address
        if not bool(config["enabled"]) or not address:
            self.connected = False
            self.ready = False
            self.last_error = None if address else "HYPERLIQUID_ACCOUNT_ADDRESS is not configured"
            self._dirty.set()
            await asyncio.sleep(30)
            return

        heartbeat_seconds = int(config["heartbeat_seconds"])
        self.connected = True
        self.ready = False
        self.received_channels.clear()
        self.last_error = None
        self._dirty.set()
        try:
            async with connect(
                self.settings.ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_queue=256,
            ) as websocket:
                for channel in ACCOUNT_DATA_WS_CHANNELS:
                    await websocket.send(json.dumps(account_subscription_payload(channel, address)))
                while True:
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=heartbeat_seconds)
                    except TimeoutError:
                        await websocket.send(json.dumps({"method": "ping"}))
                        self._dirty.set()
                        continue
                    message = json.loads(raw)
                    parsed = account_channel_payload(message)
                    if parsed is None:
                        continue
                    field, value = parsed
                    self.snapshot[field] = value
                    self.last_message_at = datetime.now(UTC)
                    self.received_channels.add(str(message.get("channel")))
                    self.ready = all(channel in self.received_channels for channel in ACCOUNT_DATA_WS_CHANNELS)
                    if self.ready:
                        self.last_error = None
                    self._dirty.set()
        finally:
            self.connected = False
            self.ready = False
            self._dirty.set()

    async def _enrichment_loop(self) -> None:
        funding_last_run = 0.0
        while True:
            config = await asyncio.to_thread(self._load_config)
            refresh_seconds = int(config["enrichment_refresh_seconds"])
            funding_seconds = int(config["funding_refresh_seconds"])
            address = self.settings.hyperliquid_account_address
            if bool(config["enabled"]) and address:
                client = HyperliquidClient(self.settings)
                try:
                    now = time.monotonic()
                    funding_refreshed = False
                    ws_fresh = websocket_snapshot_is_fresh(
                        ready=self.ready,
                        last_message_at=self.last_message_at,
                        stale_seconds=int(config["snapshot_stale_seconds"]),
                    )
                    if not ws_fresh:
                        # REST only recovers missed messages while the socket is
                        # stale or disconnected; the subscribed state is primary.
                        user_state, spot_state, open_orders = await asyncio.gather(
                            client.user_state(address),
                            client.spot_user_state(address),
                            client.frontend_open_orders(address),
                        )
                        self.snapshot["user_state"] = user_state
                        self.snapshot["spot_state"] = spot_state
                        self.snapshot["frontend_open_orders"] = open_orders
                    if funding_last_run == 0.0 or now - funding_last_run >= funding_seconds:
                        checked_at = datetime.now(UTC)
                        predicted, user_funding = await asyncio.gather(
                            client.predicted_fundings(),
                            client.user_funding(
                                address,
                                int((checked_at - timedelta(days=1)).timestamp() * 1000),
                                int(checked_at.timestamp() * 1000),
                            ),
                        )
                        self.snapshot["predicted_fundings"] = predicted
                        self.snapshot["user_funding"] = user_funding
                        funding_last_run = now
                        funding_refreshed = True
                    if not ws_fresh or funding_refreshed:
                        self.last_enrichment_at = datetime.now(UTC)
                    self.snapshot.pop("enrichment_error", None)
                    self._dirty.set()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.snapshot["enrichment_error"] = str(exc)[:500]
                    self._dirty.set()
                    logger.warning("account_data_ws_enrichment_failed", error=str(exc)[:500])
            await asyncio.sleep(refresh_seconds)

    def _load_config(self) -> dict[str, int | bool]:
        db = SessionLocal()
        try:
            return effective_account_data_ws_settings(db)
        finally:
            db.close()

    async def _writer_loop(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(self.writer_flush_seconds)
            self._dirty.clear()
            try:
                await self._write_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("account_data_ws_writer_failed", error=str(exc)[:500])
                self._dirty.set()

    async def _write_state(self) -> None:
        snapshot = dict(self.snapshot)
        value = {
            **snapshot,
            "connected": self.connected,
            "ready": self.ready,
            "network": self.settings.hyperliquid_network,
            "account_address": self.settings.hyperliquid_account_address,
            "active_subscriptions": len(self.received_channels),
            "expected_subscriptions": len(ACCOUNT_DATA_WS_CHANNELS),
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "last_enrichment_at": (
                self.last_enrichment_at.isoformat() if self.last_enrichment_at else None
            ),
            "last_error": self.last_error,
            "reconnect_count": self.reconnect_count,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(self._write_state_sync, value)

    @staticmethod
    def _write_state_sync(value: dict[str, Any]) -> None:
        db = SessionLocal()
        try:
            row = db.get(AppSetting, ACCOUNT_DATA_WS_STATE_KEY)
            if row is None:
                db.add(AppSetting(key=ACCOUNT_DATA_WS_STATE_KEY, value=value))
            else:
                row.value = value
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

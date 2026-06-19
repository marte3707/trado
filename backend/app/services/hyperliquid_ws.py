from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect

from app.core.config import Settings
from app.core.enums import MarketType, OrderMode
from app.core.runtime_settings import (
    effective_enabled_timeframes,
    effective_monitoring_only_timeframes,
)
from app.db.models import AppSetting, Candle, Market, MarketDataEvent, Position, Signal
from app.db.session import SessionLocal
from app.services.candle_sync import TIMEFRAME_MS
from app.services.hyperliquid import parse_hl_candle

logger = structlog.get_logger()

MARKET_DATA_WS_SETTING_KEY = "market_data_ws"
MARKET_DATA_WS_STATE_KEY = "market_data_ws_state"
DEFAULT_MARKET_DATA_WS_SETTINGS: dict[str, int | bool] = {
    "enabled": True,
    "max_subscriptions": 950,
    "subscription_refresh_seconds": 60,
    "heartbeat_seconds": 20,
    "reconnect_max_seconds": 60,
    "writer_flush_seconds": 1,
    "writer_batch_size": 500,
}


@dataclass(frozen=True, order=True)
class CandleSubscription:
    market_id: int
    coin: str
    timeframe: str

    @property
    def key(self) -> tuple[str, str]:
        return self.coin, self.timeframe

    def payload(self) -> dict[str, Any]:
        return {
            "method": "subscribe",
            "subscription": {
                "type": "candle",
                "coin": self.coin,
                "interval": self.timeframe,
            },
        }


def effective_market_data_ws_settings(db: Session) -> dict[str, int | bool]:
    row = db.get(AppSetting, MARKET_DATA_WS_SETTING_KEY)
    raw = row.value if row is not None and isinstance(row.value, dict) else {}
    result = dict(DEFAULT_MARKET_DATA_WS_SETTINGS)
    result["enabled"] = bool(raw.get("enabled", result["enabled"]))
    for key in DEFAULT_MARKET_DATA_WS_SETTINGS:
        if key == "enabled":
            continue
        with contextlib.suppress(TypeError, ValueError):
            result[key] = max(1, int(raw.get(key, result[key])))
    return result


def _position_timeframe(db: Session, position: Position) -> str | None:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    timeframe = metadata.get("source_timeframe")
    if isinstance(timeframe, str) and timeframe in TIMEFRAME_MS:
        return timeframe
    source_signal_id = metadata.get("source_signal_id")
    if source_signal_id is None:
        return None
    try:
        signal_id = int(source_signal_id)
    except (TypeError, ValueError):
        return None
    signal = db.get(Signal, signal_id)
    if signal is None or signal.timeframe not in TIMEFRAME_MS:
        return None
    return signal.timeframe


def build_candle_subscription_plan(
    db: Session,
    settings: Settings,
    *,
    max_subscriptions: int,
) -> list[CandleSubscription]:
    enabled_timeframes = effective_enabled_timeframes(db, settings)
    monitoring_timeframes = effective_monitoring_only_timeframes(db)
    markets = (
        db.query(Market)
        .filter(
            Market.active.is_(True),
            Market.market_type == MarketType.PERP.value,
        )
        .order_by(Market.symbol)
        .all()
    )
    market_by_id = {market.id: market for market in markets}
    priority: list[CandleSubscription] = []
    regular: list[CandleSubscription] = []
    seen: set[tuple[int, str]] = set()

    def add(target: list[CandleSubscription], market: Market, timeframe: str) -> None:
        if timeframe not in TIMEFRAME_MS or market.id is None:
            return
        key = (market.id, timeframe)
        if key in seen:
            return
        coin = str((market.metadata_json or {}).get("hl_coin") or market.symbol)
        seen.add(key)
        target.append(CandleSubscription(market.id, coin, timeframe))

    open_positions = (
        db.query(Position)
        .filter(
            Position.status == "open",
            Position.mode.in_([OrderMode.LIVE.value, OrderMode.PAPER.value]),
        )
        .order_by(Position.mode.asc(), Position.opened_at.desc())
        .all()
    )
    for position in open_positions:
        market = market_by_id.get(position.market_id)
        timeframe = _position_timeframe(db, position)
        if market is not None and timeframe is not None:
            add(priority, market, timeframe)

    monitoring_markets = [
        market
        for market in markets
        if market.base in {"BTC", "ETH"}
        or bool((market.metadata_json or {}).get("monitoring_only"))
    ]
    for timeframe in monitoring_timeframes:
        for market in monitoring_markets:
            add(priority, market, timeframe)

    for timeframe in enabled_timeframes:
        for market in markets:
            add(regular, market, timeframe)

    plan = [*priority, *regular]
    if len(plan) > max_subscriptions:
        logger.warning(
            "market_data_ws_subscription_plan_truncated",
            requested=len(plan),
            maximum=max_subscriptions,
            priority=len(priority),
        )
        plan = plan[:max_subscriptions]
    return plan


class ClosedCandleAccumulator:
    def __init__(self) -> None:
        self._current: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_emitted_open_ms: dict[tuple[str, str], int] = {}

    def ingest(
        self,
        subscription: CandleSubscription,
        row: dict[str, Any],
        *,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        try:
            open_ms = int(row["t"])
            close_ms = int(row["T"])
        except (KeyError, TypeError, ValueError):
            return []
        key = subscription.key
        previous = self._current.get(key)
        closed: list[dict[str, Any]] = []
        if previous is not None and int(previous["t"]) < open_ms:
            closed.extend(self._emit_once(subscription, previous))
        if previous is None or int(previous["t"]) <= open_ms:
            self._current[key] = row
        if close_ms < now_ms:
            closed.extend(self._emit_once(subscription, row))
        return closed

    def _emit_once(
        self,
        subscription: CandleSubscription,
        row: dict[str, Any],
    ) -> list[dict[str, Any]]:
        open_ms = int(row["t"])
        if self._last_emitted_open_ms.get(subscription.key, -1) >= open_ms:
            return []
        self._last_emitted_open_ms[subscription.key] = open_ms
        return [row]


def candle_subscription_key_from_ack(message: dict[str, Any]) -> tuple[str, str] | None:
    if message.get("channel") != "subscriptionResponse":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    subscription = data.get("subscription")
    if not isinstance(subscription, dict) or subscription.get("type") != "candle":
        return None
    coin = subscription.get("coin")
    interval = subscription.get("interval")
    if not isinstance(coin, str) or not isinstance(interval, str):
        return None
    return coin, interval


def persist_closed_candles(
    db: Session,
    batch: list[tuple[CandleSubscription, dict[str, Any]]],
) -> int:
    deduped: dict[tuple[int, str, datetime], tuple[CandleSubscription, dict[str, Any]]] = {}
    for subscription, row in batch:
        parsed = parse_hl_candle(row)
        deduped[(subscription.market_id, subscription.timeframe, parsed["open_time"])] = (
            subscription,
            parsed,
        )
    for subscription, parsed in deduped.values():
        candle_stmt = insert(Candle).values(
            market_id=subscription.market_id,
            timeframe=subscription.timeframe,
            source="hyperliquid_ws",
            **parsed,
        )
        candle_stmt = candle_stmt.on_conflict_do_update(
            constraint="uq_candle_market_tf_time",
            set_={
                "open": parsed["open"],
                "high": parsed["high"],
                "low": parsed["low"],
                "close": parsed["close"],
                "volume": parsed["volume"],
                "source": "hyperliquid_ws",
            },
        )
        db.execute(candle_stmt)
        event_stmt = insert(MarketDataEvent).values(
            market_id=subscription.market_id,
            timeframe=subscription.timeframe,
            candle_open_time=parsed["open_time"],
        )
        db.execute(event_stmt.on_conflict_do_nothing(constraint="uq_market_data_event_candle"))
    db.commit()
    return len(deduped)


class HyperliquidMarketDataService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.accumulator = ClosedCandleAccumulator()
        self.queue: asyncio.Queue[tuple[CandleSubscription, dict[str, Any]]] = asyncio.Queue(
            maxsize=20_000
        )
        self.reconnect_count = 0
        self.last_message_at: datetime | None = None
        self.last_persisted_at: datetime | None = None
        self.persisted_candles = 0
        self.last_error: str | None = None
        self.connected = False
        self.ready = False
        self.expected_subscriptions = 0
        self.active_subscriptions = 0
        self.writer_flush_seconds = 1.0
        self.writer_batch_size = 500
        self.reconnect_max_seconds = 60.0

    async def run_forever(self) -> None:
        writer = asyncio.create_task(self._writer_loop())
        delay = 1.0
        try:
            while True:
                try:
                    reconnect = await self._run_connection()
                    delay = 1.0 if reconnect == "plan_changed" else min(delay * 2, 60.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:500]
                    self.reconnect_count += 1
                    logger.exception("market_data_ws_connection_failed", error=self.last_error)
                    await self._write_state(connected=False, ready=False)
                    await asyncio.sleep(delay + random.random())
                    delay = min(delay * 2, self.reconnect_max_seconds)
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            self.connected = False
            self.ready = False
            with contextlib.suppress(Exception):
                await self._write_state(connected=False, ready=False, active=0)

    async def _run_connection(self) -> str:
        plan, config = await asyncio.to_thread(self._load_plan)
        if not bool(config["enabled"]):
            await self._write_state(connected=False, ready=False, expected=0, active=0)
            await asyncio.sleep(30)
            return "disabled"
        by_key = {subscription.key: subscription for subscription in plan}
        fingerprint = tuple(plan)
        config_fingerprint = tuple(sorted(config.items()))
        refresh_seconds = int(config["subscription_refresh_seconds"])
        heartbeat_seconds = int(config["heartbeat_seconds"])
        self.writer_flush_seconds = float(config["writer_flush_seconds"])
        self.writer_batch_size = int(config["writer_batch_size"])
        self.reconnect_max_seconds = float(config["reconnect_max_seconds"])
        acked: set[tuple[str, str]] = set()
        self.connected = True
        self.ready = not plan
        self.expected_subscriptions = len(plan)
        self.active_subscriptions = 0
        self.last_error = None
        try:
            async with connect(
                self.settings.ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_queue=4096,
            ) as websocket:
                for index, subscription in enumerate(plan, start=1):
                    await websocket.send(json.dumps(subscription.payload()))
                    if index % 100 == 0:
                        await asyncio.sleep(0.05)
                await self._write_state(
                    connected=True,
                    ready=self.ready,
                    expected=len(plan),
                    active=0,
                )
                refresh_at = time.monotonic() + refresh_seconds
                state_at = time.monotonic() + heartbeat_seconds

                async def maintenance() -> bool:
                    nonlocal refresh_at, state_at
                    now = time.monotonic()
                    if now >= state_at:
                        await self._write_state(
                            connected=True,
                            ready=self.ready,
                            expected=self.expected_subscriptions,
                            active=self.active_subscriptions,
                        )
                        state_at = now + heartbeat_seconds
                    if now < refresh_at:
                        return False
                    new_plan, new_config = await asyncio.to_thread(self._load_plan)
                    refresh_at = now + refresh_seconds
                    return (
                        tuple(new_plan) != fingerprint
                        or tuple(sorted(new_config.items())) != config_fingerprint
                    )

                while True:
                    timeout = min(heartbeat_seconds, max(0.1, refresh_at - time.monotonic()))
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                    except TimeoutError:
                        await websocket.send(json.dumps({"method": "ping"}))
                        if await maintenance():
                            return "plan_changed"
                        continue
                    self.last_message_at = datetime.now(UTC)
                    message = json.loads(raw)
                    ack_key = candle_subscription_key_from_ack(message)
                    if ack_key is not None:
                        acked.add(ack_key)
                        self.active_subscriptions = len(acked)
                        became_ready = len(acked) == len(plan)
                        if became_ready and not self.ready:
                            self.ready = True
                            self.last_error = None
                            await self._write_state(
                                connected=True,
                                ready=True,
                                expected=len(plan),
                                active=len(acked),
                            )
                        if await maintenance():
                            return "plan_changed"
                        continue
                    channel = message.get("channel")
                    data = message.get("data")
                    if channel == "candle":
                        rows = data if isinstance(data, list) else [data]
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            key = (str(row.get("s")), str(row.get("i")))
                            candle_subscription = by_key.get(key)
                            if candle_subscription is None:
                                continue
                            closed = self.accumulator.ingest(
                                candle_subscription,
                                row,
                                now_ms=int(time.time() * 1000),
                            )
                            for closed_row in closed:
                                await self.queue.put((candle_subscription, closed_row))
                    if await maintenance():
                        return "plan_changed"
        finally:
            self.connected = False
            self.ready = False
            self.active_subscriptions = 0
            with contextlib.suppress(Exception):
                await self._write_state(
                    connected=False,
                    ready=False,
                    expected=len(plan),
                    active=0,
                )

    def _load_plan(self) -> tuple[list[CandleSubscription], dict[str, int | bool]]:
        db = SessionLocal()
        try:
            config = effective_market_data_ws_settings(db)
            plan = build_candle_subscription_plan(
                db,
                self.settings,
                max_subscriptions=int(config["max_subscriptions"]),
            )
            return plan, config
        finally:
            db.close()

    async def _writer_loop(self) -> None:
        while True:
            first = await self.queue.get()
            batch = [first]
            deadline = time.monotonic() + self.writer_flush_seconds
            while len(batch) < self.writer_batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), timeout=remaining))
                except TimeoutError:
                    break
            retry_delay = 1.0
            try:
                while True:
                    try:
                        persisted = await asyncio.to_thread(self._persist_batch, batch)
                        self.persisted_candles += persisted
                        self.last_persisted_at = datetime.now(UTC)
                        self.last_error = None
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_error = f"writer: {str(exc)[:480]}"
                        logger.exception(
                            "market_data_ws_writer_failed",
                            error=self.last_error,
                            batch_size=len(batch),
                        )
                        with contextlib.suppress(Exception):
                            await self._write_state(
                                connected=self.connected,
                                ready=False,
                                expected=self.expected_subscriptions,
                                active=self.active_subscriptions,
                            )
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, self.reconnect_max_seconds)
            finally:
                for _ in batch:
                    self.queue.task_done()

    @staticmethod
    def _persist_batch(batch: list[tuple[CandleSubscription, dict[str, Any]]]) -> int:
        db = SessionLocal()
        try:
            return persist_closed_candles(db, batch)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def _write_state(
        self,
        *,
        connected: bool,
        ready: bool,
        expected: int | None = None,
        active: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._write_state_sync,
            connected,
            ready,
            expected,
            active,
        )

    def _write_state_sync(
        self,
        connected: bool,
        ready: bool,
        expected: int | None,
        active: int | None,
    ) -> None:
        db = SessionLocal()
        try:
            row = db.get(AppSetting, MARKET_DATA_WS_STATE_KEY)
            previous = row.value if row is not None and isinstance(row.value, dict) else {}
            pending_events = (
                db.query(func.count(MarketDataEvent.id))
                .filter(MarketDataEvent.processed_at.is_(None))
                .scalar()
                or 0
            )
            value = {
                **previous,
                "connected": connected,
                "ready": ready,
                "expected_subscriptions": (
                    expected if expected is not None else previous.get("expected_subscriptions", 0)
                ),
                "active_subscriptions": (
                    active if active is not None else previous.get("active_subscriptions", 0)
                ),
                "last_message_at": (
                    self.last_message_at.isoformat() if self.last_message_at else None
                ),
                "last_persisted_at": (
                    self.last_persisted_at.isoformat() if self.last_persisted_at else None
                ),
                "persisted_candles": self.persisted_candles,
                "pending_events": int(pending_events),
                "writer_queue_depth": self.queue.qsize(),
                "reconnect_count": self.reconnect_count,
                "last_error": self.last_error,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if row is None:
                db.add(AppSetting(key=MARKET_DATA_WS_STATE_KEY, value=value))
            else:
                row.value = value
                db.add(row)
            db.commit()
        finally:
            db.close()

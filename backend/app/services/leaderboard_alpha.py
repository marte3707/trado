from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    AppSetting,
    LeaderboardCopyPaperPosition,
    LeaderboardTrader,
    LeaderboardTraderFill,
    LeaderboardTraderSnapshot,
)
from app.services.hyperliquid import HyperliquidClient, HyperliquidTransientError
from app.services.hyperliquid_rate_limit import available_hyperliquid_info_request_capacity

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
LEADERBOARD_ALPHA_STATE_KEY = "leaderboard_alpha_state"
LEADERBOARD_STATS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LEADERBOARD_AUTO_SOURCE = "hyperliquid_leaderboard_auto"

DEFAULT_LEADERBOARD_ALPHA_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "mode": "observe_only",
    "lookback_days": 30,
    "recent_signal_hours": 24,
    "min_traders_for_signal": 1,
    "min_quality_score": 55,
    "max_entry_lag_pct": 0.35,
    "sync_limit_per_run": 5,
    "auto_import_top_traders": True,
    "auto_import_limit": 20,
    "auto_import_window": "week",
    "auto_import_min_volume_usd": "0",
    "auto_import_refresh_hours": 4,
    "auto_sync_enabled": True,
    "auto_sync_interval_hours": 4,
    "include_auto_imported_in_signals": True,
    "copy_paper_enabled": True,
    "copy_paper_starting_equity_usd": "10000",
    "copy_paper_position_notional_usd": "100",
    "copy_paper_fee_rate": "0.00045",
    "copy_paper_min_source_notional_usd": "25",
    "copy_paper_max_open_positions": 80,
    "copy_paper_max_open_positions_per_trader": 10,
    "copy_paper_allowed_rank_limit": 20,
    "copy_paper_only_auto_top_traders": True,
    "copy_paper_close_on_opposite_open": True,
    "copy_paper_replay_batch_size": 1000,
}


@dataclass(frozen=True)
class SyncResult:
    trader_id: int
    address: str
    fetched: int
    inserted: int
    snapshot: LeaderboardTraderSnapshot | None
    status: str = "ok"
    reason: str | None = None


def normalize_address(address: str) -> str:
    value = address.strip()
    if not ADDRESS_RE.match(value):
        raise ValueError("Indirizzo Hyperliquid non valido")
    return value.lower()


def leaderboard_alpha_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, "leaderboard_alpha")
    if row is None or not isinstance(row.value, dict):
        return dict(DEFAULT_LEADERBOARD_ALPHA_SETTINGS)
    return {**DEFAULT_LEADERBOARD_ALPHA_SETTINGS, **row.value}


def leaderboard_alpha_state(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, LEADERBOARD_ALPHA_STATE_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _store_leaderboard_alpha_state(db: Session, value: dict[str, Any]) -> None:
    row = db.get(AppSetting, LEADERBOARD_ALPHA_STATE_KEY)
    if row is None:
        db.add(AppSetting(key=LEADERBOARD_ALPHA_STATE_KEY, value=value))
    else:
        row.value = value
        db.add(row)


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


def _int_setting(value: Any, default: int, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _decimal_setting(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    parsed = _to_decimal(value)
    return parsed if parsed is not None else default


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decimal_sum(values: Sequence[Decimal | None]) -> Decimal:
    total = Decimal("0")
    for value in values:
        if value is not None:
            total += value
    return total


def _decimal_avg(values: Sequence[Decimal | None]) -> Decimal | None:
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return _decimal_sum(valid) / Decimal(len(valid))


def _dt_from_ms(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def _short_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _pct(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value * Decimal("100"))


def _safe_json_payload(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return json.loads(json.dumps(value, default=str))


def _leaderboard_window_performance(row: dict[str, Any], window: str) -> dict[str, Any]:
    performances = row.get("windowPerformances")
    if isinstance(performances, dict):
        candidate = performances.get(window)
        return candidate if isinstance(candidate, dict) else {}
    if not isinstance(performances, list):
        return {}
    for item in performances:
        if (
            isinstance(item, (list, tuple))
            and len(item) >= 2
            and item[0] == window
            and isinstance(item[1], dict)
        ):
            return item[1]
    return {}


def select_top_leaderboard_traders(
    rows: Sequence[dict[str, Any]],
    *,
    limit: int,
    window: str = "week",
    min_volume_usd: Decimal = Decimal("0"),
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        address_raw = row.get("ethAddress") or row.get("address")
        if not isinstance(address_raw, str) or not ADDRESS_RE.match(address_raw):
            continue
        performance = _leaderboard_window_performance(row, window)
        roi = _to_decimal(performance.get("roi"))
        volume = _to_decimal(performance.get("vlm") or performance.get("volume"))
        if roi is None or volume is None or volume <= min_volume_usd:
            continue
        selected.append(
            {
                "address": address_raw.lower(),
                "display_name": row.get("displayName"),
                "account_value": _to_decimal(row.get("accountValue")),
                "window": window,
                "roi": roi,
                "pnl": _to_decimal(performance.get("pnl")),
                "volume": volume,
            }
        )

    selected.sort(key=lambda item: item["roi"], reverse=True)
    top = selected[: max(1, limit)]
    for index, item in enumerate(top, start=1):
        item["rank"] = index
    return top


async def fetch_hyperliquid_leaderboard_rows() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(LEADERBOARD_STATS_URL)
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("leaderboardRows") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


async def import_top_leaderboard_traders(
    db: Session,
    *,
    limit: int = 20,
    window: str = "week",
    min_volume_usd: Decimal = Decimal("0"),
    rows: Sequence[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    source_rows = list(rows) if rows is not None else await fetch_hyperliquid_leaderboard_rows()
    top_traders = select_top_leaderboard_traders(
        source_rows,
        limit=limit,
        window=window,
        min_volume_usd=min_volume_usd,
    )
    addresses = [row["address"] for row in top_traders]
    existing = {
        row.address: row
        for row in db.query(LeaderboardTrader)
        .filter(LeaderboardTrader.address.in_(addresses or [""]))
        .all()
    }

    inserted = 0
    updated = 0
    for item in top_traders:
        trader = existing.get(item["address"])
        leaderboard_meta = {
            "rank": item["rank"],
            "window": item["window"],
            "roi": _short_decimal(item["roi"]),
            "roi_pct": _pct(item["roi"]),
            "pnl": _short_decimal(item["pnl"]),
            "volume_usd": _short_decimal(item["volume"]),
            "account_value": _short_decimal(item["account_value"]),
            "display_name": item["display_name"] if isinstance(item["display_name"], str) else None,
            "source_url": LEADERBOARD_STATS_URL,
            "imported_at": now.isoformat(),
        }
        if trader is None:
            label = item["display_name"] if isinstance(item["display_name"], str) else None
            if not label:
                label = f"HL ROI 7d #{item['rank']}"
            trader = LeaderboardTrader(
                address=item["address"],
                label=label,
                source=LEADERBOARD_AUTO_SOURCE,
                enabled=True,
                metadata_json={"leaderboard": leaderboard_meta},
            )
            db.add(trader)
            existing[item["address"]] = trader
            inserted += 1
            continue

        metadata = dict(trader.metadata_json or {})
        metadata["leaderboard"] = leaderboard_meta
        trader.metadata_json = metadata
        if not trader.label and isinstance(item["display_name"], str):
            trader.label = item["display_name"]
        if not trader.source:
            trader.source = LEADERBOARD_AUTO_SOURCE
        db.add(trader)
        updated += 1

    return {
        "status": "ok",
        "source_rows": len(source_rows),
        "selected": len(top_traders),
        "inserted": inserted,
        "updated": updated,
        "window": window,
        "min_volume_usd": _short_decimal(min_volume_usd),
    }


def _fill_key(raw: dict[str, Any]) -> str:
    candidates = [
        raw.get("hash"),
        raw.get("tid"),
        raw.get("oid"),
        raw.get("cloid"),
    ]
    stable_parts = [str(item) for item in candidates if item not in (None, "")]
    stable_parts.extend(
        str(raw.get(key) or "")
        for key in ("time", "coin", "side", "dir", "px", "sz", "closedPnl", "fee")
    )
    digest = hashlib.sha256("|".join(stable_parts).encode("utf-8")).hexdigest()
    prefix = str(raw.get("hash") or raw.get("tid") or raw.get("oid") or "fill")
    key = f"{prefix}:{digest[:24]}"
    return key[:128]


def _normalize_side(raw_side: Any) -> str | None:
    side = str(raw_side or "").strip().upper()
    if side in {"B", "BUY"}:
        return "buy"
    if side in {"A", "ASK", "SELL"}:
        return "sell"
    return None


def _normalize_intent(raw_dir: Any) -> str | None:
    direction = str(raw_dir or "").lower()
    if "open" in direction:
        return "open"
    if "close" in direction:
        return "close"
    return None


def _normalize_direction(raw_dir: Any) -> str | None:
    direction = str(raw_dir or "").lower()
    if "long" in direction:
        return "long"
    if "short" in direction:
        return "short"
    return None


def _normalized_fill(raw: dict[str, Any]) -> dict[str, Any] | None:
    filled_at = _dt_from_ms(raw.get("time"))
    coin = str(raw.get("coin") or "").strip()
    if not filled_at or not coin:
        return None
    price = _to_decimal(raw.get("px"))
    size = _to_decimal(raw.get("sz"))
    notional = price * size if price is not None and size is not None else None
    return {
        "fill_key": _fill_key(raw),
        "coin": coin,
        "side": _normalize_side(raw.get("side")),
        "direction": _normalize_direction(raw.get("dir")),
        "intent": _normalize_intent(raw.get("dir")),
        "price": price,
        "size": size,
        "notional_usd": notional,
        "closed_pnl": _to_decimal(raw.get("closedPnl")),
        "fee": _to_decimal(raw.get("fee")),
        "crossed": raw.get("crossed") if isinstance(raw.get("crossed"), bool) else None,
        "oid": str(raw.get("oid")) if raw.get("oid") not in (None, "") else None,
        "raw_data": _safe_json_payload(raw),
        "filled_at": filled_at,
    }


def _quality_score(
    *,
    fills_count: int,
    closing_count: int,
    net_pnl: Decimal,
    win_rate: Decimal | None,
    profit_factor: Decimal | None,
    top_coin_share: Decimal | None,
    crossed_share: Decimal | None,
) -> int:
    if fills_count < 8 or closing_count < 3:
        return max(0, min(40, fills_count * 3 + closing_count * 5))

    score = Decimal("45")
    if net_pnl > 0:
        score += Decimal("15")
    elif net_pnl < 0:
        score -= Decimal("15")

    if profit_factor is None:
        score += Decimal("5")
    else:
        score += max(Decimal("-15"), min(Decimal("20"), (profit_factor - Decimal("1")) * Decimal("20")))

    if win_rate is not None:
        score += max(Decimal("-10"), min(Decimal("15"), (win_rate - Decimal("0.45")) * Decimal("60")))

    score += min(Decimal("10"), Decimal(fills_count) / Decimal("6"))

    if top_coin_share is not None:
        if top_coin_share >= Decimal("0.90"):
            score -= Decimal("20")
        elif top_coin_share >= Decimal("0.70"):
            score -= Decimal("10")

    if crossed_share is not None and crossed_share >= Decimal("0.85"):
        score -= Decimal("5")

    bounded = max(Decimal("0"), min(Decimal("100"), score.to_integral_value()))
    return int(bounded)


def _classification(
    *,
    fills_count: int,
    closing_count: int,
    net_pnl: Decimal,
    profit_factor: Decimal | None,
    quality_score: int,
) -> str:
    if fills_count < 8 or closing_count < 3:
        return "insufficient"
    if net_pnl < 0 or (profit_factor is not None and profit_factor < Decimal("0.90")):
        return "quarantine"
    if quality_score >= 70 and (profit_factor is None or profit_factor >= Decimal("1.25")):
        return "candidate"
    return "watch"


def compute_trader_snapshot(
    db: Session,
    trader_id: int,
    *,
    lookback_days: int = 30,
    now: datetime | None = None,
) -> LeaderboardTraderSnapshot:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=lookback_days)
    fills = (
        db.query(LeaderboardTraderFill)
        .filter(
            LeaderboardTraderFill.trader_id == trader_id,
            LeaderboardTraderFill.filled_at >= cutoff,
        )
        .order_by(LeaderboardTraderFill.filled_at.asc())
        .all()
    )

    closed_pnl = _decimal_sum([row.closed_pnl for row in fills])
    fees_abs = _decimal_sum([abs(row.fee) for row in fills if row.fee is not None])
    volume = _decimal_sum([row.notional_usd for row in fills])
    net_pnl = closed_pnl - fees_abs

    closing_fills = [row for row in fills if (row.closed_pnl or Decimal("0")) != 0]
    open_fills = [row for row in fills if row.intent == "open"]
    wins = [row.closed_pnl for row in closing_fills if row.closed_pnl is not None and row.closed_pnl > 0]
    losses = [row.closed_pnl for row in closing_fills if row.closed_pnl is not None and row.closed_pnl < 0]
    gross_profit = _decimal_sum(wins)
    gross_loss = abs(_decimal_sum(losses))
    win_rate = Decimal(len(wins)) / Decimal(len(closing_fills)) if closing_fills else None
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    notional_by_coin: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in fills:
        if row.notional_usd is not None:
            notional_by_coin[row.coin] += row.notional_usd
    top_coin = max(notional_by_coin.items(), key=lambda item: item[1], default=(None, Decimal("0")))
    top_coin_share = top_coin[1] / volume if volume > 0 else None
    crossed_count = len([row for row in fills if row.crossed is True])
    crossed_share = Decimal(crossed_count) / Decimal(len(fills)) if fills else None
    long_open_notional = _decimal_sum(
        [row.notional_usd for row in open_fills if row.direction == "long"]
    )
    short_open_notional = _decimal_sum(
        [row.notional_usd for row in open_fills if row.direction == "short"]
    )

    quality_score = _quality_score(
        fills_count=len(fills),
        closing_count=len(closing_fills),
        net_pnl=net_pnl,
        win_rate=win_rate,
        profit_factor=profit_factor,
        top_coin_share=top_coin_share,
        crossed_share=crossed_share,
    )
    classification = _classification(
        fills_count=len(fills),
        closing_count=len(closing_fills),
        net_pnl=net_pnl,
        profit_factor=profit_factor,
        quality_score=quality_score,
    )

    snapshot = LeaderboardTraderSnapshot(
        trader_id=trader_id,
        lookback_days=lookback_days,
        fills_count=len(fills),
        closing_fills_count=len(closing_fills),
        open_fills_count=len(open_fills),
        net_pnl_usdc=net_pnl,
        closed_pnl_usdc=closed_pnl,
        fees_usdc=fees_abs,
        volume_usdc=volume,
        win_rate=win_rate,
        profit_factor=profit_factor,
        quality_score=quality_score,
        classification=classification,
        metrics={
            "wins": len(wins),
            "losses": len(losses),
            "gross_profit_usdc": _short_decimal(gross_profit),
            "gross_loss_usdc": _short_decimal(gross_loss),
            "top_coin": top_coin[0],
            "top_coin_share": float(top_coin_share) if top_coin_share is not None else None,
            "crossed_share": float(crossed_share) if crossed_share is not None else None,
            "long_open_notional_usdc": _short_decimal(long_open_notional),
            "short_open_notional_usdc": _short_decimal(short_open_notional),
            "last_fill_at": fills[-1].filled_at.isoformat() if fills else None,
        },
    )
    db.add(snapshot)
    return snapshot


async def sync_trader(
    db: Session,
    trader: LeaderboardTrader,
    *,
    client: HyperliquidClient | None = None,
    lookback_days: int = 30,
    now: datetime | None = None,
) -> SyncResult:
    now = now or datetime.now(UTC)
    client = client or HyperliquidClient(get_settings())
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=lookback_days)).timestamp() * 1000)
    if available_hyperliquid_info_request_capacity(db, request_type="userFillsByTime") <= 0:
        trader.last_sync_status = "rate_limited"
        trader.last_sync_error = "ip_weight_budget: local governor has no userFillsByTime capacity"
        return SyncResult(
            trader_id=trader.id,
            address=trader.address,
            fetched=0,
            inserted=0,
            snapshot=None,
            status="rate_limited",
            reason="ip_weight_budget",
        )

    trader.last_sync_error = None
    try:
        raw_fills = await client.user_fills_by_time(
            trader.address,
            start_ms,
            end_ms,
            aggregate_by_time=False,
        )
    except HyperliquidTransientError as exc:
        if "rate-limit governor skipped" in str(exc):
            trader.last_sync_status = "rate_limited"
            trader.last_sync_error = str(exc)[:500]
            return SyncResult(
                trader_id=trader.id,
                address=trader.address,
                fetched=0,
                inserted=0,
                snapshot=None,
                status="rate_limited",
                reason="ip_weight_budget",
            )
        raise
    normalized = [
        item for item in (_normalized_fill(row) for row in raw_fills if isinstance(row, dict)) if item
    ]
    existing_keys = {
        key
        for (key,) in db.query(LeaderboardTraderFill.fill_key)
        .filter(
            LeaderboardTraderFill.trader_id == trader.id,
            LeaderboardTraderFill.fill_key.in_([row["fill_key"] for row in normalized] or [""]),
        )
        .all()
    }

    inserted = 0
    latest_seen = trader.last_seen_fill_at
    for payload in normalized:
        if payload["fill_key"] in existing_keys:
            continue
        db.add(LeaderboardTraderFill(trader_id=trader.id, **payload))
        inserted += 1
        if latest_seen is None or payload["filled_at"] > latest_seen:
            latest_seen = payload["filled_at"]

    trader.last_seen_fill_at = latest_seen
    trader.last_sync_status = "ok"
    trader.last_sync_at = now
    snapshot = compute_trader_snapshot(
        db,
        trader.id,
        lookback_days=lookback_days,
        now=now,
    )
    db.flush()
    return SyncResult(
        trader_id=trader.id,
        address=trader.address,
        fetched=len(normalized),
        inserted=inserted,
        snapshot=snapshot,
    )


async def sync_enabled_traders(
    db: Session,
    *,
    limit: int,
    lookback_days: int,
    client: HyperliquidClient | None = None,
    now: datetime | None = None,
    due_before: datetime | None = None,
) -> list[SyncResult]:
    query = db.query(LeaderboardTrader).filter(LeaderboardTrader.enabled.is_(True))
    if due_before is not None:
        query = query.filter(
            or_(
                LeaderboardTrader.last_sync_at.is_(None),
                LeaderboardTrader.last_sync_at <= due_before,
            )
        )
    capacity = available_hyperliquid_info_request_capacity(db, request_type="userFillsByTime")
    effective_limit = min(max(1, limit), max(0, capacity))
    if effective_limit <= 0:
        return []
    traders = (
        query.order_by(LeaderboardTrader.last_sync_at.asc().nullsfirst(), LeaderboardTrader.id.asc())
        .limit(effective_limit)
        .all()
    )
    results: list[SyncResult] = []
    for trader in traders:
        try:
            result = await sync_trader(
                db,
                trader,
                client=client,
                lookback_days=lookback_days,
                now=now,
            )
            results.append(result)
            if result.status == "rate_limited":
                break
        except Exception as exc:
            trader.last_sync_at = now or datetime.now(UTC)
            trader.last_sync_status = "error"
            trader.last_sync_error = str(exc)[:500]
    return results


def _enabled_due_trader_count(db: Session, due_before: datetime) -> int:
    return (
        db.query(LeaderboardTrader)
        .filter(
            LeaderboardTrader.enabled.is_(True),
            or_(
                LeaderboardTrader.last_sync_at.is_(None),
                LeaderboardTrader.last_sync_at <= due_before,
            ),
        )
        .count()
    )


def _normalize_rate_limited_sync_errors(db: Session) -> int:
    rows = (
        db.query(LeaderboardTrader)
        .filter(
            LeaderboardTrader.last_sync_status == "error",
            LeaderboardTrader.last_sync_error.contains("rate-limit governor skipped"),
        )
        .all()
    )
    for trader in rows:
        trader.last_sync_at = None
        trader.last_sync_status = "rate_limited"
        trader.last_sync_error = "ip_weight_budget: local governor skipped previous userFillsByTime sync"
        db.add(trader)
    return len(rows)


def _leaderboard_auto_import_due(
    state: dict[str, Any],
    *,
    now: datetime,
    refresh_hours: int,
) -> bool:
    last_import_at = _parse_dt(state.get("last_auto_import_at"))
    return last_import_at is None or now - last_import_at >= timedelta(hours=refresh_hours)


async def run_leaderboard_alpha_maintenance(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = leaderboard_alpha_settings(db)
    if not settings.get("enabled", True):
        return {"status": "disabled", "reason": "leaderboard_alpha_disabled"}

    state = leaderboard_alpha_state(db)
    state["last_maintenance_at"] = now.isoformat()
    result: dict[str, Any] = {
        "status": "skipped",
        "mode": settings.get("mode", "observe_only"),
        "auto_import": None,
        "auto_sync": None,
    }
    normalized_rate_limited = _normalize_rate_limited_sync_errors(db)
    if normalized_rate_limited:
        result["normalized_rate_limited_errors"] = normalized_rate_limited

    if settings.get("auto_import_top_traders", True):
        refresh_hours = _int_setting(settings.get("auto_import_refresh_hours"), 4)
        if _leaderboard_auto_import_due(state, now=now, refresh_hours=refresh_hours):
            import_result = await import_top_leaderboard_traders(
                db,
                limit=_int_setting(settings.get("auto_import_limit"), 20),
                window=str(settings.get("auto_import_window") or "week"),
                min_volume_usd=_decimal_setting(settings.get("auto_import_min_volume_usd")),
                now=now,
            )
            state["last_auto_import_at"] = now.isoformat()
            result["auto_import"] = import_result
            result["status"] = "ok"

    if settings.get("auto_sync_enabled", True):
        interval_hours = _int_setting(settings.get("auto_sync_interval_hours"), 4)
        due_before = now - timedelta(hours=interval_hours)
        due_count = _enabled_due_trader_count(db, due_before)
        capacity = available_hyperliquid_info_request_capacity(db, request_type="userFillsByTime")
        if due_count > 0 and capacity <= 0:
            result["auto_sync"] = {
                "status": "rate_limited",
                "due_traders": due_count,
                "capacity": capacity,
                "reason": "ip_weight_budget",
            }
            result["status"] = "rate_limited" if result["status"] == "skipped" else result["status"]
        elif due_count > 0:
            sync_limit = min(_int_setting(settings.get("sync_limit_per_run"), 5), capacity)
            sync_results = await sync_enabled_traders(
                db,
                limit=sync_limit,
                lookback_days=_int_setting(settings.get("lookback_days"), 30),
                now=now,
                due_before=due_before,
            )
            rate_limited = len([row for row in sync_results if row.status == "rate_limited"])
            ok_results = [row for row in sync_results if row.status == "ok"]
            result["auto_sync"] = {
                "status": "rate_limited" if rate_limited else "ok",
                "due_traders": due_count,
                "capacity": capacity,
                "synced": len(ok_results),
                "rate_limited": rate_limited,
                "inserted": sum(row.inserted for row in ok_results),
            }
            result["status"] = "ok" if ok_results or result["status"] == "ok" else "rate_limited"
            if ok_results:
                state["last_auto_sync_at"] = now.isoformat()
        else:
            result["auto_sync"] = {
                "status": "not_due",
                "due_traders": 0,
                "capacity": capacity,
            }

    if settings.get("copy_paper_enabled", True):
        result["copy_paper"] = run_copy_paper_replay(db, settings=settings, now=now)

    _store_leaderboard_alpha_state(db, state)
    result["state"] = state
    return result


def _leaderboard_rank(metadata: dict[str, Any] | None) -> int | None:
    if not isinstance(metadata, dict):
        return None
    leaderboard = metadata.get("leaderboard")
    if not isinstance(leaderboard, dict):
        return None
    rank_raw = leaderboard.get("rank")
    if rank_raw is None:
        return None
    try:
        return int(rank_raw)
    except (TypeError, ValueError):
        return None


def _eligible_copy_trader_ids(
    db: Session,
    *,
    settings: dict[str, Any],
    snapshots: dict[int, LeaderboardTraderSnapshot] | None = None,
) -> set[int]:
    snapshots = snapshots if snapshots is not None else _latest_snapshots(db)
    rank_limit = _int_setting(settings.get("copy_paper_allowed_rank_limit"), 20)
    only_auto_top = bool(settings.get("copy_paper_only_auto_top_traders", True))
    traders = db.query(LeaderboardTrader).filter(LeaderboardTrader.enabled.is_(True)).all()
    eligible: set[int] = set()
    for trader in traders:
        if only_auto_top:
            rank = _leaderboard_rank(trader.metadata_json)
            if rank is None or rank > rank_limit:
                continue
        snapshot = snapshots.get(trader.id)
        if snapshot is not None and snapshot.classification == "quarantine":
            continue
        eligible.add(trader.id)
    return eligible


def _copy_paper_open_order_exists(db: Session, fill: LeaderboardTraderFill) -> bool:
    query = db.query(LeaderboardCopyPaperPosition.id).filter(
        LeaderboardCopyPaperPosition.trader_id == fill.trader_id,
        LeaderboardCopyPaperPosition.coin == fill.coin,
        LeaderboardCopyPaperPosition.direction == fill.direction,
    )
    if fill.oid:
        query = query.filter(LeaderboardCopyPaperPosition.source_open_order_id == fill.oid)
    else:
        query = query.filter(LeaderboardCopyPaperPosition.open_fill_id == fill.id)
    return query.first() is not None


def _copy_paper_close_order_used(db: Session, fill: LeaderboardTraderFill) -> bool:
    if not fill.oid:
        return (
            db.query(LeaderboardCopyPaperPosition.id)
            .filter(LeaderboardCopyPaperPosition.close_fill_id == fill.id)
            .first()
            is not None
        )
    return (
        db.query(LeaderboardCopyPaperPosition.id)
        .filter(
            LeaderboardCopyPaperPosition.trader_id == fill.trader_id,
            LeaderboardCopyPaperPosition.coin == fill.coin,
            LeaderboardCopyPaperPosition.direction == fill.direction,
            LeaderboardCopyPaperPosition.source_close_order_id == fill.oid,
        )
        .first()
        is not None
    )


def _copy_paper_open_count(db: Session, *, trader_id: int | None = None) -> int:
    query = db.query(LeaderboardCopyPaperPosition).filter(
        LeaderboardCopyPaperPosition.status == "open"
    )
    if trader_id is not None:
        query = query.filter(LeaderboardCopyPaperPosition.trader_id == trader_id)
    return query.count()


def _copy_paper_position_notional(
    fill: LeaderboardTraderFill,
    *,
    target_notional: Decimal,
) -> Decimal:
    if fill.notional_usd is not None and fill.notional_usd > 0:
        return min(target_notional, fill.notional_usd)
    return target_notional


def _open_copy_paper_position(
    db: Session,
    fill: LeaderboardTraderFill,
    *,
    settings: dict[str, Any],
) -> LeaderboardCopyPaperPosition | None:
    if fill.price is None or fill.price <= 0 or fill.direction not in {"long", "short"}:
        return None
    if _copy_paper_open_order_exists(db, fill):
        return None

    max_open = _int_setting(settings.get("copy_paper_max_open_positions"), 80)
    max_per_trader = _int_setting(settings.get("copy_paper_max_open_positions_per_trader"), 10)
    if _copy_paper_open_count(db) >= max_open:
        return None
    if _copy_paper_open_count(db, trader_id=fill.trader_id) >= max_per_trader:
        return None

    target_notional = _decimal_setting(settings.get("copy_paper_position_notional_usd"), Decimal("100"))
    fee_rate = _decimal_setting(settings.get("copy_paper_fee_rate"), Decimal("0.00045"))
    copy_notional = _copy_paper_position_notional(fill, target_notional=target_notional)
    if copy_notional <= 0:
        return None
    copy_size = copy_notional / fill.price
    entry_fee = copy_notional * fee_rate
    position = LeaderboardCopyPaperPosition(
        trader_id=fill.trader_id,
        open_fill_id=fill.id,
        coin=fill.coin,
        direction=fill.direction,
        status="open",
        source_open_order_id=fill.oid,
        entry_price=fill.price,
        copy_size=copy_size,
        copy_notional_usd=copy_notional,
        original_notional_usd=fill.notional_usd,
        entry_fee_usdc=entry_fee,
        opened_at=fill.filled_at,
        metadata_json={
            "source": "leaderboard_copy_paper",
            "entry_fill_key": fill.fill_key,
        },
    )
    db.add(position)
    return position


def _close_copy_paper_position(
    position: LeaderboardCopyPaperPosition,
    fill: LeaderboardTraderFill,
    *,
    settings: dict[str, Any],
    reason: str = "source_close",
) -> None:
    if fill.price is None or fill.price <= 0:
        return
    fee_rate = _decimal_setting(settings.get("copy_paper_fee_rate"), Decimal("0.00045"))
    exit_notional = position.copy_size * fill.price
    gross_pnl = (
        (fill.price - position.entry_price) * position.copy_size
        if position.direction == "long"
        else (position.entry_price - fill.price) * position.copy_size
    )
    exit_fee = exit_notional * fee_rate
    net_pnl = gross_pnl - position.entry_fee_usdc - exit_fee
    position.status = "closed"
    position.close_fill_id = fill.id
    position.source_close_order_id = fill.oid
    position.exit_price = fill.price
    position.exit_fee_usdc = exit_fee
    position.gross_pnl_usdc = gross_pnl
    position.net_pnl_usdc = net_pnl
    position.pnl_pct = net_pnl / position.copy_notional_usd if position.copy_notional_usd > 0 else None
    position.closed_at = fill.filled_at
    position.close_reason = reason
    metadata = dict(position.metadata_json or {})
    metadata["exit_fill_key"] = fill.fill_key
    position.metadata_json = metadata


def _close_opposite_copy_positions(
    db: Session,
    fill: LeaderboardTraderFill,
    *,
    settings: dict[str, Any],
) -> int:
    if fill.direction not in {"long", "short"}:
        return 0
    opposite = "short" if fill.direction == "long" else "long"
    rows = (
        db.query(LeaderboardCopyPaperPosition)
        .filter(
            LeaderboardCopyPaperPosition.trader_id == fill.trader_id,
            LeaderboardCopyPaperPosition.coin == fill.coin,
            LeaderboardCopyPaperPosition.direction == opposite,
            LeaderboardCopyPaperPosition.status == "open",
        )
        .order_by(LeaderboardCopyPaperPosition.opened_at.asc(), LeaderboardCopyPaperPosition.id.asc())
        .all()
    )
    for position in rows:
        _close_copy_paper_position(position, fill, settings=settings, reason="opposite_open")
    return len(rows)


def _copy_paper_process_fill(
    db: Session,
    fill: LeaderboardTraderFill,
    *,
    settings: dict[str, Any],
) -> dict[str, int]:
    result = {"opened": 0, "closed": 0, "skipped": 0}
    min_source_notional = _decimal_setting(settings.get("copy_paper_min_source_notional_usd"), Decimal("25"))
    if (
        fill.price is None
        or fill.price <= 0
        or fill.direction not in {"long", "short"}
        or fill.intent not in {"open", "close"}
        or (fill.notional_usd is not None and fill.notional_usd < min_source_notional)
    ):
        result["skipped"] += 1
        return result

    if fill.intent == "open":
        if _copy_paper_open_order_exists(db, fill):
            result["skipped"] += 1
            return result
        max_open = _int_setting(settings.get("copy_paper_max_open_positions"), 80)
        max_per_trader = _int_setting(settings.get("copy_paper_max_open_positions_per_trader"), 10)
        if _copy_paper_open_count(db) >= max_open or _copy_paper_open_count(db, trader_id=fill.trader_id) >= max_per_trader:
            result["skipped"] += 1
            return result
        if settings.get("copy_paper_close_on_opposite_open", True):
            result["closed"] += _close_opposite_copy_positions(db, fill, settings=settings)
        position = _open_copy_paper_position(db, fill, settings=settings)
        result["opened"] += 1 if position is not None else 0
        result["skipped"] += 0 if position is not None else 1
        return result

    if _copy_paper_close_order_used(db, fill):
        result["skipped"] += 1
        return result
    position = (
        db.query(LeaderboardCopyPaperPosition)
        .filter(
            LeaderboardCopyPaperPosition.trader_id == fill.trader_id,
            LeaderboardCopyPaperPosition.coin == fill.coin,
            LeaderboardCopyPaperPosition.direction == fill.direction,
            LeaderboardCopyPaperPosition.status == "open",
        )
        .order_by(LeaderboardCopyPaperPosition.opened_at.asc(), LeaderboardCopyPaperPosition.id.asc())
        .first()
    )
    if position is None:
        result["skipped"] += 1
        return result
    _close_copy_paper_position(position, fill, settings=settings)
    result["closed"] += 1
    return result


def _copy_paper_cursor(state: dict[str, Any]) -> tuple[datetime | None, int]:
    parsed_at = _parse_dt(state.get("last_copy_paper_fill_at"))
    try:
        fill_id = int(state.get("last_copy_paper_fill_id") or 0)
    except (TypeError, ValueError):
        fill_id = 0
    return parsed_at, max(0, fill_id)


def _latest_eligible_fill_cursor(
    db: Session,
    eligible_ids: set[int],
) -> tuple[datetime | None, int]:
    row = (
        db.query(LeaderboardTraderFill)
        .filter(LeaderboardTraderFill.trader_id.in_(eligible_ids or {-1}))
        .order_by(LeaderboardTraderFill.filled_at.desc(), LeaderboardTraderFill.id.desc())
        .first()
    )
    if row is None:
        return None, 0
    return row.filled_at, row.id


def _seed_copy_paper_cursor_if_needed(
    db: Session,
    *,
    state: dict[str, Any],
    eligible_ids: set[int],
) -> tuple[datetime | None, int, bool]:
    cursor_at, cursor_id = _copy_paper_cursor(state)
    if cursor_at is not None or cursor_id > 0:
        return cursor_at, cursor_id, False
    existing_positions = db.query(LeaderboardCopyPaperPosition.id).first() is not None
    if not existing_positions:
        return cursor_at, cursor_id, False
    latest_at, latest_id = _latest_eligible_fill_cursor(db, eligible_ids)
    if latest_at is None:
        return cursor_at, cursor_id, False
    state["last_copy_paper_fill_at"] = latest_at.isoformat()
    state["last_copy_paper_fill_id"] = latest_id
    return latest_at, latest_id, True


def _copy_paper_fills_after_cursor(
    db: Session,
    *,
    eligible_ids: set[int],
    cursor_at: datetime | None,
    cursor_id: int,
    limit: int,
) -> list[LeaderboardTraderFill]:
    query = db.query(LeaderboardTraderFill).filter(LeaderboardTraderFill.trader_id.in_(eligible_ids))
    if cursor_at is not None:
        query = query.filter(
            or_(
                LeaderboardTraderFill.filled_at > cursor_at,
                and_(
                    LeaderboardTraderFill.filled_at == cursor_at,
                    LeaderboardTraderFill.id > cursor_id,
                ),
            )
        )
    elif cursor_id > 0:
        query = query.filter(LeaderboardTraderFill.id > cursor_id)
    return (
        query.order_by(LeaderboardTraderFill.filled_at.asc(), LeaderboardTraderFill.id.asc())
        .limit(max(1, limit))
        .all()
    )


def run_copy_paper_replay(
    db: Session,
    *,
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = settings or leaderboard_alpha_settings(db)
    if not settings.get("copy_paper_enabled", True):
        return {"status": "disabled"}
    eligible_ids = _eligible_copy_trader_ids(db, settings=settings)
    if not eligible_ids:
        return {"status": "skipped", "reason": "no_eligible_traders"}
    state = leaderboard_alpha_state(db)
    cursor_at, cursor_id, seeded_cursor = _seed_copy_paper_cursor_if_needed(
        db,
        state=state,
        eligible_ids=eligible_ids,
    )
    batch_size = _int_setting(settings.get("copy_paper_replay_batch_size"), 1000)
    fills = _copy_paper_fills_after_cursor(
        db,
        eligible_ids=eligible_ids,
        cursor_at=cursor_at,
        cursor_id=cursor_id,
        limit=batch_size,
    )
    result: dict[str, Any] = {
        "status": "ok",
        "eligible_traders": len(eligible_ids),
        "fills_seen": len(fills),
        "batch_size": batch_size,
        "seeded_cursor": seeded_cursor,
        "opened": 0,
        "closed": 0,
        "skipped": 0,
    }
    last_fill = None
    for fill in fills:
        processed = _copy_paper_process_fill(db, fill, settings=settings)
        result["opened"] += processed["opened"]
        result["closed"] += processed["closed"]
        result["skipped"] += processed["skipped"]
        last_fill = fill
        if processed["opened"] or processed["closed"]:
            db.flush()

    state["last_copy_paper_run_at"] = now.isoformat()
    if last_fill is not None:
        state["last_copy_paper_fill_at"] = last_fill.filled_at.isoformat()
        state["last_copy_paper_fill_id"] = last_fill.id
    _store_leaderboard_alpha_state(db, state)
    db.flush()
    result["summary"] = build_copy_paper_summary(db, settings=settings)
    return result


def _latest_snapshots(db: Session) -> dict[int, LeaderboardTraderSnapshot]:
    latest_created = (
        db.query(
            LeaderboardTraderSnapshot.trader_id.label("trader_id"),
            func.max(LeaderboardTraderSnapshot.created_at).label("created_at"),
        )
        .group_by(LeaderboardTraderSnapshot.trader_id)
        .subquery()
    )
    rows = (
        db.query(LeaderboardTraderSnapshot)
        .join(
            latest_created,
            (LeaderboardTraderSnapshot.trader_id == latest_created.c.trader_id)
            & (LeaderboardTraderSnapshot.created_at == latest_created.c.created_at),
        )
        .all()
    )
    return {row.trader_id: row for row in rows}


def serialize_snapshot(snapshot: LeaderboardTraderSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "lookback_days": snapshot.lookback_days,
        "fills_count": snapshot.fills_count,
        "closing_fills_count": snapshot.closing_fills_count,
        "open_fills_count": snapshot.open_fills_count,
        "net_pnl_usdc": _short_decimal(snapshot.net_pnl_usdc),
        "closed_pnl_usdc": _short_decimal(snapshot.closed_pnl_usdc),
        "fees_usdc": _short_decimal(snapshot.fees_usdc),
        "volume_usdc": _short_decimal(snapshot.volume_usdc),
        "win_rate": float(snapshot.win_rate) if snapshot.win_rate is not None else None,
        "win_rate_pct": _pct(snapshot.win_rate),
        "profit_factor": float(snapshot.profit_factor) if snapshot.profit_factor is not None else None,
        "quality_score": snapshot.quality_score,
        "classification": snapshot.classification,
        "metrics": snapshot.metrics,
        "created_at": snapshot.created_at,
    }


def serialize_trader(
    trader: LeaderboardTrader,
    snapshot: LeaderboardTraderSnapshot | None = None,
) -> dict[str, Any]:
    return {
        "id": trader.id,
        "address": trader.address,
        "label": trader.label,
        "source": trader.source,
        "enabled": trader.enabled,
        "notes": trader.notes,
        "last_sync_at": trader.last_sync_at,
        "last_sync_status": trader.last_sync_status,
        "last_sync_error": trader.last_sync_error,
        "last_seen_fill_at": trader.last_seen_fill_at,
        "metadata_json": trader.metadata_json,
        "created_at": trader.created_at,
        "updated_at": trader.updated_at,
        "latest_snapshot": serialize_snapshot(snapshot),
    }


def serialize_fill(row: LeaderboardTraderFill, trader: LeaderboardTrader | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "trader_id": row.trader_id,
        "trader_label": trader.label if trader is not None else None,
        "trader_address": trader.address if trader is not None else None,
        "coin": row.coin,
        "side": row.side,
        "direction": row.direction,
        "intent": row.intent,
        "price": _short_decimal(row.price),
        "size": _short_decimal(row.size),
        "notional_usd": _short_decimal(row.notional_usd),
        "closed_pnl": _short_decimal(row.closed_pnl),
        "fee": _short_decimal(row.fee),
        "crossed": row.crossed,
        "filled_at": row.filled_at,
    }


def serialize_copy_paper_position(
    row: LeaderboardCopyPaperPosition,
    trader: LeaderboardTrader | None = None,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "trader_id": row.trader_id,
        "trader_label": trader.label if trader is not None else None,
        "trader_address": trader.address if trader is not None else None,
        "coin": row.coin,
        "direction": row.direction,
        "status": row.status,
        "entry_price": _short_decimal(row.entry_price),
        "exit_price": _short_decimal(row.exit_price),
        "copy_size": _short_decimal(row.copy_size),
        "copy_notional_usd": _short_decimal(row.copy_notional_usd),
        "original_notional_usd": _short_decimal(row.original_notional_usd),
        "entry_fee_usdc": _short_decimal(row.entry_fee_usdc),
        "exit_fee_usdc": _short_decimal(row.exit_fee_usdc),
        "gross_pnl_usdc": _short_decimal(row.gross_pnl_usdc),
        "net_pnl_usdc": _short_decimal(row.net_pnl_usdc),
        "pnl_pct": _pct(row.pnl_pct),
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "close_reason": row.close_reason,
        "metadata_json": row.metadata_json,
    }


def _profit_factor_from_positions(positions: Sequence[LeaderboardCopyPaperPosition]) -> Decimal | None:
    profits = _decimal_sum(
        [row.net_pnl_usdc for row in positions if row.net_pnl_usdc is not None and row.net_pnl_usdc > 0]
    )
    losses = abs(
        _decimal_sum(
            [row.net_pnl_usdc for row in positions if row.net_pnl_usdc is not None and row.net_pnl_usdc < 0]
        )
    )
    if losses <= 0:
        return None
    return profits / losses


def _copy_paper_trader_rankings(
    db: Session,
    *,
    positions: Sequence[LeaderboardCopyPaperPosition],
) -> list[dict[str, Any]]:
    trader_ids = {row.trader_id for row in positions}
    traders = {
        row.id: row
        for row in db.query(LeaderboardTrader).filter(LeaderboardTrader.id.in_(trader_ids or {-1})).all()
    }
    grouped: dict[int, list[LeaderboardCopyPaperPosition]] = defaultdict(list)
    for position in positions:
        grouped[position.trader_id].append(position)

    rankings: list[dict[str, Any]] = []
    for trader_id, rows in grouped.items():
        closed = [row for row in rows if row.status == "closed"]
        wins = [row for row in closed if row.net_pnl_usdc is not None and row.net_pnl_usdc > 0]
        net_pnl = _decimal_sum([row.net_pnl_usdc for row in closed])
        trader = traders.get(trader_id)
        profit_factor = _profit_factor_from_positions(closed)
        rankings.append(
            {
                "trader_id": trader_id,
                "trader_label": trader.label if trader else None,
                "trader_address": trader.address if trader else None,
                "leaderboard_rank": _leaderboard_rank(trader.metadata_json if trader else None),
                "open_positions": len([row for row in rows if row.status == "open"]),
                "closed_positions": len(closed),
                "net_pnl_usdc": _short_decimal(net_pnl),
                "win_rate_pct": float(Decimal(len(wins)) / Decimal(len(closed)) * Decimal("100")) if closed else None,
                "profit_factor": float(profit_factor) if profit_factor is not None else None,
                "avg_trade_pnl_usdc": _short_decimal(_decimal_avg([row.net_pnl_usdc for row in closed])),
            }
        )
    return sorted(
        rankings,
        key=lambda row: (
            Decimal(str(row["net_pnl_usdc"] or "0")),
            row["closed_positions"],
        ),
        reverse=True,
    )


def build_copy_paper_summary(
    db: Session,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or leaderboard_alpha_settings(db)
    starting_equity = _decimal_setting(settings.get("copy_paper_starting_equity_usd"), Decimal("10000"))
    positions = db.query(LeaderboardCopyPaperPosition).all()
    closed = [row for row in positions if row.status == "closed"]
    open_positions = [row for row in positions if row.status == "open"]
    wins = [row for row in closed if row.net_pnl_usdc is not None and row.net_pnl_usdc > 0]
    realized_pnl = _decimal_sum([row.net_pnl_usdc for row in closed])
    fees = _decimal_sum([row.entry_fee_usdc for row in positions]) + _decimal_sum(
        [row.exit_fee_usdc for row in closed]
    )
    equity = starting_equity + realized_pnl
    profit_factor = _profit_factor_from_positions(closed)
    traders = {
        row.id: row
        for row in db.query(LeaderboardTrader)
        .filter(LeaderboardTrader.id.in_({row.trader_id for row in positions} or {-1}))
        .all()
    }
    recent_closed = (
        db.query(LeaderboardCopyPaperPosition, LeaderboardTrader)
        .join(LeaderboardTrader, LeaderboardTrader.id == LeaderboardCopyPaperPosition.trader_id)
        .filter(LeaderboardCopyPaperPosition.status == "closed")
        .order_by(LeaderboardCopyPaperPosition.closed_at.desc().nullslast(), LeaderboardCopyPaperPosition.id.desc())
        .limit(40)
        .all()
    )
    open_rows = (
        db.query(LeaderboardCopyPaperPosition, LeaderboardTrader)
        .join(LeaderboardTrader, LeaderboardTrader.id == LeaderboardCopyPaperPosition.trader_id)
        .filter(LeaderboardCopyPaperPosition.status == "open")
        .order_by(LeaderboardCopyPaperPosition.opened_at.desc(), LeaderboardCopyPaperPosition.id.desc())
        .limit(40)
        .all()
    )
    return {
        "enabled": bool(settings.get("copy_paper_enabled", True)),
        "summary": {
            "starting_equity_usd": _short_decimal(starting_equity),
            "equity_usd": _short_decimal(equity),
            "realized_pnl_usdc": _short_decimal(realized_pnl),
            "return_pct": _pct(realized_pnl / starting_equity) if starting_equity > 0 else None,
            "open_positions": len(open_positions),
            "closed_positions": len(closed),
            "total_positions": len(positions),
            "win_rate_pct": float(Decimal(len(wins)) / Decimal(len(closed)) * Decimal("100")) if closed else None,
            "profit_factor": float(profit_factor) if profit_factor is not None else None,
            "avg_trade_pnl_usdc": _short_decimal(_decimal_avg([row.net_pnl_usdc for row in closed])),
            "total_fees_usdc": _short_decimal(fees),
            "copied_traders": len({row.trader_id for row in positions}),
            "position_notional_usd": str(settings.get("copy_paper_position_notional_usd") or "100"),
            "fee_rate": str(settings.get("copy_paper_fee_rate") or "0.00045"),
        },
        "open_positions": [serialize_copy_paper_position(position, trader) for position, trader in open_rows],
        "recent_closed_positions": [
            serialize_copy_paper_position(position, trader) for position, trader in recent_closed
        ],
        "trader_rankings": _copy_paper_trader_rankings(db, positions=positions)[:20],
        "by_status": {
            "open": len(open_positions),
            "closed": len(closed),
        },
        "tracked_traders": [
            {
                "trader_id": trader_id,
                "label": trader.label,
                "address": trader.address,
                "leaderboard_rank": _leaderboard_rank(trader.metadata_json),
            }
            for trader_id, trader in traders.items()
        ],
    }


def _leaderboard_signals(
    db: Session,
    *,
    settings: dict[str, Any],
    snapshots: dict[int, LeaderboardTraderSnapshot],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    recent_hours = int(settings.get("recent_signal_hours") or 6)
    min_traders = int(settings.get("min_traders_for_signal") or 2)
    min_quality = int(settings.get("min_quality_score") or 55)
    cutoff = now - timedelta(hours=max(1, recent_hours))
    qualified_ids = {
        trader_id
        for trader_id, snapshot in snapshots.items()
        if snapshot.quality_score >= min_quality and snapshot.classification != "quarantine"
    }
    signal_trader_ids = set(qualified_ids)
    if settings.get("include_auto_imported_in_signals", True):
        auto_limit = _int_setting(settings.get("auto_import_limit"), 20)
        auto_window = str(settings.get("auto_import_window") or "week")
        auto_rows = (
            db.query(LeaderboardTrader.id, LeaderboardTrader.metadata_json)
            .filter(LeaderboardTrader.enabled.is_(True))
            .all()
        )
        for trader_id, metadata in auto_rows:
            leaderboard = metadata.get("leaderboard") if isinstance(metadata, dict) else None
            if not isinstance(leaderboard, dict):
                continue
            if leaderboard.get("window") != auto_window:
                continue
            rank = _int_setting(leaderboard.get("rank"), auto_limit + 1)
            if rank <= auto_limit:
                signal_trader_ids.add(trader_id)

    quarantine_ids = {
        trader_id
        for trader_id, snapshot in snapshots.items()
        if snapshot.classification == "quarantine"
    }
    signal_trader_ids.difference_update(quarantine_ids)
    if not signal_trader_ids:
        return []

    rows = (
        db.query(LeaderboardTraderFill, LeaderboardTrader)
        .join(LeaderboardTrader, LeaderboardTrader.id == LeaderboardTraderFill.trader_id)
        .filter(
            LeaderboardTrader.enabled.is_(True),
            LeaderboardTraderFill.trader_id.in_(signal_trader_ids),
            LeaderboardTraderFill.filled_at >= cutoff,
            LeaderboardTraderFill.intent == "open",
            LeaderboardTraderFill.direction.isnot(None),
        )
        .order_by(LeaderboardTraderFill.filled_at.desc())
        .all()
    )

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for fill, trader in rows:
        key = (fill.coin, fill.direction or "")
        item = grouped.setdefault(
            key,
            {
                "coin": fill.coin,
                "direction": fill.direction,
                "trader_ids": set(),
                "traders": [],
                "fill_count": 0,
                "notional_usd": Decimal("0"),
                "latest_fill_at": fill.filled_at,
                "avg_quality_score": Decimal("0"),
            },
        )
        item["fill_count"] += 1
        item["trader_ids"].add(trader.id)
        if fill.notional_usd is not None:
            item["notional_usd"] += fill.notional_usd
        if fill.filled_at > item["latest_fill_at"]:
            item["latest_fill_at"] = fill.filled_at
        snapshot = snapshots.get(trader.id)
        item["traders"].append(
            {
                "id": trader.id,
                "label": trader.label,
                "address": trader.address,
                "quality_score": snapshot.quality_score if snapshot else None,
                "fill_notional_usd": _short_decimal(fill.notional_usd),
                "filled_at": fill.filled_at,
            }
        )

    signals: list[dict[str, Any]] = []
    for item in grouped.values():
        trader_count = len(item["trader_ids"])
        if trader_count < min_traders:
            continue
        qualities = [
            Decimal(str(row["quality_score"]))
            for row in item["traders"]
            if row.get("quality_score") is not None
        ]
        avg_quality = _decimal_sum(qualities) / Decimal(len(qualities)) if qualities else None
        signals.append(
            {
                "coin": item["coin"],
                "direction": item["direction"],
                "trader_count": trader_count,
                "fill_count": item["fill_count"],
                "notional_usd": _short_decimal(item["notional_usd"]),
                "latest_fill_at": item["latest_fill_at"],
                "avg_quality_score": float(avg_quality) if avg_quality is not None else None,
                "traders": sorted(
                    item["traders"],
                    key=lambda row: (row.get("quality_score") or 0, row.get("filled_at")),
                    reverse=True,
                )[:8],
                "mode": "confirm_only",
                "confirmation_level": "cluster" if trader_count >= 2 else "single",
            }
        )
    return sorted(
        signals,
        key=lambda row: (
            row["trader_count"],
            Decimal(str(row["notional_usd"] or "0")),
            row["latest_fill_at"],
        ),
        reverse=True,
    )[:20]


def build_dashboard(db: Session) -> dict[str, Any]:
    settings = leaderboard_alpha_settings(db)
    state = leaderboard_alpha_state(db)
    traders = (
        db.query(LeaderboardTrader)
        .order_by(LeaderboardTrader.enabled.desc(), LeaderboardTrader.created_at.desc())
        .all()
    )
    snapshots = _latest_snapshots(db)
    recent_rows = (
        db.query(LeaderboardTraderFill, LeaderboardTrader)
        .join(LeaderboardTrader, LeaderboardTrader.id == LeaderboardTraderFill.trader_id)
        .order_by(LeaderboardTraderFill.filled_at.desc())
        .limit(60)
        .all()
    )
    serialized_traders = [serialize_trader(row, snapshots.get(row.id)) for row in traders]
    candidate_count = len(
        [
            row
            for row in serialized_traders
            if (row.get("latest_snapshot") or {}).get("classification") == "candidate"
        ]
    )
    signals = _leaderboard_signals(db, settings=settings, snapshots=snapshots)
    last_sync_at = max(
        [row.last_sync_at for row in traders if row.last_sync_at is not None],
        default=None,
    )
    return {
        "settings": settings,
        "summary": {
            "traders": len(traders),
            "enabled_traders": len([row for row in traders if row.enabled]),
            "candidates": candidate_count,
            "signals": len(signals),
            "last_sync_at": last_sync_at,
            "mode": "observe_only",
        },
        "state": state,
        "copy_paper": build_copy_paper_summary(db, settings=settings),
        "traders": serialized_traders,
        "signals": signals,
        "recent_fills": [serialize_fill(fill, trader) for fill, trader in recent_rows],
    }

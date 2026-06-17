"""Account Hyperliquid: equity history, snapshot, ordini e protezioni."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.api.routers.common import _decimal, _log_and_redact, _string_decimal
from app.core.config import get_settings
from app.core.runtime_settings import (
    effective_live_execution_settings,
)
from app.db.models import (
    AccountSnapshot,
    AppSetting,
    DecisionLog,
    Market,
    Order,
    Position,
)
from app.db.session import get_db
from app.engine.execution import OrderExecutionEngine
from app.services.hyperliquid import (
    HyperliquidClient,
    HyperliquidError,
    HyperliquidTradingClient,
)
from app.services.unified_account_ratio import (
    DEFAULT_UAR_RISK_SETTINGS,
    calculate_unified_account_ratio,
    decimal_or_none,
    quantized_decimal,
)

_logger = structlog.get_logger()

router = APIRouter()

_ACCOUNT_SNAPSHOT_CACHE_TTL_SECONDS = 25.0
_ACCOUNT_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _account_snapshot_cache_key(settings: Any) -> str:
    return f"{settings.hyperliquid_network}:{settings.hyperliquid_account_address or 'none'}"


def _uar_setting_decimal(risk: dict, key: str) -> Decimal:
    default = DEFAULT_UAR_RISK_SETTINGS[key]
    return decimal_or_none(risk.get(key, default)) or Decimal(str(default))


def _redact_address(address: str | None) -> str | None:
    if not address:
        return None
    if len(address) <= 12:
        return address
    return f"{address[:6]}...{address[-4:]}"


def _position_payload(position: dict[str, Any], protection: dict[str, Any] | None = None) -> dict:
    size = _decimal(position.get("szi"))
    payload = {
        "coin": position.get("coin"),
        "side": "long" if size is not None and size > 0 else "short" if size is not None and size < 0 else None,
        "size": str(abs(size)) if size is not None else None,
        "entry_price": _string_decimal(position.get("entryPx")),
        "position_value": _string_decimal(position.get("positionValue")),
        "unrealized_pnl": _string_decimal(position.get("unrealizedPnl")),
        "return_on_equity": _string_decimal(position.get("returnOnEquity")),
        "liquidation_price": _string_decimal(position.get("liquidationPx")),
        "margin_used": _string_decimal(position.get("marginUsed")),
        "leverage": position.get("leverage"),
        "cum_funding": position.get("cumFunding"),
    }
    if protection is not None:
        payload["protection"] = protection
    return payload


def _merge_protection_states(states: list[dict[str, Any]]) -> dict[str, Any]:
    if not states:
        return {
            "status": "untracked",
            "needs_intervention": False,
            "repairable": False,
            "expected_orders": [],
            "active_orders": [],
            "missing_orders": [],
            "rejected_orders": [],
            "issues": [],
            "summary": "No Trado live position linked to this Hyperliquid position",
            "local_position_ids": [],
            "stop_update_failed": False,
            "stop_replacement_attempts": 0,
        }
    expected = sorted({item for state in states for item in state.get("expected_orders", [])})
    active = sorted({item for state in states for item in state.get("active_orders", [])})
    missing = sorted({item for state in states for item in state.get("missing_orders", [])})
    rejected = sorted({item for state in states for item in state.get("rejected_orders", [])})
    issues = [issue for state in states for issue in state.get("issues", [])]
    position_ids = [int(state["local_position_id"]) for state in states if state.get("local_position_id") is not None]
    stop_update_failed = any(bool(state.get("stop_update_failed")) for state in states)
    stop_attempts = max((int(state.get("stop_replacement_attempts") or 0) for state in states), default=0)
    needs_intervention = bool(missing or rejected or stop_update_failed)
    summary_parts: list[str] = []
    if missing:
        summary_parts.append("Missing " + ", ".join(missing))
    if rejected:
        summary_parts.append("Rejected " + ", ".join(rejected))
    if stop_update_failed:
        summary_parts.append("stop update failed")
    return {
        "status": "intervention_required" if needs_intervention else "ok",
        "needs_intervention": needs_intervention,
        "repairable": True,
        "expected_orders": expected,
        "active_orders": active,
        "missing_orders": missing,
        "rejected_orders": rejected,
        "issues": issues,
        "summary": " | ".join(summary_parts) if summary_parts else "All expected protection orders are present",
        "local_position_ids": position_ids,
        "stop_update_failed": stop_update_failed,
        "stop_replacement_attempts": stop_attempts,
    }


def _account_position_for_coin(user_state: dict[str, Any], coin: str) -> dict[str, Any] | None:
    for item in user_state.get("assetPositions", []):
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        if isinstance(position, dict) and position.get("coin") == coin:
            return position
    return None


def _spot_balance_payload(balance: dict[str, Any]) -> dict:
    return {
        "coin": balance.get("coin"),
        "total": _string_decimal(balance.get("total")),
        "hold": _string_decimal(balance.get("hold")),
        "entry_ntl": _string_decimal(balance.get("entryNtl")),
    }


def _predicted_funding_payload(row: Any) -> dict | None:
    if not isinstance(row, list) or len(row) < 2:
        return None
    coin = row[0]
    exchanges = row[1]
    if not isinstance(coin, str) or not isinstance(exchanges, list):
        return None
    hyperliquid = None
    compact_exchanges = []
    for item in exchanges:
        if not isinstance(item, list) or len(item) < 2:
            continue
        exchange = item[0]
        data = item[1] if isinstance(item[1], dict) else {}
        payload = {
            "exchange": exchange,
            "funding_rate": _string_decimal(data.get("fundingRate")),
            "next_funding_time": data.get("nextFundingTime"),
            "funding_interval_hours": data.get("fundingIntervalHours"),
        }
        compact_exchanges.append(payload)
        if exchange == "HlPerp":
            hyperliquid = payload
    return {"coin": coin, "hyperliquid": hyperliquid, "exchanges": compact_exchanges}


def _latest_account_snapshot(db: Session) -> dict | None:
    row = db.query(AccountSnapshot).order_by(AccountSnapshot.timestamp.desc()).first()
    if row is None:
        return None
    return {
        "timestamp": row.timestamp,
        "total_equity": str(row.total_equity) if row.total_equity is not None else None,
        "available_balance": str(row.available_balance) if row.available_balance is not None else None,
        "margin_used": str(row.margin_used) if row.margin_used is not None else None,
        "unrealized_pnl": str(row.unrealized_pnl) if row.unrealized_pnl is not None else None,
    }


async def _fetch_market_meta(client: HyperliquidClient) -> tuple[dict | None, dict | None, str | None]:
    try:
        return await client.meta(), await client.mids(), None
    except Exception as exc:
        return None, None, _log_and_redact("account_snapshot_api_error", exc)


async def _fetch_account_state(
    client: HyperliquidClient,
    address: str | None,
    checked_at: datetime,
) -> tuple[dict | None, dict | None, list[dict], list[dict], str | None]:
    if not address:
        return None, None, [], [], None
    try:
        user_state = await client.user_state(address)
        spot_state = await client.spot_user_state(address)
        open_orders = await client.frontend_open_orders(address)
        end_ms = int(checked_at.timestamp() * 1000)
        day_ago_ms = end_ms - (24 * 60 * 60 * 1000)
        user_funding = await client.user_funding(address, day_ago_ms, end_ms)
        return user_state, spot_state, open_orders, user_funding, None
    except Exception as exc:
        return None, None, [], [], _log_and_redact("account_snapshot_account_error", exc)


async def _fetch_predicted_fundings(client: HyperliquidClient) -> tuple[list[dict], str | None]:
    try:
        raw = await client.predicted_fundings()
        return [
            item
            for item in (_predicted_funding_payload(row) for row in raw)
            if item is not None
        ], None
    except Exception as exc:
        return [], _log_and_redact("account_snapshot_predicted_error", exc)


def _hl_period_for_days(days: int) -> str:
    """Map a day-count to the most appropriate HL portfolio period key."""
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    return "allTime"


def _hl_best_hist(period_map: dict, days: int) -> list:
    """Return the accountValueHistory list that best covers the requested window.

    For windows > 7 days we use allTime (full coverage, ~38 pts).
    For ≤ 7 days we prefer week (higher resolution, ~60 pts) and fall back to allTime.
    For ≤ 1 day we prefer day, then week, then allTime.
    """
    for period in (["day", "week", "allTime"] if days <= 1 else (["week", "allTime"] if days <= 7 else ["allTime", "week"])):
        hist = period_map.get(period, {}).get("accountValueHistory", [])
        if hist:
            return hist
    return []


def _compute_equity_points(raw_pairs: list[tuple[float, float]]) -> list[dict]:
    """Given [(ts_ms, equity), ...] sorted ascending, return point dicts with drawdown."""
    points = []
    peak: float | None = None
    for ts_ms, equity in raw_pairs:
        if equity <= 0:
            continue
        if peak is None or equity > peak:
            peak = equity
        dd = ((peak - equity) / peak * 100) if peak and peak > 0 else 0.0
        ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        points.append({"ts": ts_dt.isoformat(), "equity": round(equity, 4), "drawdown_pct": round(dd, 4)})
    return points


@router.get("/account/equity-history")
async def account_equity_history(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> dict:
    """Return account equity history.

    Primary source: HL portfolio endpoint (authoritative, covers full account value incl. spot).
    Supplementary: local account_snapshots with effective_account_value for recent high-res data.
    """
    settings = get_settings()
    address = settings.hyperliquid_account_address
    since_ms = (datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000

    # ── Fetch from HL ──────────────────────────────────────────────────────────
    hl_pairs: list[tuple[float, float]] = []
    hl_error: str | None = None
    if address:
        try:
            client = HyperliquidClient(settings)
            portfolio_data = await client.portfolio(address)
            period_map: dict[str, list] = {item[0]: item[1] for item in portfolio_data if isinstance(item, list) and len(item) == 2}
            hist = _hl_best_hist(period_map, days)
            hl_pairs = [
                (float(ts), float(val))
                for ts, val in hist
                if float(ts) >= since_ms and float(val) > 0
            ]
            hl_pairs.sort(key=lambda x: x[0])
        except Exception as exc:
            hl_error = str(exc)

    # ── Fallback: local snapshots (new records with effective_account_value) ──
    local_pairs: list[tuple[float, float]] = []
    if not hl_pairs:
        since_dt = datetime.fromtimestamp(since_ms / 1000, tz=UTC)
        rows = (
            db.query(AccountSnapshot)
            .filter(
                AccountSnapshot.timestamp >= since_dt,
                AccountSnapshot.effective_account_value.isnot(None),
            )
            .order_by(AccountSnapshot.timestamp.asc())
            .all()
        )
        local_pairs = [
            (row.timestamp.timestamp() * 1000, float(row.effective_account_value))
            for row in rows
            if row.effective_account_value is not None
        ]

    raw_pairs = hl_pairs if hl_pairs else local_pairs
    points = _compute_equity_points(raw_pairs)

    current_equity = points[-1]["equity"] if points else None
    peak_equity = max((p["equity"] for p in points), default=None)
    max_drawdown_pct = max((p["drawdown_pct"] for p in points), default=0.0)
    return {
        "points": points,
        "current_equity": current_equity,
        "peak_equity": peak_equity,
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "days": days,
        "source": "hyperliquid" if hl_pairs else "local",
        "hl_error": hl_error,
    }


@router.get("/account/snapshot")
async def account_snapshot(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    cache_key = _account_snapshot_cache_key(settings)
    cached = _ACCOUNT_SNAPSHOT_CACHE.get(cache_key)
    if cached is not None:
        cached_at, cached_payload = cached
        if perf_counter() - cached_at <= _ACCOUNT_SNAPSHOT_CACHE_TTL_SECONDS:
            return deepcopy(cached_payload)

    client = HyperliquidClient(settings)
    checked_at = datetime.now(UTC)
    started = perf_counter()
    meta, mids, api_error = await _fetch_market_meta(client)
    api_latency_ms = round((perf_counter() - started) * 1000, 1)

    address = settings.hyperliquid_account_address
    user_state, spot_state, open_orders, user_funding, account_error = await _fetch_account_state(
        client, address, checked_at
    )
    predicted_fundings, predicted_error = await _fetch_predicted_fundings(client)

    margin = (user_state or {}).get("marginSummary", {})
    cross_margin = (user_state or {}).get("crossMarginSummary", {})
    withdrawable = (user_state or {}).get("withdrawable")
    from app.engine.execution import coin_for_market
    from app.services.live_reconciliation import (
        _index_frontend_open_orders,
        protection_state_for_live_position,
    )
    local_live_positions = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.mode == "live", Position.status == "open")
        .all()
    )
    local_positions_by_coin: dict[str, list[Position]] = {}
    for local_position in local_live_positions:
        if local_position.market is None:
            continue
        try:
            coin = coin_for_market(local_position.market)
        except ValueError:
            continue
        local_positions_by_coin.setdefault(coin, []).append(local_position)
    frontend_open_orders_by_coin = _index_frontend_open_orders(open_orders)
    positions = [
        _position_payload(
            item.get("position", {}),
            protection=_merge_protection_states(
                [
                    protection_state_for_live_position(
                        db,
                        local_position,
                        frontend_open_orders_by_coin.get(str(item.get("position", {}).get("coin") or ""), []),
                    )
                    for local_position in local_positions_by_coin.get(str(item.get("position", {}).get("coin") or ""), [])
                ]
            ),
        )
        for item in (user_state or {}).get("assetPositions", [])
        if isinstance(item, dict)
    ]
    spot_balances = [
        _spot_balance_payload(item)
        for item in (spot_state or {}).get("balances", [])
        if isinstance(item, dict)
    ]
    position_coins = {item.get("coin") for item in positions if item.get("coin")}
    relevant_fundings = [
        item for item in predicted_fundings if item.get("coin") in position_coins
    ] or predicted_fundings[:12]

    total_equity = _decimal(margin.get("accountValue"))
    available_balance = _decimal(withdrawable)
    margin_used = _decimal(margin.get("totalMarginUsed"))
    unrealized_pnl = sum(
        (_decimal(position.get("unrealized_pnl")) or Decimal("0") for position in positions),
        Decimal("0"),
    )
    free_spot_usdc = Decimal("0")
    for bal in spot_balances:
        if bal.get("coin") == "USDC":
            total_usdc = _decimal(bal.get("total")) or Decimal("0")
            hold_usdc = _decimal(bal.get("hold")) or Decimal("0")
            free_spot_usdc = max(Decimal("0"), total_usdc - hold_usdc)
            break
    effective_account_value = (total_equity or Decimal("0")) + free_spot_usdc
    risk_setting = db.get(AppSetting, "risk")
    risk_value = risk_setting.value if risk_setting is not None and isinstance(risk_setting.value, dict) else {}
    uar = calculate_unified_account_ratio(
        user_state,
        free_spot_usdc=free_spot_usdc,
        warn_pct=_uar_setting_decimal(risk_value, "unified_account_ratio_warn_pct"),
        block_pct=_uar_setting_decimal(risk_value, "unified_account_ratio_block_pct"),
        derisk_pct=_uar_setting_decimal(risk_value, "unified_account_ratio_derisk_pct"),
    )
    if user_state is not None:
        db.add(
            AccountSnapshot(
                timestamp=checked_at,
                total_equity=total_equity,
                effective_account_value=effective_account_value,
                free_spot_usdc=free_spot_usdc,
                available_balance=available_balance,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl,
                raw_data={
                    "marginSummary": margin,
                    "crossMarginSummary": cross_margin,
                    "withdrawable": withdrawable,
                    "positions": positions,
                },
            )
        )
        db.commit()

    payload = {
        "status": "ok" if api_error is None and account_error is None else "degraded",
        "network": settings.hyperliquid_network,
        "checked_at": checked_at,
        "api": {
            "status": "ok" if api_error is None else "error",
            "latency_ms": api_latency_ms,
            "info_url": settings.info_url,
            "ws_url": settings.ws_url,
            "universe_count": len((meta or {}).get("universe", [])),
            "mids_count": len(mids or {}),
            "error": api_error,
        },
        "account": {
            "configured": bool(address),
            "address": _redact_address(address),
            "account_value": _string_decimal(margin.get("accountValue")),
            "effective_account_value": str(effective_account_value.quantize(Decimal("0.000001"))),
            "free_spot_usdc": str(free_spot_usdc.quantize(Decimal("0.000001"))),
            "withdrawable": _string_decimal(withdrawable),
            "total_margin_used": _string_decimal(margin.get("totalMarginUsed")),
            "total_notional_position": _string_decimal(margin.get("totalNtlPos")),
            "cross_ntl_pos": _string_decimal(cross_margin.get("totalNtlPos")),
            "cross_account_value": _string_decimal(cross_margin.get("accountValue")),
            "cross_margin_used": _string_decimal(cross_margin.get("totalMarginUsed")),
            "cross_maintenance_margin_used": _string_decimal((user_state or {}).get("crossMaintenanceMarginUsed")),
            "unified_account_ratio_pct": quantized_decimal(uar.pct),
            "unified_account_ratio_status": uar.status,
            "unified_account_ratio_source": uar.source,
            "unified_account_ratio_threshold_pct": quantized_decimal(uar.threshold_pct),
            "unified_account_ratio_numerator": quantized_decimal(uar.numerator),
            "unified_account_ratio_denominator": quantized_decimal(uar.denominator),
            "open_positions": len(positions),
            "open_orders": len(open_orders),
            "error": account_error,
        },
        "positions": positions,
        "spot_balances": spot_balances,
        "open_orders": open_orders[:50],
        "funding": {
            "status": "ok" if predicted_error is None else "error",
            "predicted": relevant_fundings,
            "user_last_24h": user_funding[:50],
            "error": predicted_error,
        },
        "last_snapshot": _latest_account_snapshot(db),
    }
    if api_error is None and account_error is None:
        _ACCOUNT_SNAPSHOT_CACHE[cache_key] = (perf_counter(), deepcopy(payload))
    return payload


@router.post("/account/orders/{oid}/cancel")
def cancel_account_order(
    oid: int,
    coin: str = Query(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._\-/]+$"),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    live_settings = effective_live_execution_settings(db, settings)
    try:
        response = HyperliquidTradingClient(settings).cancel_order(
            coin=coin,
            oid=oid,
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
        )
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.add(
        DecisionLog(
            event_type="live_order_cancel_requested",
            message="Open order cancel requested from account monitor",
            data={"coin": coin, "oid": oid, "response": response},
        )
    )
    db.commit()
    return {"status": "cancel_requested", "coin": coin, "oid": oid, "response": response}


@router.post("/account/positions/{coin}/close")
def close_account_position(
    coin: str = Path(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._\-/]+$"),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    live_settings = effective_live_execution_settings(db, settings)
    address = settings.hyperliquid_account_address
    if not address:
        raise HTTPException(status_code=400, detail="HYPERLIQUID_ACCOUNT_ADDRESS is required")
    trading = HyperliquidTradingClient(settings)
    size: Decimal | None = None
    try:
        remote = _account_position_for_coin(trading.user_state(address), coin)
        if remote is None:
            raise HTTPException(status_code=404, detail="Position not found on Hyperliquid")
        size = _decimal(remote.get("szi"))
        if size is None or size == 0:
            raise HTTPException(status_code=409, detail="Position has no open size")
        result = trading.submit_market_close(
            coin=coin,
            is_buy=size < 0,
            size=abs(size),
            max_slippage_pct=float(live_settings["max_slippage_pct"]),
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
        )
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if size is None:
        raise HTTPException(status_code=409, detail="Position has no open size")
    open_positions = (
        db.query(Position)
        .join(Position.market)
        .filter(
            Position.status == "open",
            Position.mode == "live",
            or_(
                Market.symbol == coin,
                Market.metadata_json["hl_coin"].astext == coin,
            ),
        )
        .all()
    )
    exit_price = result.avg_price or Decimal(str(abs(size)))
    executor = OrderExecutionEngine(settings)
    for pos in open_positions:
        executor.close_position(db, pos, exit_price=exit_price, reason="manual", commit=False)
    db.add(
        DecisionLog(
            event_type="live_account_position_close_requested",
            message="Remote account position close requested from account monitor",
            data={
                "coin": coin,
                "side": "long" if size > 0 else "short",
                "size": str(abs(size)),
                "result": result.raw_response,
                "oid": result.oid,
                "trado_positions_closed": [pos.id for pos in open_positions],
            },
        )
    )
    db.commit()
    return {
        "status": result.status,
        "coin": coin,
        "oid": result.oid,
        "avg_price": str(result.avg_price) if result.avg_price is not None else None,
        "filled_size": str(result.filled_size) if result.filled_size is not None else None,
    }


@router.post("/account/positions/patch-missing-exit-data")
def patch_missing_exit_data(db: Session = Depends(get_db)) -> dict:
    """Reconcile exit data for closed live positions using HL fill history.

    Three cases are handled in a single pass:
    1. realized_pnl IS NULL (e.g. UNI: no SL order in DB, no fill matched).
    2. A filled exit order exists whose oid is NOT in HL fills — data is wrong
       (e.g. BTC: reconciliation matched the wrong fill; actual close was a
       manual market order). Overrides with the real HL close fill.
    3. exit_price missing from metadata — written from the last filled exit
       order without changing realized_pnl.
    """
    settings = get_settings()
    address = settings.hyperliquid_account_address
    if not address:
        raise HTTPException(status_code=400, detail="HYPERLIQUID_ACCOUNT_ADDRESS is required")
    from app.engine.execution import coin_for_market
    from app.services.live_reconciliation import (
        _find_close_fill,
        _index_fills,
        _index_fills_by_coin,
    )
    trading = HyperliquidTradingClient(settings)
    try:
        raw_fills = trading.user_fills(address)
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _, fills_by_oid = _index_fills(raw_fills or [])
    fills_by_coin = _index_fills_by_coin(raw_fills or [])
    all_closed = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.mode == "live", Position.status == "closed")
        .all()
    )
    patched = []
    for pos in all_closed:
        if pos.market is None:
            continue
        try:
            coin = coin_for_market(pos.market)
        except ValueError:
            continue
        metadata = dict(pos.metadata_json or {})
        exit_orders = (
            db.query(Order)
            .filter(
                Order.position_id == pos.id,
                Order.order_type.in_(["tp1", "tp2", "tp3", "stop_loss", "manual"]),
                Order.status == "filled",
            )
            .order_by(Order.id.desc())
            .all()
        )
        last_exit = exit_orders[0] if exit_orders else None

        # Case 2: filled exit order whose oid is not in HL fills → wrong data
        wrong_data = False
        if last_exit is not None and last_exit.hyperliquid_order_id:
            try:
                oid_int = int(last_exit.hyperliquid_order_id)
                wrong_data = oid_int not in fills_by_oid
            except (TypeError, ValueError):
                pass

        # Case 1: no pnl at all; Case 2: wrong data from bad fill match
        if pos.realized_pnl is None or wrong_data:
            hl_fill = _find_close_fill(coin, pos.opened_at, fills_by_coin)
            if hl_fill is None:
                continue
            hl_px = _decimal(hl_fill.get("px"))
            hl_pnl = _decimal(hl_fill.get("closedPnl"))
            if hl_px is None or hl_pnl is None:
                continue
            pos.realized_pnl = hl_pnl
            metadata["realized_pnl"] = str(hl_pnl)
            metadata["exit_price"] = str(hl_px)
            metadata["exit_reason"] = "hl_fill_patch"
            # If the exit order was incorrectly marked as filled, correct it
            if wrong_data and last_exit is not None:
                last_exit.status = "canceled"
                last_exit.raw_response = {**(last_exit.raw_response or {}), "patched_to_canceled": True}
                db.add(last_exit)
            pos.metadata_json = metadata
            db.add(pos)
            db.add(DecisionLog(
                event_type="live_position_exit_data_patched",
                market_id=pos.market_id,
                position_id=pos.id,
                message="Exit data reconciled from HL fill history",
                data={"coin": coin, "exit_price": str(hl_px), "realized_pnl": str(hl_pnl),
                      "was_wrong_data": wrong_data},
            ))
            patched.append({"position_id": pos.id, "coin": coin,
                            "exit_price": str(hl_px), "realized_pnl": str(hl_pnl),
                            "was_wrong_data": wrong_data})
            continue

        # Case 3: pnl is correct but exit_price missing from metadata
        if not metadata.get("exit_price") and last_exit is not None and last_exit.price is not None:
            metadata["exit_price"] = str(last_exit.price)
            pos.metadata_json = metadata
            db.add(pos)
            patched.append({"position_id": pos.id, "coin": coin,
                            "exit_price": str(last_exit.price), "realized_pnl": None,
                            "was_wrong_data": False})
    db.commit()
    return {"patched": len(patched), "positions": patched}


@router.post("/account/positions/{coin}/repair-protection")
def repair_position_protection(
    coin: str,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    address = settings.hyperliquid_account_address
    if not address:
        raise HTTPException(status_code=400, detail="HYPERLIQUID_ACCOUNT_ADDRESS is required")
    trading = HyperliquidTradingClient(settings)
    from app.services.live_reconciliation import (
        _index_frontend_open_orders,
        protection_state_for_live_position,
        reconcile_live_positions,
    )

    updated = reconcile_live_positions(db, trading_client=trading, coins={coin}, force_repair=True)
    local_positions = (
        db.query(Position)
        .join(Position.market)
        .options(joinedload(Position.market))
        .filter(
            Position.mode == "live",
            Position.status == "open",
            (Market.symbol == coin) | (Market.metadata_json["hl_coin"].astext == coin),
        )
        .all()
    )
    if not local_positions:
        raise HTTPException(status_code=404, detail=f"No open Trado live position found for {coin}")
    try:
        frontend_open_orders = trading.frontend_open_orders(address)
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    frontend_by_coin = _index_frontend_open_orders(frontend_open_orders)
    states = [
        protection_state_for_live_position(
            db,
            position,
            frontend_by_coin.get(coin, []),
        )
        for position in local_positions
    ]
    aggregated = _merge_protection_states(states)
    db.add(
        DecisionLog(
            event_type="live_protection_manual_repair_requested",
            message="Manual protection repair requested from account monitor",
            data={
                "coin": coin,
                "updated_positions": updated,
                "position_ids": aggregated.get("local_position_ids", []),
                "status": aggregated.get("status"),
                "issues": aggregated.get("issues", []),
            },
        )
    )
    db.commit()
    return {
        "coin": coin,
        "updated_positions": updated,
        "protection": aggregated,
    }

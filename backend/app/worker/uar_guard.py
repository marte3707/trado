"""Unified account ratio guard: kill-switch e derisk su margine unificato."""
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from app.core.enums import OrderMode
from app.core.runtime_settings import (
    effective_live_execution_settings,
)
from app.db.models import AppSetting, DecisionLog, Position
from app.engine.execution import OrderExecutionEngine
from app.services.hyperliquid import (
    DEFAULT_GUARD_STATE_CACHE_SECONDS,
    HyperliquidTradingClient,
)
from app.services.unified_account_ratio import (
    DEFAULT_UAR_RISK_SETTINGS,
    calculate_unified_account_ratio,
    decimal_or_none,
    free_spot_usdc_from_balances,
)

logger = structlog.get_logger()


def _risk_setting_value(db) -> dict[str, Any]:
    setting = db.get(AppSetting, "risk")
    return dict(setting.value) if setting is not None and isinstance(setting.value, dict) else {}


def _uar_decimal_setting(risk: dict[str, Any], key: str) -> Decimal:
    default = DEFAULT_UAR_RISK_SETTINGS[key]
    parsed = decimal_or_none(risk.get(key, default))
    return parsed if parsed is not None else Decimal(str(default))


def _uar_int_setting(risk: dict[str, Any], key: str) -> int:
    default = int(DEFAULT_UAR_RISK_SETTINGS[key])
    try:
        return max(1, int(risk.get(key, default)))
    except (TypeError, ValueError):
        return default


def _arm_uar_kill_switch(db, ratio_pct: Decimal, block_pct: Decimal) -> None:
    now = datetime.now(UTC).isoformat()
    setting = db.get(AppSetting, "kill_switch") or AppSetting(key="kill_switch", value={})
    value = dict(setting.value) if isinstance(setting.value, dict) else {}
    value.update(
        {
            "enabled": True,
            "reason": "unified_account_ratio_block",
            "triggered_at": now,
            "unified_account_ratio_pct": str(ratio_pct),
            "unified_account_ratio_block_pct": str(block_pct),
        }
    )
    setting.value = value
    db.add(setting)
    db.add(
        DecisionLog(
            event_type="unified_account_ratio_kill_switch_enabled",
            message="Kill switch enabled because Unified Account Ratio is above block threshold",
            data={
                "unified_account_ratio_pct": str(ratio_pct),
                "block_pct": str(block_pct),
            },
        )
    )


def _remote_position_coin(position: dict[str, Any]) -> str | None:
    coin = position.get("coin")
    return coin if isinstance(coin, str) and coin else None


def _select_uar_derisk_position(user_state: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in user_state.get("assetPositions", []):
        if not isinstance(item, dict) or not isinstance(item.get("position"), dict):
            continue
        position = item["position"]
        size = decimal_or_none(position.get("szi"))
        if size is None or size == 0 or _remote_position_coin(position) is None:
            continue
        candidates.append(position)
    if not candidates:
        return None

    def key(position: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
        margin_used = decimal_or_none(position.get("marginUsed")) or Decimal("0")
        unrealized_pnl = decimal_or_none(position.get("unrealizedPnl")) or Decimal("0")
        position_value = decimal_or_none(position.get("positionValue")) or Decimal("0")
        return margin_used, -unrealized_pnl, position_value

    return max(candidates, key=key)


def _local_live_positions_for_coin(db, coin: str) -> list[Position]:
    rows = (
        db.query(Position)
        .filter(Position.status == "open", Position.mode == OrderMode.LIVE.value)
        .all()
    )
    matches: list[Position] = []
    for position in rows:
        market = getattr(position, "market", None)
        symbol = getattr(market, "symbol", None)
        metadata = getattr(market, "metadata_json", None)
        hl_coin = metadata.get("hl_coin") if isinstance(metadata, dict) else None
        if symbol == coin or hl_coin == coin:
            matches.append(position)
    return matches


def _cancel_remote_orders_for_coin(
    trading: HyperliquidTradingClient,
    address: str,
    coin: str,
    expires_after_seconds: int,
) -> list[dict[str, Any]]:
    cancelled: list[dict[str, Any]] = []
    for order in trading.frontend_open_orders(address):
        if not isinstance(order, dict) or order.get("coin") != coin:
            continue
        oid = order.get("oid")
        if oid is None:
            cancelled.append({"oid": oid, "status": "skipped_invalid_oid"})
            continue
        try:
            oid_int = int(str(oid))
        except (TypeError, ValueError):
            cancelled.append({"oid": oid, "status": "skipped_invalid_oid"})
            continue
        try:
            response = trading.cancel_order(
                coin=coin,
                oid=oid_int,
                expires_after_seconds=expires_after_seconds,
            )
            cancelled.append({"oid": oid_int, "status": "cancelled", "response": response})
        except Exception as exc:
            cancelled.append({"oid": oid_int, "status": "error", "error": str(exc)})
    return cancelled


def audit_unified_account_ratio_guard(
    db,
    executor: OrderExecutionEngine,
    trading_client: HyperliquidTradingClient | None = None,
) -> dict[str, Any]:
    settings = executor.settings
    address = settings.hyperliquid_account_address
    if not address:
        return {"status": "skipped", "reason": "missing_account_address"}

    risk = _risk_setting_value(db)
    warn_pct = _uar_decimal_setting(risk, "unified_account_ratio_warn_pct")
    block_pct = _uar_decimal_setting(risk, "unified_account_ratio_block_pct")
    derisk_pct = _uar_decimal_setting(risk, "unified_account_ratio_derisk_pct")
    target_pct = _uar_decimal_setting(risk, "unified_account_ratio_target_pct")
    derisk_enabled = bool(
        risk.get(
            "unified_account_ratio_derisk_enabled",
            DEFAULT_UAR_RISK_SETTINGS["unified_account_ratio_derisk_enabled"],
        )
    )
    max_positions = _uar_int_setting(risk, "unified_account_ratio_derisk_max_positions_per_cycle")

    trading = trading_client or HyperliquidTradingClient(settings)
    user_state = trading.user_state(address, max_age_seconds=DEFAULT_GUARD_STATE_CACHE_SECONDS)
    spot_state = trading.spot_user_state(address, max_age_seconds=DEFAULT_GUARD_STATE_CACHE_SECONDS)
    balances = spot_state.get("balances", []) if isinstance(spot_state, dict) else []
    free_spot_usdc = free_spot_usdc_from_balances(balances) if isinstance(balances, list) else Decimal("0")
    ratio = calculate_unified_account_ratio(
        user_state,
        free_spot_usdc=free_spot_usdc,
        warn_pct=warn_pct,
        block_pct=block_pct,
        derisk_pct=derisk_pct,
    )
    if ratio.pct is None:
        db.add(
            DecisionLog(
                event_type="unified_account_ratio_guard_unavailable",
                message="Unified Account Ratio unavailable during guard audit",
                data={"source": ratio.source},
            )
        )
        db.commit()
        return {"status": "unavailable", "source": ratio.source}

    result: dict[str, Any] = {
        "status": ratio.status,
        "unified_account_ratio_pct": str(ratio.pct),
        "source": ratio.source,
        "closed": [],
    }

    if ratio.pct >= block_pct:
        _arm_uar_kill_switch(db, ratio.pct, block_pct)

    if not derisk_enabled or ratio.pct < derisk_pct:
        db.commit()
        return result

    live_settings = effective_live_execution_settings(db, settings)
    for _ in range(max_positions):
        position = _select_uar_derisk_position(user_state)
        if position is None:
            db.add(
                DecisionLog(
                    event_type="unified_account_ratio_derisk_no_positions",
                    message="Unified Account Ratio is critical but no remote position is open",
                    data={"unified_account_ratio_pct": str(ratio.pct), "derisk_pct": str(derisk_pct)},
                )
            )
            break
        coin = _remote_position_coin(position)
        size = decimal_or_none(position.get("szi"))
        if coin is None or size is None or size == 0:
            break
        close_result = trading.submit_market_close(
            coin=coin,
            is_buy=size < 0,
            size=abs(size),
            max_slippage_pct=float(live_settings["max_slippage_pct"]),
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
        )
        cancelled = _cancel_remote_orders_for_coin(
            trading,
            address,
            coin,
            int(live_settings["order_expires_after_seconds"]),
        )
        exit_price = (
            close_result.avg_price
            or decimal_or_none(position.get("markPx"))
            or decimal_or_none(position.get("entryPx"))
        )
        local_closed: list[int] = []
        if exit_price is not None:
            for local_position in _local_live_positions_for_coin(db, coin):
                executor.close_position(
                    db,
                    local_position,
                    exit_price=exit_price,
                    reason="unified_account_ratio_derisk",
                    commit=False,
                )
                if local_position.id is not None:
                    local_closed.append(local_position.id)
        event_data = {
            "coin": coin,
            "side": "long" if size > 0 else "short",
            "size": str(abs(size)),
            "unified_account_ratio_pct": str(ratio.pct),
            "derisk_pct": str(derisk_pct),
            "target_pct": str(target_pct),
            "oid": close_result.oid,
            "status": close_result.status,
            "cancelled_orders": cancelled,
            "trado_positions_closed": local_closed,
        }
        db.add(
            DecisionLog(
                event_type="unified_account_ratio_derisk_close_requested",
                message="Market reduce-only close requested by Unified Account Ratio guard",
                data=event_data,
            )
        )
        result["closed"].append(event_data)
        break

    db.commit()
    return result



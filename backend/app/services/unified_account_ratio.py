from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


DEFAULT_UAR_RISK_SETTINGS: dict[str, Any] = {
    "unified_account_ratio_warn_pct": 80,
    "unified_account_ratio_block_pct": 90,
    "unified_account_ratio_derisk_pct": 95,
    "unified_account_ratio_target_pct": 85,
    "unified_account_ratio_derisk_enabled": True,
    "unified_account_ratio_derisk_max_positions_per_cycle": 1,
}


@dataclass(frozen=True)
class UnifiedAccountRatio:
    pct: Decimal | None
    status: str
    source: str
    threshold_pct: Decimal
    numerator: Decimal | None
    denominator: Decimal | None


def decimal_or_none(value: object | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def free_spot_usdc_from_balances(balances: list[dict[str, Any]]) -> Decimal:
    for balance in balances:
        if balance.get("coin") != "USDC":
            continue
        total = decimal_or_none(balance.get("total")) or Decimal("0")
        hold = decimal_or_none(balance.get("hold")) or Decimal("0")
        return max(Decimal("0"), total - hold)
    return Decimal("0")


def unified_account_ratio_status(
    pct: Decimal | None,
    *,
    warn_pct: Decimal,
    block_pct: Decimal,
    derisk_pct: Decimal,
) -> str:
    if pct is None:
        return "unavailable"
    if pct >= derisk_pct:
        return "liquidation_risk"
    if pct >= block_pct:
        return "critical"
    if pct >= warn_pct:
        return "warning"
    return "ok"


def calculate_unified_account_ratio(
    user_state: dict[str, Any] | None,
    *,
    free_spot_usdc: Decimal = Decimal("0"),
    warn_pct: Decimal = Decimal("80"),
    block_pct: Decimal = Decimal("90"),
    derisk_pct: Decimal = Decimal("95"),
) -> UnifiedAccountRatio:
    state = user_state or {}
    margin_raw = state.get("marginSummary")
    cross_margin_raw = state.get("crossMarginSummary")
    margin: dict[str, Any] = margin_raw if isinstance(margin_raw, dict) else {}
    cross_margin: dict[str, Any] = cross_margin_raw if isinstance(cross_margin_raw, dict) else {}

    numerator = decimal_or_none(state.get("crossMaintenanceMarginUsed"))
    cross_account_value = decimal_or_none(cross_margin.get("accountValue"))
    margin_account_value = decimal_or_none(margin.get("accountValue"))

    denominator: Decimal | None = None
    source = "unavailable"
    if cross_account_value is not None:
        denominator = cross_account_value + max(Decimal("0"), free_spot_usdc)
        source = "cross_maintenance_over_cross_equity_plus_free_spot_usdc"
    elif margin_account_value is not None:
        denominator = margin_account_value
        source = "cross_maintenance_over_margin_equity_fallback"

    pct: Decimal | None = None
    if numerator is not None and denominator is not None and denominator > 0:
        pct = (numerator / denominator) * Decimal("100")

    return UnifiedAccountRatio(
        pct=pct,
        status=unified_account_ratio_status(
            pct,
            warn_pct=warn_pct,
            block_pct=block_pct,
            derisk_pct=derisk_pct,
        ),
        source=source,
        threshold_pct=derisk_pct,
        numerator=numerator,
        denominator=denominator,
    )


def quantized_decimal(value: Decimal | None, places: str = "0.000001") -> str | None:
    return str(value.quantize(Decimal(places))) if value is not None else None

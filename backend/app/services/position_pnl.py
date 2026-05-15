from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping

from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.enums import OrderMode
from app.db.models import Market, Position
from app.services.hyperliquid import HyperliquidClient


def calculate_unrealized_pnl(position: Position, mark_price: Decimal | None) -> Decimal | None:
    if mark_price is None:
        return None
    side = position.side.lower()
    if side == "long":
        return (mark_price - position.entry_price) * position.size
    if side == "short":
        return (position.entry_price - mark_price) * position.size
    return None


def _decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _mid_candidates(market: Market) -> list[str]:
    metadata = market.metadata_json or {}
    values = [
        metadata.get("hl_coin"),
        market.symbol,
        market.base,
    ]
    if "/" in market.symbol:
        values.append(market.symbol.split("/", 1)[0])
    return [value for value in values if isinstance(value, str) and value]


def mark_price_for_market(market: Market, mids: Mapping[str, str]) -> Decimal | None:
    for key in _mid_candidates(market):
        price = _decimal(mids.get(key))
        if price is not None:
            return price
    return None


async def refresh_open_position_pnls(
    db: Session,
    client: HyperliquidClient | None = None,
) -> int:
    positions = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.status == "open", Position.mode != OrderMode.LIVE.value)
        .all()
    )
    if not positions:
        return 0

    mids = await (client or HyperliquidClient(get_settings())).mids()
    updated = 0
    now = datetime.now(timezone.utc)
    updated_at = now.isoformat()
    for position in positions:
        if position.market is None:
            continue
        mark_price = mark_price_for_market(position.market, mids)
        pnl = calculate_unrealized_pnl(position, mark_price)
        if pnl is None:
            continue
        position.unrealized_pnl = pnl
        position.pnl_updated_at = now
        metadata = dict(position.metadata_json or {})
        metadata["last_mark_price"] = str(mark_price)
        metadata["unrealized_pnl_updated_at"] = updated_at
        position.metadata_json = metadata
        db.add(position)
        updated += 1

    if updated:
        db.commit()
    return updated

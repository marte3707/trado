"""Report costo-per-trade per le posizioni live.

Quantifica la differenza fra il PnL *lordo* riportato (che deriva da `closedPnl`
di Hyperliquid, al lordo delle fee) e il PnL *netto* reale, scomponendo i costi:

- **slippage di entry**: scarto fra il prezzo eseguito (`position.entry_price`) e
  il prezzo teorico del segnale (`signal.entry_price`);
- **fee**: dal campo reale `Order.fee` quando registrato dai fill, altrimenti
  stimato come `taker_fee_rate * notional` per lato (entry + exit);
- **funding**: `position.cum_funding_since_open` quando disponibile.

È puramente analitico/in lettura: non muta posizioni né ordini.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session, joinedload

from app.core.enums import OrderMode
from app.db.models import Order, Position, Signal


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class LiveTradeCost:
    position_id: int
    symbol: str | None
    side: str
    notional: Decimal | None
    gross_pnl: Decimal | None
    slippage_entry_usd: Decimal | None
    slippage_entry_bps: Decimal | None
    fee_usd: Decimal
    fee_is_estimated: bool
    funding_usd: Decimal
    net_pnl: Decimal | None


@dataclass(frozen=True)
class LiveCostReport:
    trades: int
    gross_pnl: Decimal
    fee_usd: Decimal
    funding_usd: Decimal
    slippage_usd: Decimal
    net_pnl: Decimal
    fee_tracked_trades: int
    fee_estimated_trades: int
    avg_slippage_bps: Decimal | None
    taker_fee_rate: float
    items: list[LiveTradeCost]


def _str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def serialize_live_cost_report(report: LiveCostReport, item_limit: int = 50) -> dict:
    """Serializza il report per l'API (Decimal → stringhe, lista item troncata)."""
    return {
        "trades": report.trades,
        "taker_fee_rate": report.taker_fee_rate,
        "gross_pnl": _str(report.gross_pnl),
        "fee_usd": _str(report.fee_usd),
        "funding_usd": _str(report.funding_usd),
        "slippage_usd": _str(report.slippage_usd),
        "net_pnl": _str(report.net_pnl),
        "fee_tracked_trades": report.fee_tracked_trades,
        "fee_estimated_trades": report.fee_estimated_trades,
        "avg_slippage_bps": _str(report.avg_slippage_bps),
        "items": [
            {
                "position_id": item.position_id,
                "symbol": item.symbol,
                "side": item.side,
                "notional": _str(item.notional),
                "gross_pnl": _str(item.gross_pnl),
                "slippage_entry_usd": _str(item.slippage_entry_usd),
                "slippage_entry_bps": _str(item.slippage_entry_bps),
                "fee_usd": _str(item.fee_usd),
                "fee_is_estimated": item.fee_is_estimated,
                "funding_usd": _str(item.funding_usd),
                "net_pnl": _str(item.net_pnl),
            }
            for item in report.items[:item_limit]
        ],
    }


def _direction_sign(side: str) -> int:
    return 1 if side.lower() == "long" else -1


def compute_trade_cost(
    position: Position,
    signal: Signal | None,
    orders: list[Order],
    taker_fee_rate: float,
) -> LiveTradeCost:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    size = _decimal(metadata.get("initial_size")) or position.size
    entry = position.entry_price
    notional = entry * size if size is not None else None

    # Slippage di entry: costo positivo = esecuzione peggiore del prezzo del segnale.
    slippage_usd: Decimal | None = None
    slippage_bps: Decimal | None = None
    signal_entry = signal.entry_price if signal is not None else None
    if signal_entry is not None and signal_entry > 0 and size is not None:
        slippage_usd = (entry - signal_entry) * size * _direction_sign(position.side)
        signal_notional = signal_entry * size
        if signal_notional > 0:
            slippage_bps = slippage_usd / signal_notional * Decimal("10000")

    # Fee: preferisci il dato reale dei fill (Order.fee), altrimenti stima da rate.
    tracked_fee = sum(
        (o.fee for o in orders if o.fee is not None), Decimal("0")
    )
    fee_is_estimated = tracked_fee <= 0
    if fee_is_estimated and notional is not None:
        # Round-trip: una fee per il lato di entry + una per il lato di exit.
        fee_usd = notional * Decimal(str(taker_fee_rate)) * Decimal("2")
    else:
        fee_usd = tracked_fee

    funding_usd = _decimal(position.cum_funding_since_open) or Decimal("0")

    gross_pnl = _decimal(position.realized_pnl)
    net_pnl: Decimal | None = None
    if gross_pnl is not None:
        # funding_usd è già firmato come impatto su equity (negativo = costo).
        net_pnl = gross_pnl - fee_usd + funding_usd

    return LiveTradeCost(
        position_id=position.id,
        symbol=position.market.symbol if position.market is not None else None,
        side=position.side,
        notional=notional,
        gross_pnl=gross_pnl,
        slippage_entry_usd=slippage_usd,
        slippage_entry_bps=slippage_bps,
        fee_usd=fee_usd,
        fee_is_estimated=fee_is_estimated,
        funding_usd=funding_usd,
        net_pnl=net_pnl,
    )


def aggregate_live_costs(
    db: Session,
    taker_fee_rate: float,
    limit: int = 500,
) -> LiveCostReport:
    positions = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(
            Position.mode == OrderMode.LIVE.value,
            Position.status == "closed",
            Position.realized_pnl.is_not(None),
        )
        .order_by(Position.closed_at.desc())
        .limit(limit)
        .all()
    )
    if not positions:
        return LiveCostReport(
            trades=0,
            gross_pnl=Decimal("0"),
            fee_usd=Decimal("0"),
            funding_usd=Decimal("0"),
            slippage_usd=Decimal("0"),
            net_pnl=Decimal("0"),
            fee_tracked_trades=0,
            fee_estimated_trades=0,
            avg_slippage_bps=None,
            taker_fee_rate=taker_fee_rate,
            items=[],
        )

    position_ids = [p.id for p in positions]
    orders_by_position: dict[int, list[Order]] = {pid: [] for pid in position_ids}
    for order in (
        db.query(Order).filter(Order.position_id.in_(position_ids)).all()
    ):
        if order.position_id in orders_by_position:
            orders_by_position[order.position_id].append(order)

    signal_ids = {
        int(p.metadata_json["source_signal_id"])
        for p in positions
        if isinstance(p.metadata_json, dict)
        and p.metadata_json.get("source_signal_id") is not None
    }
    signals: dict[int, Signal] = (
        {s.id: s for s in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )

    items: list[LiveTradeCost] = []
    for position in positions:
        meta = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        signal_id = meta.get("source_signal_id")
        signal = signals.get(int(signal_id)) if signal_id is not None else None
        items.append(
            compute_trade_cost(
                position,
                signal,
                orders_by_position.get(position.id, []),
                taker_fee_rate,
            )
        )

    gross = sum((i.gross_pnl for i in items if i.gross_pnl is not None), Decimal("0"))
    fees = sum((i.fee_usd for i in items), Decimal("0"))
    funding = sum((i.funding_usd for i in items), Decimal("0"))
    slippage = sum(
        (i.slippage_entry_usd for i in items if i.slippage_entry_usd is not None),
        Decimal("0"),
    )
    net = sum((i.net_pnl for i in items if i.net_pnl is not None), Decimal("0"))
    bps_values = [i.slippage_entry_bps for i in items if i.slippage_entry_bps is not None]
    avg_bps = (
        sum(bps_values, Decimal("0")) / Decimal(len(bps_values)) if bps_values else None
    )

    return LiveCostReport(
        trades=len(items),
        gross_pnl=gross,
        fee_usd=fees,
        funding_usd=funding,
        slippage_usd=slippage,
        net_pnl=net,
        fee_tracked_trades=sum(1 for i in items if not i.fee_is_estimated),
        fee_estimated_trades=sum(1 for i in items if i.fee_is_estimated),
        avg_slippage_bps=avg_bps,
        taker_fee_rate=taker_fee_rate,
        items=items,
    )

"use client";

import { Square, Trash2 } from "lucide-react";

import { Order, Position, apiDelete, apiPost } from "@/lib/api";
import {
  ICON_BTN,
  ICON_BTN_RED,
  TD,
  TH,
  TableKey,
  asNumber,
  formatCompactNumber,
  formatDate,
  formatEur,
  marketLabel,
  orderStatusBadge,
  relativeTime,
  sideBadge,
} from "@/components/dashboard/helpers";
import { EmptyRow, PaginationControls } from "@/components/dashboard/primitives";

interface TradingViewProps {
  positions: Position[];
  orders: Order[];
  positionsPage: { page: number; pageSize: number };
  ordersPage: { page: number; pageSize: number };
  onTablePageChange: (table: TableKey, patch: Partial<{ page: number; pageSize: number }>) => void;
  runAction: (label: string, action: () => Promise<void>) => void;
}

export function TradingView({
  positions,
  orders,
  positionsPage,
  ordersPage,
  onTablePageChange,
  runAction,
}: TradingViewProps) {
  const openPositions = positions.filter((p) => p.status === "open");
  const completedOrders = orders.filter((o) =>
    ["filled", "executed", "closed"].includes(o.status.toLowerCase())
  );
  const positionPage = Math.min(
    positionsPage.page,
    Math.max(1, Math.ceil(positions.length / positionsPage.pageSize))
  );
  const orderPage = Math.min(
    ordersPage.page,
    Math.max(1, Math.ceil(orders.length / ordersPage.pageSize))
  );
  const paginatedPositions = positions.slice(
    (positionPage - 1) * positionsPage.pageSize,
    positionPage * positionsPage.pageSize
  );
  const paginatedOrders = orders.slice(
    (orderPage - 1) * ordersPage.pageSize,
    orderPage * ordersPage.pageSize
  );

  return (
    <div className="flex flex-col gap-4 mb-5">

      {/* Posizioni */}
      <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
        <div className="flex items-center justify-between gap-2 px-5 py-3.5 border-b border-divider">
          <h2 className="text-sm font-semibold text-text">Posizioni</h2>
          <div className="flex items-center gap-2">
            {openPositions.length > 0 && (
              <span className="w-1.5 h-1.5 rounded-full bg-green animate-pulse" />
            )}
            <span className="text-xs text-muted">{openPositions.length} aperte · {positions.length} totali</span>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                {["Coppia", "Lato", "Entry", "P/L live", "SL / TP", "Size", "Stato", ""].map((h, i) => (
                  <th key={i} className={TH}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 && (
                <EmptyRow colSpan={8} message="Nessuna posizione" />
              )}
              {paginatedPositions.map((pos) => {
                const livePnl = asNumber(pos.unrealized_pnl ?? pos.realized_pnl);
                const livePnlClass = livePnl === null ? "text-muted" : livePnl >= 0 ? "text-green" : "text-red";

                return (
                  <tr key={pos.id} className="hover:bg-white/2 transition-colors group">
                    <td className={`${TD} text-text text-xs font-medium`}>{marketLabel(pos.market)}</td>
                    <td className={TD}>{sideBadge(pos.side)}</td>
                    <td className={`${TD} text-text text-xs`}>{pos.entry_price}</td>
                    <td className={`${TD} text-xs font-semibold ${livePnlClass}`}>{formatEur(livePnl)}</td>
                    <td className={`${TD} text-xs`}>
                      <div className="flex flex-col gap-0.5 leading-tight">
                        <span className="text-red/80">SL {pos.stop_loss ?? "—"}</span>
                        <span className="text-green/80">TP {pos.take_profit ?? "—"}</span>
                      </div>
                    </td>
                    <td className={`${TD} text-text text-xs`}>{pos.size}</td>
                    <td className={TD}>
                      <span
                        className={
                          pos.status === "open"
                            ? "text-green text-xs font-medium"
                            : "text-muted text-xs"
                        }
                      >
                        {pos.status === "open" ? "Aperta" : "Chiusa"}
                      </span>
                    </td>
                    <td className={TD}>
                      <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button
                          className={`${ICON_BTN} w-7 h-7`}
                          title="Chiudi posizione"
                          disabled={pos.status !== "open"}
                          onClick={() => {
                            const label = marketLabel(pos.market);
                            const side = pos.side.toUpperCase();
                            const pnl = asNumber(pos.unrealized_pnl ?? pos.realized_pnl);
                            const pnlStr = pnl !== null ? ` (P/L: ${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} $)` : "";
                            if (!window.confirm(`Chiudere ${side} ${label}${pnlStr}?\nVerrà inviato un ordine di chiusura a mercato.`)) return;
                            runAction(`close-${pos.id}`, () =>
                              apiPost(`/api/positions/${pos.id}/close`).then(() => undefined)
                            );
                          }}
                        >
                          <Square size={12} />
                        </button>
                        <button
                          className={`${ICON_BTN_RED} w-7 h-7`}
                          title="Elimina posizione"
                          onClick={() => {
                            const message = pos.status === "open"
                              ? `Eliminare la posizione aperta #${pos.id}? Questo rimuove il tracking, non invia un ordine di chiusura.`
                              : `Eliminare la posizione #${pos.id}?`;
                            if (!window.confirm(message)) return;
                            runAction(`delete-position-${pos.id}`, () =>
                              apiDelete(`/api/positions/${pos.id}`).then(() => undefined)
                            );
                          }}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <PaginationControls
          total={positions.length}
          page={positionPage}
          pageSize={positionsPage.pageSize}
          onPageChange={(page) => onTablePageChange("positions", { page })}
          onPageSizeChange={(pageSize) => onTablePageChange("positions", { pageSize })}
        />
      </div>

      {/* Ordini */}
      <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
        <div className="flex items-center justify-between gap-2 px-5 py-3.5 border-b border-divider">
          <h2 className="text-sm font-semibold text-text">Ordini</h2>
          <span className="text-xs text-muted">{orders.length} recenti · {completedOrders.length} completati</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                {["#", "Coppia", "Sig/Pos", "Tipo", "Modo", "Lato", "Prezzo", "Size", "Stato", "Ora"].map((h) => (
                  <th key={h} className={TH}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {orders.length === 0 && (
                <EmptyRow colSpan={10} message="Nessun ordine" />
              )}
              {paginatedOrders.map((order) => (
                <tr key={order.id} className="hover:bg-white/2 transition-colors">
                  <td className={`${TD} text-muted/50 text-xs`}>{order.id}</td>
                  <td className={`${TD} text-text text-xs font-medium`}>{marketLabel(order.market)}</td>
                  <td className={`${TD} text-muted text-xs`}>
                    <div className="flex flex-col gap-0.5 leading-tight">
                      <span>Sig {order.signal_id ?? "—"}</span>
                      <span>Pos {order.position_id ?? "—"}</span>
                    </div>
                  </td>
                  <td className={`${TD} text-text text-xs`}>{order.order_type}</td>
                  <td className={`${TD} text-text text-xs`}>{order.mode}</td>
                  <td className={TD}>{sideBadge(order.side)}</td>
                  <td className={`${TD} text-text text-xs font-medium`}>{order.price ?? "—"}</td>
                  <td className={`${TD} text-text text-xs`}>{formatCompactNumber(order.size, 6)}</td>
                  <td className={TD}>{orderStatusBadge(order.status)}</td>
                  <td className={`${TD} text-muted text-xs`} title={formatDate(order.created_at)}>
                    {relativeTime(order.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <PaginationControls
          total={orders.length}
          page={orderPage}
          pageSize={ordersPage.pageSize}
          onPageChange={(page) => onTablePageChange("orders", { page })}
          onPageSizeChange={(pageSize) => onTablePageChange("orders", { pageSize })}
        />
      </div>
    </div>
  );
}

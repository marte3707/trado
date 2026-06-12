"use client";

import { RefreshCcw, Trash2 } from "lucide-react";

import { AppSettings, Calibration, Position, Signal, apiDelete, apiPost } from "@/lib/api";
import {
  ICON_BTN_RED,
  TD,
  TH,
  TableKey,
  asNumber,
  calibrationChangeLabel,
  calibrationChangeValue,
  formatCompactNumber,
  formatEur,
  formatPercent,
  getExitPrice,
  marketLabel,
  relativeTime,
  sideBadge,
} from "@/components/dashboard/helpers";
import { EmptyRow, PaginationControls } from "@/components/dashboard/primitives";

interface PerformanceViewProps {
  signals: Signal[];
  positions: Position[];
  completedOrdersCount: number;
  calibration: Calibration;
  onCalibrationChange: (calibration: Calibration) => void;
  settings: AppSettings;
  busy: string | null;
  runAction: (label: string, action: () => Promise<void>) => void;
  closedPositionsPage: { page: number; pageSize: number };
  onTablePageChange: (table: TableKey, patch: Partial<{ page: number; pageSize: number }>) => void;
}

export function PerformanceView({
  signals,
  positions,
  completedOrdersCount,
  calibration,
  onCalibrationChange,
  settings,
  busy,
  runAction,
  closedPositionsPage,
  onTablePageChange,
}: PerformanceViewProps) {
  const openPositions = positions.filter((p) => p.status === "open");
  const closedPositions = positions.filter((p) => p.status !== "open");
  const riskSettings = settings.risk ?? {};
  const minAverageQuoteVolumeUsd = asNumber(riskSettings.min_average_quote_volume_usd);
  const autoCalibrationSettings = settings.auto_calibration ?? {};
  const autoCalibrationEnabled = autoCalibrationSettings.enabled !== false;
  const lastCalibrationRun =
    typeof calibration.state?.last_run_at === "string" ? calibration.state.last_run_at : null;
  const realizedPnls = closedPositions
    .map((position) => asNumber(position.realized_pnl))
    .filter((value): value is number => value !== null);
  const totalPnl = realizedPnls.reduce((sum, value) => sum + value, 0);
  const winningTrades = realizedPnls.filter((value) => value > 0).length;
  const losingTrades = realizedPnls.filter((value) => value < 0).length;
  const winRate = realizedPnls.length > 0 ? (winningTrades / realizedPnls.length) * 100 : null;
  const averagePnl = realizedPnls.length > 0 ? totalPnl / realizedPnls.length : null;
  const bestTrade = realizedPnls.length > 0 ? Math.max(...realizedPnls) : null;
  const worstTrade = realizedPnls.length > 0 ? Math.min(...realizedPnls) : null;
  const averageHoldMs = closedPositions
    .map((position) => {
      if (!position.closed_at) return null;
      return new Date(position.closed_at).getTime() - new Date(position.opened_at).getTime();
    })
    .filter((value): value is number => value !== null && Number.isFinite(value))
    .reduce(
      (acc, value, _index, array) => ({
        total: acc.total + value,
        count: array.length
      }),
      { total: 0, count: 0 }
    );
  const averageHoldHours = averageHoldMs.count > 0 ? averageHoldMs.total / averageHoldMs.count / 3600000 : null;
  const closedPositionPage = Math.min(
    closedPositionsPage.page,
    Math.max(1, Math.ceil(closedPositions.length / closedPositionsPage.pageSize))
  );
  const paginatedClosedPositions = closedPositions.slice(
    (closedPositionPage - 1) * closedPositionsPage.pageSize,
    closedPositionPage * closedPositionsPage.pageSize
  );

  return (
    <>
      <section className="grid grid-cols-4 gap-3 mb-5 max-md:grid-cols-2 max-sm:grid-cols-1">
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">P&L realizzato</span>
          <strong className={`text-2xl font-bold ${totalPnl >= 0 ? "text-green" : "text-red"}`}>
            {formatEur(totalPnl)}
          </strong>
          <span className="text-xs text-muted">{realizedPnls.length} trade con P&L</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Win rate</span>
          <strong className="text-2xl font-bold text-text">{formatPercent(winRate)}</strong>
          <span className="text-xs text-muted">{winningTrades} vincenti · {losingTrades} perdenti</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">P&L medio</span>
          <strong className={`text-2xl font-bold ${(averagePnl ?? 0) >= 0 ? "text-green" : "text-red"}`}>
            {formatEur(averagePnl)}
          </strong>
          <span className="text-xs text-muted">Best {formatEur(bestTrade)} · worst {formatEur(worstTrade)}</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Durata media</span>
          <strong className="text-2xl font-bold text-text">
            {averageHoldHours === null ? "—" : `${averageHoldHours.toFixed(1)}h`}
          </strong>
          <span className="text-xs text-muted">{closedPositions.length} posizioni chiuse</span>
        </div>
      </section>

      <section className="grid grid-cols-3 gap-3 mb-5 max-md:grid-cols-1">
        <div className="bg-surface rounded-2xl shadow-card p-4">
          <h2 className="text-sm font-semibold text-text mb-3">Distribuzione</h2>
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Long chiusi</span>
              <strong className="text-text">
                {closedPositions.filter((position) => position.side.toLowerCase() === "long").length}
              </strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Short chiusi</span>
              <strong className="text-text">
                {closedPositions.filter((position) => position.side.toLowerCase() === "short").length}
              </strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Auto TP/SL</span>
              <strong className="text-text">
                {closedPositions.filter((position) => typeof position.metadata_json?.exit_reason === "string").length}
              </strong>
            </div>
          </div>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4">
          <h2 className="text-sm font-semibold text-text mb-3">Rischio</h2>
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Posizioni aperte</span>
              <strong className="text-text">{openPositions.length}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Max posizioni</span>
              <strong className="text-text">{String(riskSettings.max_open_positions ?? "—")}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Liquidità min.</span>
              <strong className="text-text">{formatEur(minAverageQuoteVolumeUsd)}</strong>
            </div>
          </div>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4">
          <h2 className="text-sm font-semibold text-text mb-3">Operatività</h2>
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Ordini completati</span>
              <strong className="text-text">{completedOrdersCount}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Segnali eseguiti</span>
              <strong className="text-text">{signals.filter((signal) => signal.status === "executed").length}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Conversione</span>
              <strong className="text-text">
                {formatPercent(signals.length > 0 ? (positions.length / signals.length) * 100 : null)}
              </strong>
            </div>
          </div>
        </div>
      </section>

      <section className="bg-surface rounded-2xl shadow-card overflow-hidden mb-5">
        <div className="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-divider max-sm:flex-col max-sm:items-start">
          <div>
            <h2 className="text-sm font-semibold text-text">Auto-calibrazione</h2>
            <p className="text-xs text-muted mt-0.5">
              {autoCalibrationEnabled ? "attiva" : "disattivata"} · {calibration.reason}
            </p>
          </div>
          <button
            className="inline-flex items-center gap-1.5 px-3 h-8 rounded-lg bg-blue/15 text-blue text-xs font-semibold hover:bg-blue/20 transition-colors disabled:opacity-40"
            onClick={() =>
              runAction("auto-calibration", () =>
                apiPost<Calibration>("/api/auto-calibration/run").then((result) => {
                  onCalibrationChange(result);
                })
              )
            }
            disabled={busy === "auto-calibration"}
            title="Esegui auto-calibrazione"
          >
            <RefreshCcw size={13} className={busy === "auto-calibration" ? "animate-spin" : ""} />
            Calibra
          </button>
        </div>
        <div className="grid grid-cols-[0.9fr_1.1fr] gap-4 p-4 max-lg:grid-cols-1">
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Trade analizzati</span>
              <strong className="text-text">{calibration.overall.trades}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Win rate storico</span>
              <strong className="text-text">{formatPercent(calibration.overall.win_rate * 100)}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Profit factor</span>
              <strong className="text-text">{formatCompactNumber(calibration.overall.profit_factor)}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Ultima esecuzione</span>
              <strong className="text-text">{lastCalibrationRun ? relativeTime(lastCalibrationRun) : "—"}</strong>
            </div>
          </div>
          <div className="grid gap-2">
            {calibration.changes.length === 0 && (
              <p className="text-xs text-muted py-2">Nessuna variazione proposta con lo storico disponibile.</p>
            )}
            {calibration.changes.slice(0, 6).map((change) => (
              <div
                key={`${change.target}:${change.key}`}
                className="grid grid-cols-[1fr_auto] gap-2 rounded-xl bg-surface-2 px-3 py-2"
                title={change.reason}
              >
                <span className="text-xs text-text truncate">{calibrationChangeLabel(change)}</span>
                <strong className="text-xs text-blue whitespace-nowrap">
                  {calibrationChangeValue(change.before)} → {calibrationChangeValue(change.after)}
                </strong>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="bg-surface rounded-2xl shadow-card overflow-hidden mb-5">
        <div className="flex items-center justify-between gap-2 px-5 py-3.5 border-b border-divider">
          <h2 className="text-sm font-semibold text-text">Posizioni chiuse</h2>
          <span className="text-xs text-muted">{closedPositions.length} chiuse</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse min-w-200">
            <thead>
              <tr>
                {["Coppia", "Lato", "Entry", "Exit", "P&L", "Aperta", "Chiusa", ""].map((h) => (
                  <th key={h} className={TH}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {closedPositions.length === 0 && (
                <EmptyRow colSpan={8} message="Nessuna posizione chiusa" />
              )}
              {paginatedClosedPositions.map((position) => {
                const pnl = asNumber(position.realized_pnl);
                return (
                  <tr key={position.id} className="hover:bg-white/2 transition-colors group">
                    <td className={`${TD} text-text text-xs font-medium`}>{marketLabel(position.market)}</td>
                    <td className={TD}>{sideBadge(position.side)}</td>
                    <td className={`${TD} text-text text-xs`}>{position.entry_price}</td>
                    <td className={`${TD} text-text text-xs`}>{getExitPrice(position) ?? "—"}</td>
                    <td className={`${TD} text-xs font-semibold ${pnl === null ? "text-muted" : pnl >= 0 ? "text-green" : "text-red"}`}>
                      {formatEur(position.realized_pnl)}
                    </td>
                    <td className={`${TD} text-muted text-xs`}>{relativeTime(position.opened_at)}</td>
                    <td className={`${TD} text-muted text-xs`}>{relativeTime(position.closed_at)}</td>
                    <td className={TD}>
                      <button
                        className={`${ICON_BTN_RED} w-7 h-7 opacity-0 group-hover:opacity-100 transition-opacity`}
                        title="Elimina posizione"
                        onClick={() => {
                          if (!window.confirm(`Eliminare la posizione #${position.id}?`)) return;
                          runAction(`delete-position-${position.id}`, () =>
                            apiDelete(`/api/positions/${position.id}`).then(() => undefined)
                          );
                        }}
                      >
                        <Trash2 size={12} />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <PaginationControls
          total={closedPositions.length}
          page={closedPositionPage}
          pageSize={closedPositionsPage.pageSize}
          onPageChange={(page) => onTablePageChange("closedPositions", { page })}
          onPageSizeChange={(pageSize) => onTablePageChange("closedPositions", { pageSize })}
        />
      </section>
    </>
  );
}

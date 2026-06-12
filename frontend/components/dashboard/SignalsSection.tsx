"use client";

import { ArrowDown, ArrowUp, ArrowUpDown, Ban, Play, RotateCcw, Shield, Trash2 } from "lucide-react";

import { Signal, apiDelete, apiPost } from "@/lib/api";
import {
  Filters,
  ICON_BTN,
  ICON_BTN_RED,
  INPUT_CLS,
  SELECT_CLS,
  SignalSort,
  SignalSortKey,
  TD,
  TH,
  directionBadge,
  directionTooltip,
  formatDate,
  marketKey,
  relativeTime,
  scoreBgClass,
  scoreColor,
  scoreSummary,
  statusBadge,
} from "@/components/dashboard/helpers";
import { EmptyRow, PaginationControls, PillFilter } from "@/components/dashboard/primitives";

interface SignalsSectionProps {
  signals: Signal[]; // già filtrati e ordinati dal parent
  page: number;
  pageSize: number;
  onTablePageChange: (patch: Partial<{ page: number; pageSize: number }>) => void;
  signalSearch: string;
  onSignalSearchChange: (value: string) => void;
  filters: Filters;
  onFiltersChange: (next: Filters) => void;
  onFiltersCommit: (next: Filters) => void;
  timeframeOptions: string[];
  signalSort: SignalSort;
  onToggleSort: (key: SignalSortKey) => void;
  onSelectMarket: (key: string) => void;
  showTooltip: (e: React.MouseEvent, text: string) => void;
  hideTooltip: () => void;
  runAction: (label: string, action: () => Promise<void>) => void;
  liveTradingAllowed: boolean;
  killSwitchEnabled: boolean;
}

export function SignalsSection({
  signals,
  page,
  pageSize,
  onTablePageChange,
  signalSearch,
  onSignalSearchChange,
  filters,
  onFiltersChange,
  onFiltersCommit,
  timeframeOptions,
  signalSort,
  onToggleSort,
  onSelectMarket,
  showTooltip,
  hideTooltip,
  runAction,
  liveTradingAllowed,
  killSwitchEnabled,
}: SignalsSectionProps) {
  const safePage = Math.min(page, Math.max(1, Math.ceil(signals.length / pageSize)));
  const paginatedSignals = signals.slice((safePage - 1) * pageSize, safePage * pageSize);

  function signalSortIcon(key: SignalSortKey) {
    if (signalSort?.key !== key) return <ArrowUpDown size={12} className="opacity-40" />;
    return signalSort.direction === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />;
  }

  function signalHeader(key: SignalSortKey, label: string) {
    return (
      <button
        type="button"
        onClick={() => onToggleSort(key)}
        className={`flex items-center gap-1 uppercase tracking-wider ${signalSort?.key === key ? "text-text" : "text-muted/70"}`}
        title={`Ordina per ${label}`}
      >
        <span>{label}</span>
        {signalSortIcon(key)}
      </button>
    );
  }

  return (
    <section className="bg-surface rounded-2xl shadow-card overflow-hidden mb-5">
      <div className="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-divider flex-wrap gap-y-2">
        <div className="flex items-center gap-2.5">
          <h2 className="text-sm font-semibold text-text">Segnali</h2>
          {signals.length > 0 && (
            <span className="text-xs text-muted bg-surface-2 px-2 py-0.5 rounded-full">
              {signals.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            type="text"
            value={signalSearch}
            onChange={(e) => onSignalSearchChange(e.target.value)}
            placeholder="Cerca segnale..."
            className={`${INPUT_CLS} w-44`}
          />
          {/* Status pill-tabs */}
          <PillFilter
            value={filters.status}
            options={[
              { value: "all",      label: "Tutti"    },
              { value: "new",      label: "Nuovi"    },
              { value: "executed", label: "Eseguiti" },
              { value: "ignored",  label: "Ignorati" },
            ]}
            onChange={(v) => onFiltersCommit({ ...filters, status: v })}
          />
          {/* Market type */}
          <PillFilter
            value={filters.marketType}
            options={[
              { value: "all",  label: "Spot+Perp" },
              { value: "spot", label: "Spot"      },
              { value: "perp", label: "Perp"      },
            ]}
            onChange={(v) => {
              onFiltersChange({ ...filters, marketType: v });
              onTablePageChange({ page: 1 });
            }}
          />
          {/* Direction */}
          <select
            value={filters.direction}
            onChange={(e) => onFiltersCommit({ ...filters, direction: e.target.value })}
            className={SELECT_CLS}
          >
            <option value="all">Direzione</option>
            <option value="LONG">Long</option>
            <option value="STRONG_LONG">Strong Long</option>
            <option value="SHORT">Short</option>
            <option value="STRONG_SHORT">Strong Short</option>
          </select>
          {/* Timeframe */}
          <select
            value={filters.timeframe}
            onChange={(e) => onFiltersCommit({ ...filters, timeframe: e.target.value })}
            className={SELECT_CLS}
          >
            <option value="all">TF</option>
            {timeframeOptions.map((tf) => (
              <option key={tf} value={tf}>{tf}</option>
            ))}
          </select>
          {/* Min score */}
          <div className="flex items-center gap-1.5 bg-surface-2 rounded-lg px-2 h-7">
            <span className="text-xs text-muted/70">Score ≥</span>
            <input
              min="0"
              max="100"
              type="number"
              value={filters.minScore}
              onChange={(e) => onFiltersChange({ ...filters, minScore: e.target.value })}
              onBlur={() => onFiltersCommit(filters)}
              className="w-10 bg-transparent text-text text-xs focus:outline-none border-0 p-0"
            />
          </div>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse min-w-240">
          <thead>
            <tr>
              <th className={TH}>{signalHeader("id", "#")}</th>
              <th className={TH}>{signalHeader("symbol", "Coppia")}</th>
              <th className={TH}>{signalHeader("timeframe", "TF")}</th>
              <th className={TH}>{signalHeader("direction", "Direzione")}</th>
              <th className={TH}>{signalHeader("score", "Score")}</th>
              <th className={TH}>{signalHeader("entry", "Entry")}</th>
              <th className={TH}>SL / TP</th>
              <th className={TH}>{signalHeader("riskReward", "R/R")}</th>
              <th className={TH}>{signalHeader("status", "Stato")}</th>
              <th className={TH}>{signalHeader("createdAt", "Ora")}</th>
              <th className={TH} />
            </tr>
          </thead>
          <tbody>
            {signals.length === 0 && (
              <EmptyRow colSpan={11} message="Nessun segnale corrisponde ai filtri selezionati" />
            )}
            {paginatedSignals.map((sig) => (
              <tr key={sig.id} className="hover:bg-white/2 transition-colors group">
                <td className={`${TD} text-muted/50 text-xs`}>{sig.id}</td>
                <td className={`${TD} font-semibold`}>
                  <button
                    className="bg-transparent border-none p-0 text-text text-xs font-semibold cursor-pointer hover:text-blue transition-colors"
                    onClick={() => sig.market && onSelectMarket(marketKey(sig.market))}
                  >
                    {sig.market?.symbol ?? "—"}
                    <span className="ml-1 text-muted/60 font-normal text-[10px]">
                      {sig.market?.market_type?.toUpperCase()}
                    </span>
                  </button>
                </td>
                <td className={`${TD} text-muted text-xs`}>{sig.timeframe}</td>
                <td className={TD}>
                  <span
                    className="cursor-help"
                    onMouseEnter={(e) => showTooltip(e, directionTooltip(sig.direction, sig.reason))}
                    onMouseLeave={hideTooltip}
                  >
                    {directionBadge(sig.direction)}
                  </span>
                </td>
                <td className={TD}>
                  <span
                    className="cursor-help flex flex-col gap-1"
                    onMouseEnter={(e) => showTooltip(e, scoreSummary(sig))}
                    onMouseLeave={hideTooltip}
                  >
                    <span className={`text-sm font-bold leading-none ${scoreColor(sig.score)}`}>{sig.score}</span>
                    <span className="h-0.5 w-10 rounded-full bg-white/8 overflow-hidden">
                      <span
                        className={`block h-full rounded-full ${scoreBgClass(sig.score)}`}
                        style={{ width: `${sig.score}%` }}
                      />
                    </span>
                  </span>
                </td>
                <td className={`${TD} text-text font-medium text-xs`}>{sig.entry_price}</td>
                <td className={`${TD} text-xs`}>
                  <div className="flex flex-col gap-0.5 leading-tight">
                    {sig.stop_loss && (
                      <span className="text-red/80">SL {sig.stop_loss}</span>
                    )}
                    {sig.take_profit_1 && (
                      <span className="text-green/70">TP1 {sig.take_profit_1}</span>
                    )}
                    {sig.take_profit_2 && (
                      <span className="text-green/90">TP2 {sig.take_profit_2}</span>
                    )}
                    {sig.take_profit_3 && (
                      <span className="text-green/60">TP3 {sig.take_profit_3}</span>
                    )}
                    {!sig.stop_loss && !sig.take_profit_1 && !sig.take_profit_2 && !sig.take_profit_3 && (
                      <span className="text-muted">—</span>
                    )}
                  </div>
                </td>
                <td className={`${TD} text-muted text-xs`}>{sig.risk_reward ?? "—"}</td>
                <td className={TD}>{statusBadge(sig.status)}</td>
                <td
                  className={`${TD} text-muted text-xs`}
                  title={formatDate(sig.created_at)}
                >
                  {relativeTime(sig.created_at)}
                </td>
                <td className={TD}>
                  <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      className={ICON_BTN}
                      title="Paper trade"
                      disabled={sig.status !== "new"}
                      onClick={() =>
                        runAction(`paper-${sig.id}`, () =>
                          apiPost(`/api/signals/${sig.id}/execute-paper`).then(() => undefined)
                        )
                      }
                    >
                      <Play size={13} />
                    </button>
                    <button
                      className={ICON_BTN}
                      title="Ignora"
                      disabled={sig.status !== "new"}
                      onClick={() =>
                        runAction(`ignore-${sig.id}`, () =>
                          apiPost(`/api/signals/${sig.id}/ignore`).then(() => undefined)
                        )
                      }
                    >
                      <Ban size={13} />
                    </button>
                    <button
                      className={ICON_BTN}
                      title="Ripristina"
                      disabled={sig.status !== "ignored"}
                      onClick={() =>
                        runAction(`reopen-${sig.id}`, () =>
                          apiPost(`/api/signals/${sig.id}/reopen`).then(() => undefined)
                        )
                      }
                    >
                      <RotateCcw size={13} />
                    </button>
                    <button
                      className={ICON_BTN_RED}
                      title="Esegui live"
                      disabled={!liveTradingAllowed || killSwitchEnabled || sig.status !== "new"}
                      onClick={() => {
                        const symbol = sig.market?.symbol ?? `#${sig.id}`;
                        const dir = sig.direction.toUpperCase();
                        if (!window.confirm(`Eseguire segnale LIVE ${dir} ${symbol}?\nQuesta operazione invierà un ordine reale su Hyperliquid.`)) return;
                        runAction(`live-${sig.id}`, () =>
                          apiPost(`/api/signals/${sig.id}/execute-live`).then(() => undefined)
                        );
                      }}
                    >
                      <Shield size={13} />
                    </button>
                    <button
                      className={ICON_BTN_RED}
                      title="Elimina segnale"
                      onClick={() => {
                        if (!window.confirm(`Eliminare il segnale #${sig.id}?`)) return;
                        runAction(`delete-signal-${sig.id}`, () =>
                          apiDelete(`/api/signals/${sig.id}`).then(() => undefined)
                        );
                      }}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <PaginationControls
        total={signals.length}
        page={safePage}
        pageSize={pageSize}
        onPageChange={(nextPage) => onTablePageChange({ page: nextPage })}
        onPageSizeChange={(nextPageSize) => onTablePageChange({ pageSize: nextPageSize })}
      />
    </section>
  );
}

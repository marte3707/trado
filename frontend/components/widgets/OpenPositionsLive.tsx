"use client";

import { ArrowRight, ChevronLeft, ChevronRight } from "lucide-react";

import { Card } from "@/components/ui/card";
import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import type { Position } from "@/lib/api";

interface OpenPositionsLiveProps {
  positions: Position[];
  className?: string;
  onOpen?: (position: Position) => void;
  label?: string;
  page?: number;
  pageSize?: number;
  onPageChange?: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
}

const PAGE_SIZE_OPTIONS = [6, 12, 24];

function asNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function unrealized(position: Position): number | null {
  const direct = asNumber(position.unrealized_pnl);
  if (direct !== null) return direct;
  const mark = asNumber(position.metadata_json?.last_mark_price);
  const entry = asNumber(position.entry_price);
  const size = asNumber(position.size);
  if (mark == null || entry == null || size == null) return null;
  return (mark - entry) * Math.abs(size) * (position.side.toLowerCase() === "short" ? -1 : 1);
}

function timeInTrade(openedAt: string): string {
  const ms = Date.now() - new Date(openedAt).getTime();
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ${mins % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}g ${hours % 24}h`;
}

function pctFromEntry(position: Position): number | null {
  const entry = asNumber(position.entry_price);
  const mark = asNumber(position.metadata_json?.last_mark_price);
  if (entry == null || mark == null || entry === 0) return null;
  const pct = ((mark - entry) / entry) * 100;
  return position.side.toLowerCase() === "short" ? -pct : pct;
}

export function OpenPositionsLive({
  positions,
  className,
  onOpen,
  label,
  page = 1,
  pageSize = 6,
  onPageChange,
  onPageSizeChange,
}: OpenPositionsLiveProps) {
  const open = positions
    .filter((p) => p.status.toLowerCase() === "open")
    .sort((a, b) => new Date(b.opened_at).getTime() - new Date(a.opened_at).getTime());
  const totalPages = Math.max(1, Math.ceil(open.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const pageStart = (safePage - 1) * pageSize;
  const pageItems = open.slice(pageStart, pageStart + pageSize);
  const rangeStart = open.length === 0 ? 0 : pageStart + 1;
  const rangeEnd = Math.min(open.length, pageStart + pageSize);

  return (
    <Card className={cn("flex flex-col", className)}>
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Posizioni aperte
          </span>
          {label && (
            <span className="inline-flex h-5 items-center rounded-md border border-border bg-bg-2 px-1.5 font-mono text-[10px] uppercase tracking-wider text-fg-2">
              {label}
            </span>
          )}
        </div>
        <span className="font-mono text-[10px] text-fg-3">{open.length}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {open.length === 0 ? (
          <div className="grid h-full min-h-32 place-items-center text-xs text-fg-3">
            Nessuna posizione aperta {label ? `in ${label}` : ""}.
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {pageItems.map((p) => {
              const pnl = unrealized(p);
              const sideLong = p.side.toLowerCase() === "long";
              const isLive = p.mode.toLowerCase() === "live";
              const tone = pnl == null ? "muted" : pnl >= 0 ? "up" : "down";
              const ageStr = timeInTrade(p.opened_at);
              const pct = pctFromEntry(p);
              return (
                <li key={p.id}>
                  <button
                    className="group flex w-full items-start gap-2.5 px-6 py-3 text-left transition-colors hover:bg-bg-2"
                    onClick={() => onOpen?.(p)}
                  >
                    <div className="flex flex-col gap-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            "inline-flex h-5 items-center rounded-md border px-1.5 font-mono text-[10px]",
                            sideLong
                              ? "border-success/30 bg-success/10 text-success"
                              : "border-danger/30 bg-danger/10 text-danger",
                          )}
                        >
                          {sideLong ? "LONG" : "SHORT"}
                        </span>
                        <span className="font-mono text-sm font-medium tabular-nums text-fg">
                          {p.market?.symbol ?? "—"}
                        </span>
                        <span
                          className={cn(
                            "inline-flex h-5 items-center rounded-md border px-1.5 font-mono text-[10px] uppercase",
                            isLive
                              ? "border-danger/30 bg-danger/10 text-danger"
                              : "border-warn/30 bg-warn/10 text-warn",
                          )}
                        >
                          {p.mode}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 text-[10px] text-fg-3">
                        <span title="Tempo in trade">{ageStr}</span>
                        {pct != null && (
                          <>
                            <span className="text-fg-4">·</span>
                            <span
                              className={pct >= 0 ? "text-success" : "text-danger"}
                              title="Variazione % dall'entry"
                            >
                              {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
                            </span>
                          </>
                        )}
                        {p.stop_loss && (
                          <>
                            <span className="text-fg-4">·</span>
                            <span className="text-fg-3" title="Stop loss">SL {p.stop_loss}</span>
                          </>
                        )}
                      </div>
                    </div>
                    <span className="ml-auto flex items-center gap-2 pt-0.5 shrink-0">
                      <span
                        className={cn(
                          "font-mono text-sm tabular-nums",
                          tone === "up" && "text-success",
                          tone === "down" && "text-danger",
                          tone === "muted" && "text-fg-3",
                        )}
                      >
                        {pnl != null ? fmt.pnl(pnl) : "—"}
                      </span>
                      <ArrowRight className="h-3.5 w-3.5 text-fg-4 transition-transform group-hover:translate-x-0.5 group-hover:text-fg-2" />
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-3 max-sm:flex-col max-sm:items-start">
        <span className="font-mono text-[10px] text-fg-3">
          {rangeStart}-{rangeEnd} di {open.length}
        </span>
        <div className="flex items-center gap-2">
          <select
            value={pageSize}
            onChange={(event) => onPageSizeChange?.(Number(event.target.value))}
            className="h-8 rounded-md border border-border bg-bg-1 px-2 font-mono text-[10px] text-fg outline-none transition-colors hover:bg-bg-2 focus:border-fg-3"
            aria-label="Posizioni per pagina"
          >
            {PAGE_SIZE_OPTIONS.map((size) => (
              <option key={size} value={size}>
                {size} / pagina
              </option>
            ))}
          </select>
          <div className="flex items-center gap-1">
            <button
              className="grid h-8 w-8 place-items-center rounded-md border border-border text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
              onClick={() => onPageChange?.(safePage - 1)}
              disabled={safePage <= 1}
              title="Pagina precedente"
              type="button"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <span className="min-w-11 text-center font-mono text-[10px] text-fg-3">
              {safePage}/{totalPages}
            </span>
            <button
              className="grid h-8 w-8 place-items-center rounded-md border border-border text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
              onClick={() => onPageChange?.(safePage + 1)}
              disabled={safePage >= totalPages}
              title="Pagina successiva"
              type="button"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </Card>
  );
}

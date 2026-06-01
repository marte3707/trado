"use client";

import { useMemo } from "react";
import { Activity, TrendingDown } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { SignalFunnelChart } from "@/components/calibration/SignalFunnelChart";
import { RejectionReasonsList } from "@/components/calibration/RejectionReasonsList";
import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import { useOutcomeProfile } from "@/lib/hooks/queries";
import type {
  Calibration,
  CalibrationHistoryEntry,
  CalibrationStats,
  OutcomeProfile,
  Signal,
  SignalRejections,
  WorkerCycle,
} from "@/lib/api";

interface Props {
  calibration: Calibration;
  history: CalibrationHistoryEntry[];
  signals: Signal[];
  rejections: SignalRejections;
  workerCycles: WorkerCycle[];
}

function pct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function eventTone(eventType: string): "success" | "danger" | "warn" | "info" {
  if (eventType.includes("applied")) return "success";
  if (eventType.includes("failed")) return "danger";
  if (eventType.includes("skipped")) return "warn";
  return "info";
}

const TONE_BG: Record<string, string> = {
  success: "border-success/40 bg-success/10 text-success",
  danger: "border-danger/40 bg-danger/10 text-danger",
  warn: "border-warn/40 bg-warn/10 text-warn",
  info: "border-info/40 bg-info/10 text-info",
};

const EDGE_CHANGE_KEYS = new Set([
  "directional_score_adjustments",
  "score_bucket_adjustments",
  "market_score_adjustments",
  "timeframe_score_adjustments",
]);

function weakestRows(groups: Record<string, CalibrationStats> | undefined, limit = 5) {
  return Object.entries(groups ?? {})
    .filter(([, stats]) => stats.trades > 0)
    .sort(([, left], [, right]) => {
      const leftPf = left.profit_factor ?? Number.POSITIVE_INFINITY;
      const rightPf = right.profit_factor ?? Number.POSITIVE_INFINITY;
      return leftPf - rightPf || left.realized_pnl - right.realized_pnl;
    })
    .slice(0, limit);
}

export function DiagnosticsTab({
  calibration,
  history,
  signals,
  rejections,
  workerCycles,
}: Props) {
  const { data: outcomeProfile } = useOutcomeProfile(500);
  const overall = calibration.overall;
  const winRate = overall?.win_rate ?? null;
  const profitFactor = overall?.profit_factor ?? null;
  const realizedPnl = overall?.realized_pnl ?? null;

  const recentHistory = useMemo(() => history.slice(0, 8), [history]);
  const recentCycles = useMemo(() => workerCycles.slice(0, 5), [workerCycles]);
  const edgeChanges = useMemo(
    () => calibration.changes.filter((change) => EDGE_CHANGE_KEYS.has(change.key)),
    [calibration.changes],
  );

  return (
    <div className="space-y-4">
      {/* Top metrics */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Calibration win rate"
          value={pct(winRate)}
          tone={winRate == null ? "muted" : winRate >= 0.55 ? "success" : winRate >= 0.45 ? "muted" : "danger"}
          hint={`${overall?.trades ?? 0} trade considerati`}
        />
        <MetricCard
          label="Profit factor"
          value={profitFactor != null ? profitFactor.toFixed(2) : "—"}
          tone={profitFactor == null ? "muted" : profitFactor >= 1.5 ? "success" : profitFactor >= 1 ? "muted" : "danger"}
          hint={profitFactor != null && profitFactor < 1 ? "Sotto break-even" : "Gross profit / Gross loss"}
        />
        <MetricCard
          label="Realized PnL"
          value={fmt.pnl(realizedPnl)}
          tone={realizedPnl == null ? "muted" : realizedPnl >= 0 ? "success" : "danger"}
        />
        <MetricCard
          label="Rejections (lookback)"
          value={String(rejections.total ?? 0)}
          tone={(rejections.total ?? 0) > 0 ? "warn" : "muted"}
          hint={Object.keys(rejections.by_reason ?? {}).length + " motivi distinti"}
        />
      </div>

      {/* Funnel + rejection */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <SignalFunnelChart
          signals={signals}
          rejections={rejections}
          className="lg:col-span-2"
        />
        <RejectionReasonsList rejections={rejections} />
      </div>

      <OutcomeProfilePanel profile={outcomeProfile} edgeChanges={edgeChanges} />

      {/* Calibration history + worker cycles */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Calibration history */}
        <Card className="flex flex-col p-0 overflow-hidden">
          <div className="border-b border-border px-6 py-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Calibration history
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Ultimi cicli auto-calibration (applied / skipped / failed)
            </div>
          </div>
          <ScrollArea className="flex-1">
            {recentHistory.length === 0 ? (
              <div className="grid h-24 place-items-center text-xs text-fg-3">
                Nessuna calibrazione registrata.
              </div>
            ) : (
              <ul className="divide-y divide-border">
                {recentHistory.map((entry) => {
                  const tone = eventTone(entry.event_type);
                  const label = entry.event_type
                    .replace("auto_calibration_", "")
                    .replace(/_/g, " ");
                  return (
                    <li key={entry.id} className="flex flex-col gap-1 px-6 py-3">
                      <div className="flex items-center justify-between gap-2">
                        <Badge
                          variant="outline"
                          className={cn("font-mono uppercase", TONE_BG[tone])}
                        >
                          {label}
                        </Badge>
                        <span className="font-mono text-[10px] text-fg-3">
                          {fmt.relTime(entry.timestamp)}
                        </span>
                      </div>
                      {entry.reason && (
                        <div className="text-xs text-fg-2">{entry.reason}</div>
                      )}
                      {entry.changes.length > 0 && (
                        <div className="text-[11px] text-fg-3">
                          {entry.changes.length} change applicat{entry.changes.length === 1 ? "o" : "i"}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </ScrollArea>
        </Card>

        {/* Worker cycles */}
        <Card className="flex flex-col p-0 overflow-hidden">
          <div className="flex items-center justify-between border-b border-border px-6 py-4">
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Worker cycles
              </div>
              <div className="mt-0.5 text-xs text-fg-3">
                Ultimi cicli del worker (con errori se presenti)
              </div>
            </div>
            <Badge variant="outline" className="font-mono">
              {workerCycles.length}
            </Badge>
          </div>
          <ScrollArea className="flex-1">
            {recentCycles.length === 0 ? (
              <div className="grid h-24 place-items-center text-xs text-fg-3">
                Nessun ciclo worker registrato.
              </div>
            ) : (
              <ul className="divide-y divide-border">
                {recentCycles.map((c) => {
                  const isFailed = c.event_type === "worker_cycle_failed";
                  return (
                    <li key={c.id} className="flex flex-col gap-1.5 px-6 py-3">
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          {isFailed ? (
                            <TrendingDown className="h-3.5 w-3.5 text-danger" />
                          ) : (
                            <Activity className="h-3.5 w-3.5 text-success" />
                          )}
                          <span className="font-mono text-xs text-fg-2">
                            {isFailed ? "failed" : "cycle"}
                          </span>
                        </div>
                        <span className="font-mono text-[10px] text-fg-3">
                          {fmt.relTime(c.timestamp)}
                        </span>
                      </div>
                      <div className="grid grid-cols-4 gap-2 text-[11px]">
                        <Stat label="Markets" value={c.data.markets} />
                        <Stat label="Synced" value={c.data.synced} />
                        <Stat label="Skipped" value={c.data.skipped} />
                        <Stat label="Signals" value={c.data.generated} />
                      </div>
                      {c.data.error && (
                        <div className="rounded-md border border-danger/30 bg-danger/10 px-2 py-1 font-mono text-[10px] text-danger">
                          {c.data.error}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </ScrollArea>
        </Card>
      </div>
    </div>
  );
}

function OutcomeProfilePanel({
  profile,
  edgeChanges,
}: {
  profile: OutcomeProfile | null | undefined;
  edgeChanges: Calibration["changes"];
}) {
  const weakDirections = weakestRows(profile?.by_direction, 3);
  const weakBuckets = weakestRows(profile?.by_score_bucket, 4);
  const weakMarkets = weakestRows(profile?.by_market, 4);
  const tpRows = Object.entries(profile?.by_tp_state ?? {});
  const activePenaltyCount = edgeChanges.reduce((total, change) => {
    if (change.after && typeof change.after === "object") {
      return total + Object.keys(change.after).length;
    }
    return total;
  }, 0);

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Outcome profile
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Profilo ultimi {profile?.filters.lookback ?? 500} trade chiusi per edge calibration
          </div>
        </div>
        <Badge variant="outline" className={cn("font-mono", activePenaltyCount > 0 && "border-warn/40 bg-warn/10 text-warn")}>
          {activePenaltyCount} penalty preview
        </Badge>
      </div>
      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-4">
        <OutcomeGroup title="Direzione" rows={weakDirections} />
        <OutcomeGroup title="Score bucket" rows={weakBuckets} />
        <OutcomeGroup title="Market probation" rows={weakMarkets} />
        <OutcomeGroup title="TP profile" rows={tpRows} />
      </div>
    </Card>
  );
}

function OutcomeGroup({
  title,
  rows,
}: {
  title: string;
  rows: [string, CalibrationStats][];
}) {
  return (
    <div className="min-w-0">
      <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
        {title}
      </div>
      {rows.length === 0 ? (
        <div className="rounded border border-border px-3 py-2 text-xs text-fg-3">
          Nessun dato
        </div>
      ) : (
        <div className="space-y-1.5">
          {rows.map(([key, stats]) => (
            <div key={key} className="rounded border border-border px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-xs text-fg-2">{key}</span>
                <span
                  className={cn(
                    "font-mono text-xs tabular-nums",
                    stats.realized_pnl >= 0 ? "text-success" : "text-danger",
                  )}
                >
                  {fmt.pnl(stats.realized_pnl)}
                </span>
              </div>
              <div className="mt-1 flex items-center justify-between text-[10px] text-fg-3">
                <span>{stats.trades} trade</span>
                <span>WR {pct(stats.win_rate)}</span>
                <span>PF {stats.profit_factor != null ? stats.profit_factor.toFixed(2) : "—"}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MetricCard({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone: "success" | "danger" | "warn" | "muted";
  hint?: string;
}) {
  return (
    <Card className="p-5">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-2 font-mono text-2xl font-semibold tabular-nums",
          tone === "success" && "text-success",
          tone === "danger" && "text-danger",
          tone === "warn" && "text-warn",
          tone === "muted" && "text-fg",
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-1 text-[11px] text-fg-3">{hint}</div>}
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: number | string | undefined }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-fg-3">{label}</div>
      <div className="font-mono text-xs text-fg tabular-nums">{value ?? "—"}</div>
    </div>
  );
}

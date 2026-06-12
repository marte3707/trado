"use client";

import { useMemo, useState } from "react";
import { Activity, AlertTriangle, CheckCircle2, Clock3, MinusCircle, TrendingDown } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { SignalFunnelChart } from "@/components/calibration/SignalFunnelChart";
import { RejectionReasonsList } from "@/components/calibration/RejectionReasonsList";
import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import { useOutcomeProfile, useEquitySlope, useExpectedRCalibration, useSystemCycles } from "@/lib/hooks/queries";
import type {
  Calibration,
  CalibrationHistoryEntry,
  CalibrationStats,
  ExpectedRBucket,
  ExpectedRCalibration,
  OutcomeProfile,
  Signal,
  SignalRejections,
  SystemCycleService,
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

function computeLiveZeroCycles(cycles: WorkerCycle[]): number {
  let streak = 0;
  for (const c of cycles) {
    if (c.event_type === "worker_cycle_failed") {
      streak++;
      continue;
    }
    const activeTf = c.data.active_timeframes;
    if (Array.isArray(activeTf) && activeTf.length === 0) continue;
    if ((c.data.generated ?? 0) === 0) {
      streak++;
    } else {
      break;
    }
  }
  return streak;
}

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
  const [outcomeMode, setOutcomeMode] = useState<"paper" | "live" | null>("paper");
  const { data: outcomeProfile } = useOutcomeProfile(500, outcomeMode);
  const { data: expectedRCalibration } = useExpectedRCalibration();
  const { data: systemCycles } = useSystemCycles();
  const overall = calibration.overall;
  const winRate = overall?.win_rate ?? null;
  const profitFactor = overall?.profit_factor ?? null;
  const realizedPnl = overall?.realized_pnl ?? null;

  const recentHistory = useMemo(() => history.slice(0, 8), [history]);
  const recentCycles = useMemo(() => workerCycles.slice(0, 5), [workerCycles]);
  const lastSignal = signals[0] ?? null;
  const edgeChanges = useMemo(
    () => calibration.changes.filter((change) => EDGE_CHANGE_KEYS.has(change.key)),
    [calibration.changes],
  );

  return (
    <div className="space-y-4">
      {/* Top metrics */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
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
        <LastSignalCard signal={lastSignal} />
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

      <OutcomeProfilePanel
        profile={outcomeProfile}
        edgeChanges={edgeChanges}
        mode={outcomeMode}
        onModeChange={setOutcomeMode}
      />

      <ExpectedRCalibrationPanel data={expectedRCalibration} />

      <SystemCyclesPanel diagnostics={systemCycles} />

      <SkipReasonsPanel workerCycles={workerCycles} />

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
                      <div className="grid grid-cols-5 gap-2 text-[11px]">
                        <Stat label="Markets" value={c.data.markets} />
                        <Stat label="Synced" value={c.data.synced} />
                        <Stat label="Skipped" value={c.data.skipped} />
                        <Stat
                          label="Signals"
                          value={c.data.generated}
                          tone={(c.data.generated ?? 0) > 0 ? "success" : undefined}
                        />
                        <Stat label="Closed" value={c.data.closed} />
                      </div>
                      {Array.isArray(c.data.active_timeframes) && c.data.active_timeframes.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {c.data.active_timeframes.map((tf) => (
                            <span key={tf} className="rounded bg-surface-1 px-1.5 py-0.5 font-mono text-[10px] text-fg-3">
                              {tf}
                            </span>
                          ))}
                        </div>
                      )}
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

function statsHealth(stats: CalibrationStats): "success" | "warn" | "danger" | "muted" {
  const pf = stats.profit_factor;
  if (pf == null) return "muted";
  if (pf >= 1.5) return "success";
  if (pf >= 1.0) return "warn";
  return "danger";
}

const HEALTH_BORDER_L: Record<string, string> = {
  success: "border-l-success/60",
  warn: "border-l-warn/60",
  danger: "border-l-danger/60",
  muted: "border-l-border",
};

const HEALTH_TEXT: Record<string, string> = {
  success: "text-success",
  warn: "text-warn",
  danger: "text-danger",
  muted: "text-fg-3",
};

function WrBar({ wr }: { wr: number | null | undefined }) {
  const val = (wr ?? 0) * 100;
  const tone = (wr ?? 0) >= 0.55 ? "success" : (wr ?? 0) >= 0.45 ? "warn" : "danger";
  return (
    <div className="flex flex-1 items-center gap-1.5 min-w-0">
      <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface-1">
        <div
          className={cn(
            "h-full rounded-full",
            tone === "success" && "bg-success/70",
            tone === "warn" && "bg-warn/60",
            tone === "danger" && "bg-danger/60",
          )}
          style={{ width: `${Math.min(val, 100)}%` }}
        />
      </div>
      <span className="w-8 shrink-0 text-right font-mono text-[10px] tabular-nums text-fg-3">
        {pct(wr)}
      </span>
    </div>
  );
}

function PfPill({ pf }: { pf: number | null | undefined }) {
  if (pf == null) return <span className="font-mono text-[10px] text-fg-3">—</span>;
  const tone = pf >= 1.5 ? "success" : pf >= 1.0 ? "warn" : "danger";
  return (
    <span
      className={cn(
        "rounded px-1.5 py-0.5 font-mono text-[10px] tabular-nums font-medium",
        tone === "success" && "bg-success/15 text-success",
        tone === "warn" && "bg-warn/15 text-warn",
        tone === "danger" && "bg-danger/15 text-danger",
      )}
    >
      {pf.toFixed(2)}
    </span>
  );
}

function EmptySlot() {
  return (
    <div className="rounded border border-border px-3 py-2 text-xs text-fg-3">Nessun dato</div>
  );
}

function DirectionGroup({ rows }: { rows: [string, CalibrationStats][] }) {
  return (
    <div className="min-w-0">
      <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">Direzione</div>
      {rows.length === 0 ? (
        <EmptySlot />
      ) : (
        <div className="space-y-2">
          {rows.map(([key, stats]) => {
            const health = statsHealth(stats);
            const isLong = key.toLowerCase() === "long";
            return (
              <div
                key={key}
                className={cn(
                  "rounded-md border p-3",
                  health === "success" && "border-success/30 bg-success/5",
                  health === "warn" && "border-warn/30 bg-warn/5",
                  health === "danger" && "border-danger/30 bg-danger/5",
                  health === "muted" && "border-border",
                )}
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span
                    className={cn(
                      "text-xs font-semibold",
                      isLong ? "text-success" : "text-danger",
                    )}
                  >
                    {key.toUpperCase()}
                  </span>
                  <span
                    className={cn(
                      "font-mono text-xs tabular-nums font-semibold",
                      stats.realized_pnl >= 0 ? "text-success" : "text-danger",
                    )}
                  >
                    {fmt.pnl(stats.realized_pnl)}
                  </span>
                </div>
                <div className="grid grid-cols-3 gap-1 text-center">
                  <div>
                    <div className="text-[9px] uppercase tracking-wider text-fg-3">WR</div>
                    <div className={cn("font-mono text-[11px]", HEALTH_TEXT[health])}>
                      {pct(stats.win_rate)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[9px] uppercase tracking-wider text-fg-3">PF</div>
                    <div className={cn("font-mono text-[11px]", HEALTH_TEXT[health])}>
                      {stats.profit_factor?.toFixed(2) ?? "—"}
                    </div>
                  </div>
                  <div>
                    <div className="text-[9px] uppercase tracking-wider text-fg-3">Trade</div>
                    <div className="font-mono text-[11px] text-fg">{stats.trades}</div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function OutcomeGroup({ title, rows }: { title: string; rows: [string, CalibrationStats][] }) {
  return (
    <div className="min-w-0">
      <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</div>
      {rows.length === 0 ? (
        <EmptySlot />
      ) : (
        <div className="space-y-1.5">
          {rows.map(([key, stats]) => {
            const health = statsHealth(stats);
            return (
              <div
                key={key}
                className={cn("rounded border border-l-2 border-border px-3 py-2", HEALTH_BORDER_L[health])}
              >
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate font-mono text-xs text-fg-2">{key}</span>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <PfPill pf={stats.profit_factor} />
                    <span
                      className={cn(
                        "font-mono text-xs tabular-nums",
                        stats.realized_pnl >= 0 ? "text-success" : "text-danger",
                      )}
                    >
                      {fmt.pnl(stats.realized_pnl)}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="shrink-0 text-[10px] text-fg-3">{stats.trades}t</span>
                  <WrBar wr={stats.win_rate} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

const TP_COLORS: Record<string, string> = {
  full_tp: "bg-success",
  tp1: "bg-success/80",
  tp2: "bg-success/60",
  partial_tp: "bg-success/50",
  stop_loss: "bg-danger",
  time_stop: "bg-warn",
  liquidation: "bg-danger/90",
  manual: "bg-info/70",
};

const TP_ORDER = ["full_tp", "tp1", "tp2", "partial_tp", "stop_loss", "time_stop", "liquidation", "manual"];

function TpProfileGroup({ rows }: { rows: [string, CalibrationStats][] }) {
  const total = rows.reduce((s, [, stats]) => s + stats.trades, 0);
  const sorted = [...rows].sort(([a], [b]) => {
    const ai = TP_ORDER.indexOf(a);
    const bi = TP_ORDER.indexOf(b);
    if (ai === -1 && bi === -1) return 0;
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });

  return (
    <div className="min-w-0">
      <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">TP Profile</div>
      {rows.length === 0 ? (
        <EmptySlot />
      ) : (
        <>
          <div className="mb-3 flex h-3 overflow-hidden rounded-full">
            {sorted.map(([key, stats]) => (
              <div
                key={key}
                className={cn(TP_COLORS[key] ?? "bg-fg-3/30")}
                style={{ width: total > 0 ? `${(stats.trades / total) * 100}%` : "0%" }}
                title={`${key}: ${stats.trades} trade`}
              />
            ))}
          </div>
          <div className="space-y-1.5">
            {sorted.map(([key, stats]) => (
              <div key={key} className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-1.5">
                  <div className={cn("h-2 w-2 shrink-0 rounded-sm", TP_COLORS[key] ?? "bg-fg-3/30")} />
                  <span className="truncate font-mono text-[11px] text-fg-2">{key}</span>
                </div>
                <span className="shrink-0 font-mono text-[10px] tabular-nums text-fg-3">
                  {total > 0 ? `${((stats.trades / total) * 100).toFixed(0)}%` : "—"}
                  <span className="ml-1 text-fg-3/60">({stats.trades})</span>
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function compactDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

const SYSTEM_STATUS_TONE: Record<SystemCycleService["status"], string> = {
  ok: "border-success/40 bg-success/10 text-success",
  warn: "border-warn/40 bg-warn/10 text-warn",
  failed: "border-danger/40 bg-danger/10 text-danger",
  stale: "border-warn/40 bg-warn/10 text-warn",
  skipped: "border-border bg-surface-1 text-fg-3",
  unknown: "border-border bg-surface-1 text-fg-3",
};

function SystemStatusIcon({ status }: { status: SystemCycleService["status"] }) {
  if (status === "ok") return <CheckCircle2 className="h-3.5 w-3.5 text-success" />;
  if (status === "failed" || status === "warn") return <AlertTriangle className="h-3.5 w-3.5 text-danger" />;
  if (status === "stale") return <Clock3 className="h-3.5 w-3.5 text-warn" />;
  if (status === "skipped") return <MinusCircle className="h-3.5 w-3.5 text-fg-3" />;
  return <Activity className="h-3.5 w-3.5 text-fg-3" />;
}

function SystemCycleRow({ service }: { service: SystemCycleService }) {
  const failureText = `${service.failure_count}/${service.failure_threshold} fail`;
  return (
    <div className="grid gap-2 rounded border border-border px-3 py-2.5 sm:grid-cols-[1fr_auto] sm:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <SystemStatusIcon status={service.status} />
          <span className="min-w-0 truncate text-xs font-semibold text-fg">{service.label}</span>
          <Badge
            variant="outline"
            className={cn("shrink-0 font-mono text-[10px]", SYSTEM_STATUS_TONE[service.status])}
          >
            {service.status} ({service.state})
          </Badge>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-fg-3">
          <span>ultimo: {service.last_seen_at ? fmt.relTime(service.last_seen_at) : "—"}</span>
          <span>atteso: {compactDuration(service.expected_interval_seconds)}</span>
          <span>finestra: {compactDuration(service.failure_window_seconds)}</span>
        </div>
      </div>
      <div className="flex items-center justify-between gap-2 sm:justify-end">
        <span
          className={cn(
            "font-mono text-[11px] tabular-nums",
            service.failure_count >= service.failure_threshold ? "text-danger" : "text-fg-3",
          )}
        >
          {failureText}
        </span>
        {service.consecutive_failures > 0 && (
          <Badge variant="outline" className="font-mono text-[10px] border-warn/40 bg-warn/10 text-warn">
            streak {service.consecutive_failures}
          </Badge>
        )}
      </div>
    </div>
  );
}

function SystemCyclesPanel({
  diagnostics,
}: {
  diagnostics:
    | {
        status: "ok" | "degraded";
        degraded_count: number;
        services: SystemCycleService[];
      }
    | null
    | undefined;
}) {
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">System cycles</div>
          <div className="mt-0.5 text-xs text-fg-3">
            Stato aggregato dei loop operativi, con soglia failure per finestra temporale.
          </div>
        </div>
        <Badge
          variant="outline"
          className={cn(
            "font-mono",
            diagnostics?.status === "degraded"
              ? "border-warn/40 bg-warn/10 text-warn"
              : "border-success/40 bg-success/10 text-success",
          )}
        >
          {diagnostics?.status ?? "loading"}
          {diagnostics ? ` · ${diagnostics.degraded_count} degraded` : ""}
        </Badge>
      </div>
      <div className="mt-4 grid grid-cols-1 gap-2 lg:grid-cols-2">
        {diagnostics == null ? (
          <div className="rounded border border-border px-3 py-2 text-xs text-fg-3">
            Diagnostica cicli non disponibile.
          </div>
        ) : (
          diagnostics.services.map((service) => (
            <SystemCycleRow key={service.id} service={service} />
          ))
        )}
      </div>
    </Card>
  );
}

function OutcomeProfilePanel({
  profile,
  edgeChanges,
  mode,
  onModeChange,
}: {
  profile: OutcomeProfile | null | undefined;
  edgeChanges: Calibration["changes"];
  mode: "paper" | "live" | null;
  onModeChange: (mode: "paper" | "live" | null) => void;
}) {
  const directionRows = Object.entries(profile?.by_direction ?? {}).filter(([, s]) => s.trades > 0);
  const weakBuckets = weakestRows(profile?.by_score_bucket, 4);
  const weakMarkets = weakestRows(profile?.by_market, 4);
  const tpRows = Object.entries(profile?.by_tp_state ?? {}).filter(([, s]) => s.trades > 0);
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
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded-md border border-border text-[10px] font-medium uppercase tracking-wider">
            {([
              ["paper", "Paper"],
              ["live", "Live"],
              [null, "Tutti"],
            ] as const).map(([value, label]) => (
              <button
                key={label}
                type="button"
                onClick={() => onModeChange(value)}
                className={cn(
                  "px-2 py-1 transition-colors",
                  mode === value ? "bg-fg-3/20 text-fg-1" : "text-fg-3 hover:text-fg-2",
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <Badge
            variant="outline"
            className={cn("font-mono", activePenaltyCount > 0 && "border-warn/40 bg-warn/10 text-warn")}
          >
            {activePenaltyCount} penalty preview
          </Badge>
        </div>
      </div>
      <div className="mt-5 grid grid-cols-1 gap-6 lg:grid-cols-4">
        <DirectionGroup rows={directionRows} />
        <OutcomeGroup title="Score bucket" rows={weakBuckets} />
        <OutcomeGroup title="Market probation" rows={weakMarkets} />
        <TpProfileGroup rows={tpRows} />
      </div>
    </Card>
  );
}

const SKIP_REASON_COLORS: Record<string, string> = {
  volatility_filter: "bg-info/60",
  mtf_confluence: "bg-warn/60",
  entry_quality_filter: "bg-warn/50",
  risk_reward: "bg-danger/50",
  ob_imbalance_only: "bg-danger/40",
  whale_contra_flow: "bg-fg-3/50",
  funding_block: "bg-fg-3/50",
  insufficient_candles: "bg-fg-3/30",
  duplicate: "bg-fg-3/30",
  low_liquidity: "bg-fg-3/30",
};

function SkipReasonsPanel({ workerCycles }: { workerCycles: WorkerCycle[] }) {
  const { data: slope } = useEquitySlope();

  const aggregated = useMemo(() => {
    const totals: Record<string, number> = {};
    for (const cycle of workerCycles.slice(0, 10)) {
      for (const [key, count] of Object.entries(cycle.data.skip_reasons ?? {})) {
        totals[key] = (totals[key] ?? 0) + count;
      }
    }
    const total = Object.values(totals).reduce((s, v) => s + v, 0);
    return {
      rows: Object.entries(totals)
        .sort(([, a], [, b]) => b - a)
        .map(([reason, count]) => ({
          reason,
          count,
          share: total > 0 ? count / total : 0,
        })),
      total,
    };
  }, [workerCycles]);

  const liveZeroCycles = useMemo(() => computeLiveZeroCycles(workerCycles), [workerCycles]);

  if (aggregated.rows.length === 0) return null;

  const storedZeroCycles = slope?.consecutive_zero_signal_cycles ?? null;
  const hardCycles = slope?.exit_conditions?.starvation_hard_cycles ?? 15;
  const isStale = storedZeroCycles != null && storedZeroCycles !== liveZeroCycles;
  const displayCycles = liveZeroCycles;
  const starvationRatio = displayCycles / hardCycles;

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">Skip reasons</div>
          <div className="mt-0.5 text-xs text-fg-3">
            Analisi skip ultimi {Math.min(10, workerCycles.length)} cicli —{" "}
            {aggregated.total.toLocaleString()} skip totali
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge
            variant="outline"
            className={cn(
              "font-mono",
              starvationRatio >= 1
                ? "border-danger/40 bg-danger/10 text-danger"
                : starvationRatio >= 0.7
                  ? "border-warn/40 bg-warn/10 text-warn"
                  : "border-border text-fg-3",
            )}
          >
            {displayCycles}/{hardCycles} zero cycles
          </Badge>
          {isStale && (
            <Badge variant="outline" className="font-mono text-[10px] border-border text-fg-3" title={`Valore DB: ${storedZeroCycles} (da ultima calibrazione)`}>
              DB: {storedZeroCycles}
            </Badge>
          )}
        </div>
      </div>
      <div className="mt-4 space-y-2.5">
        {aggregated.rows.map(({ reason, count, share }) => (
          <div key={reason} className="grid grid-cols-[1fr_3rem_3rem] items-center gap-3">
            <div className="min-w-0">
              <div className="mb-1 font-mono text-[11px] text-fg-2 truncate">{reason}</div>
              <div className="h-1.5 overflow-hidden rounded-full bg-surface-1">
                <div
                  className={cn(
                    "h-full rounded-full transition-all",
                    SKIP_REASON_COLORS[reason] ?? "bg-fg-2/40",
                  )}
                  style={{ width: `${(share * 100).toFixed(1)}%` }}
                />
              </div>
            </div>
            <span className="font-mono text-[10px] tabular-nums text-fg-3 text-right">
              {(share * 100).toFixed(0)}%
            </span>
            <span className="font-mono text-[10px] tabular-nums text-fg-3 text-right">{count}</span>
          </div>
        ))}
      </div>
    </Card>
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

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string | undefined;
  tone?: "success" | "danger" | "warn";
}) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "font-mono text-xs tabular-nums",
          tone === "success" && "text-success",
          tone === "danger" && "text-danger",
          tone === "warn" && "text-warn",
          !tone && "text-fg",
        )}
      >
        {value ?? "—"}
      </div>
    </div>
  );
}

const CONFIDENCE_TONE: Record<string, string> = {
  high: "border-success/40 bg-success/10 text-success",
  medium: "border-warn/40 bg-warn/10 text-warn",
  low: "border-border bg-surface-1 text-fg-3",
};

const BUCKET_ORDER = ["50-59", "60-69", "70-79", "80+", "unknown"];

function expectedRTone(value: number | null | undefined): "success" | "warn" | "danger" | "muted" {
  if (value == null || !Number.isFinite(value)) return "muted";
  if (value >= 0.2) return "success";
  if (value >= 0) return "warn";
  return "danger";
}

function ExpectedRCalibrationPanel({ data }: { data: ExpectedRCalibration | null | undefined }) {
  const grouped = useMemo(() => {
    const groups: Record<string, ExpectedRBucket[]> = {};
    if (!data) return groups;
    for (const bucket of Object.values(data.buckets)) {
      const key = `${bucket.direction}:${bucket.timeframe}`;
      groups[key] ??= [];
      groups[key].push(bucket);
    }
    for (const list of Object.values(groups)) {
      list.sort(
        (a, b) =>
          (BUCKET_ORDER.indexOf(a.score_bucket) + 999) % 999 -
          ((BUCKET_ORDER.indexOf(b.score_bucket) + 999) % 999),
      );
    }
    return groups;
  }, [data]);

  const groupKeys = Object.keys(grouped).sort();
  const totalSamples = useMemo(
    () =>
      data
        ? Object.values(data.buckets).reduce((sum, b) => sum + b.n_samples, 0)
        : 0,
    [data],
  );

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Expected R calibration
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Mappa empirica score → Expected R-multiple, con Bayesian shrinkage (prior{" "}
            {data?.prior_strength ?? "—"}) su {data?.lookback_positions ?? "—"} posizioni paper.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono">
            {totalSamples} sample
          </Badge>
          {data?.computed_at && (
            <Badge variant="outline" className="font-mono text-[10px] border-border text-fg-3">
              {fmt.relTime(data.computed_at)}
            </Badge>
          )}
        </div>
      </div>
      {!data || groupKeys.length === 0 ? (
        <div className="mt-5 text-xs text-fg-3">
          Nessuna posizione paper disponibile per calibrare i bucket.
        </div>
      ) : (
        <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {groupKeys.map((groupKey) => {
            const buckets = grouped[groupKey];
            const [direction, timeframe] = groupKey.split(":");
            const isLong = direction === "LONG";
            return (
              <div key={groupKey} className="rounded-md border border-border p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span
                      className={cn(
                        "font-mono text-xs font-semibold",
                        isLong ? "text-success" : "text-danger",
                      )}
                    >
                      {direction}
                    </span>
                    <span className="font-mono text-[11px] text-fg-3">{timeframe}</span>
                  </div>
                  <span className="font-mono text-[10px] text-fg-3">
                    {buckets.reduce((s, b) => s + b.n_samples, 0)} sample
                  </span>
                </div>
                <div className="space-y-1.5">
                  {buckets.map((bucket) => {
                    const tone = expectedRTone(bucket.expected_r);
                    return (
                      <div
                        key={bucket.key}
                        className="grid grid-cols-[3.5rem_1fr_3rem_2.5rem] items-center gap-2"
                      >
                        <span className="font-mono text-[11px] text-fg-2">
                          {bucket.score_bucket}
                        </span>
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span
                            className={cn(
                              "font-mono text-[11px] tabular-nums",
                              tone === "success" && "text-success",
                              tone === "warn" && "text-warn",
                              tone === "danger" && "text-danger",
                              tone === "muted" && "text-fg-3",
                            )}
                          >
                            {bucket.expected_r != null
                              ? `${bucket.expected_r >= 0 ? "+" : ""}${bucket.expected_r.toFixed(2)}R`
                              : "—"}
                          </span>
                          <span className="font-mono text-[10px] text-fg-3">
                            WR {pct(bucket.shrunk_win_rate)}
                          </span>
                        </div>
                        <span className="font-mono text-[10px] tabular-nums text-fg-3 text-right">
                          n={bucket.n_samples}
                        </span>
                        <span
                          className={cn(
                            "rounded border px-1.5 py-0.5 text-center font-mono text-[9px] uppercase",
                            CONFIDENCE_TONE[bucket.confidence] ?? "border-border text-fg-3",
                          )}
                          title={`Confidence: ${bucket.confidence} (min ${data.min_samples_for_use} sample)`}
                        >
                          {bucket.confidence}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function LastSignalCard({ signal }: { signal: Signal | null }) {
  if (!signal) {
    return (
      <Card className="p-5">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">Ultimo segnale</div>
        <div className="mt-2 font-mono text-sm text-fg-3">—</div>
      </Card>
    );
  }

  const isLong = signal.direction.toUpperCase() === "LONG";

  return (
    <Card className="p-5">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">Ultimo segnale</div>
      <div className="mt-2 flex items-baseline gap-1.5 min-w-0">
        <span className="truncate font-mono text-sm font-semibold text-fg">
          {signal.market?.symbol ?? "—"}
        </span>
        <span className="shrink-0 font-mono text-[11px] text-fg-3">{signal.timeframe}</span>
      </div>
      <div className="mt-1 flex items-center gap-2">
        <span
          className={cn(
            "font-mono text-xs font-semibold",
            isLong ? "text-success" : "text-danger",
          )}
        >
          {signal.direction.toUpperCase()}
        </span>
        <span className="font-mono text-xs text-fg-3">score {signal.score}</span>
      </div>
      <div className="mt-1 font-mono text-[10px] text-fg-3">{fmt.relTime(signal.created_at)}</div>
    </Card>
  );
}

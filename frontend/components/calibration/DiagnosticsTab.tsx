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
import {
  useOutcomeProfile,
  useEquitySlope,
  useExpectedRCalibration,
  useLiveEligibility,
  useMacroDirection,
  useSignalCandidateRejections,
  useStrategies,
  useSystemCycles,
  useStrategyEdge,
} from "@/lib/hooks/queries";
import type {
  CalibrationStats,
  AppSettings,
  ExpectedRBucket,
  ExpectedRCalibration,
  LiveEligibilityClass,
  LiveEligibilityCohort,
  LiveEligibilityReport,
  OutcomeProfile,
  Signal,
  SignalCandidateRejectionSample,
  SignalRejections,
  StrategyEdgeCell,
  StrategyEdgeReport,
  StrategyEdgeStrategy,
  SystemCycleService,
  MacroDirectionStatus,
  WorkerCycle,
} from "@/lib/api";

interface Props {
  signals: Signal[];
  rejections: SignalRejections;
  workerCycles: WorkerCycle[];
  settings: AppSettings;
}

function pct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
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
  signals,
  rejections,
  workerCycles,
  settings,
}: Props) {
  const [outcomeMode, setOutcomeMode] = useState<"paper" | "live" | null>("paper");
  const { data: outcomeProfile } = useOutcomeProfile(500, outcomeMode);
  const { data: expectedRCalibration } = useExpectedRCalibration();
  const { data: strategyEdge } = useStrategyEdge();
  const { data: liveEligibility } = useLiveEligibility();
  const { data: systemCycles } = useSystemCycles();
  const { data: macroDirection } = useMacroDirection();
  const { data: strategies = [] } = useStrategies();
  const overall = outcomeProfile?.overall;
  const winRate = overall?.win_rate ?? null;
  const profitFactor = overall?.profit_factor ?? null;
  const realizedPnl = overall?.realized_pnl ?? null;

  const recentCycles = useMemo(() => workerCycles.slice(0, 5), [workerCycles]);
  const lastSignal = signals[0] ?? null;

  return (
    <div className="space-y-4">
      {/* Top metrics */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <MetricCard
          label="Outcome win rate"
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

      <SignalGenerationPanel settings={settings} macroDirection={macroDirection} enabledStrategies={strategies.filter((strategy) => strategy.enabled).length} totalStrategies={strategies.length} />

      <OutcomeProfilePanel
        profile={outcomeProfile}
        mode={outcomeMode}
        onModeChange={setOutcomeMode}
      />

      <StrategyEdgePanel data={strategyEdge} />

      <LiveEligibilityPanel data={liveEligibility} />

      <ExpectedRCalibrationPanel data={expectedRCalibration} />

      <SystemCyclesPanel diagnostics={systemCycles} />

      <SkipReasonsPanel workerCycles={workerCycles} />

      <CandidateRejectionSamplesPanel />

      <Card className="flex flex-col overflow-hidden p-0">
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
  );
}

function CandidateRejectionSamplesPanel() {
  const { data } = useSignalCandidateRejections(30);
  const samples = data?.recent ?? [];

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Candidate score samples
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Campione dei candidati respinti: consenso reale e confronto shadow, senza modificare i gate.
          </div>
        </div>
        <Badge variant="outline" className="font-mono">
          {data?.total ?? 0} sample
        </Badge>
      </div>
      {samples.length === 0 ? (
        <div className="mt-4 rounded border border-border px-3 py-2 text-xs text-fg-3">
          I campioni compariranno ai prossimi rifiuti per score. Il shadow è solo diagnostico.
        </div>
      ) : (
        <div className="mt-4 space-y-2">
          {samples.map((sample) => <CandidateRejectionRow key={sample.id} sample={sample} />)}
        </div>
      )}
    </Card>
  );
}

function CandidateRejectionRow({ sample }: { sample: SignalCandidateRejectionSample }) {
  const shadow = sample.participation_shadow;
  const pipeline = sample.threshold_pipeline;
  const components = pipeline
    ? Object.entries(pipeline.edge_components).filter(([, value]) => value > 0)
    : [];
  return (
    <div className="grid gap-2 rounded border border-border px-3 py-2.5 sm:grid-cols-[1fr_auto] sm:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs text-fg">{sample.market} · {sample.timeframe}</span>
          <Badge variant="outline" className="font-mono text-[10px]">{sample.reason}</Badge>
          <span className="font-mono text-[10px] text-fg-3">{fmt.relTime(sample.timestamp)}</span>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-fg-3">
          <span>score {sample.score}/{sample.effective_min_score}</span>
          {pipeline && <span>base {pipeline.base_min_score} + edge {pipeline.edge_total} → {pipeline.final_min_score}</span>}
          <span>vote {(sample.voting_ratio * 100).toFixed(0)}% ({sample.voting_weight}/{sample.all_weight})</span>
          {sample.neutralized_strategies.length > 0 && <span>neutralizzate: {sample.neutralized_strategies.join(", ")}</span>}
          {components.length > 0 && <span>componenti: {components.map(([key, value]) => `${key} +${value}`).join(", ")}</span>}
        </div>
      </div>
      {shadow && (
        <Badge
          variant="outline"
          className={cn(
            "justify-self-start font-mono text-[10px] sm:justify-self-end",
            shadow.passes_score_gate ? "border-warn/40 bg-warn/10 text-warn" : "border-border text-fg-3",
          )}
        >
          shadow {(shadow.ratio * 100).toFixed(0)}%: {shadow.score}/{sample.effective_min_score}
          {shadow.passes_score_gate ? " score-pass" : " score-block"}
        </Badge>
      )}
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function asNum(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function penaltyRows(value: unknown, source: string) {
  return Object.entries(asRecord(value)).flatMap(([key, raw]) => {
    const payload = asRecord(raw);
    const adjustment = asNum(payload.adjustment ?? raw);
    return adjustment > 0 ? [{ key, source, adjustment, stats: asRecord(payload.stats) }] : [];
  });
}

function SignalGenerationPanel({
  settings,
  macroDirection,
  enabledStrategies,
  totalStrategies,
}: {
  settings: AppSettings;
  macroDirection: MacroDirectionStatus | null | undefined;
  enabledStrategies: number;
  totalStrategies: number;
}) {
  const risk = asRecord(settings.risk);
  const penalties = [
    ...penaltyRows(risk.directional_score_adjustments, "direction"),
    ...penaltyRows(risk.score_bucket_adjustments, "score bucket"),
    ...penaltyRows(risk.market_score_adjustments, "market"),
    ...penaltyRows(risk.timeframe_score_adjustments, "timeframe"),
  ].sort((left, right) => right.adjustment - left.adjustment || left.key.localeCompare(right.key));
  const enabled = risk.edge_penalties_enabled !== false;
  const baseScore = asNum(risk.min_signal_score, 60);
  const participation = asNum(risk.participation_ratio, 0.35);
  const cap = asNum(risk.max_combined_score_adjustment, 15);
  const minMarketTrades = asNum(risk.edge_penalty_min_market_trades, 20);
  const minExpectedR = risk.min_signal_expected_r == null ? null : asNum(risk.min_signal_expected_r);

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">Signal generation control tower</div>
          <div className="mt-0.5 text-xs text-fg-3">Configurazione runtime che trasforma un candidato in segnale. Non include i guardrail di esecuzione live.</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge variant="outline" className={cn("font-mono", enabled ? "border-warn/40 bg-warn/10 text-warn" : "border-border text-fg-3")}>
            edge penalties {enabled ? "on" : "suspended"}
          </Badge>
          {macroDirection && <Badge variant="outline" className={cn("font-mono", macroDirection.direction === "bullish" ? "border-success/40 bg-success/10 text-success" : "border-border text-fg-3")}>
            BTC {macroDirection.direction} · {macroDirection.timeframe} · {macroDirection.suspended_family ? `${macroDirection.suspended_family} penalties suspended` : "no suspension"}
          </Badge>}
        </div>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-2 lg:grid-cols-7">
        <ControlMetric label="Base score" value={String(baseScore)} />
        <ControlMetric label="Consensus" value={`${(participation * 100).toFixed(0)}%`} />
        <ControlMetric label="Edge cap" value={`+${cap}`} />
        <ControlMetric label="Penalty rules" value={String(penalties.length)} />
        <ControlMetric label="Market evidence" value={`${minMarketTrades}t`} />
        <ControlMetric label="Min Expected R" value={minExpectedR == null ? "off" : minExpectedR.toFixed(2)} />
        <ControlMetric label="Strategies" value={`${enabledStrategies}/${totalStrategies}`} />
      </div>
      <div className="mt-4 rounded border border-border bg-bg-2 px-3 py-2 font-mono text-[11px] text-fg-2">
        pipeline: consensus → score → base {baseScore} + edge adjustment (max +{cap}) → altri filtri → signal
      </div>
      <div className="mt-4 space-y-1.5">
        {penalties.length === 0 ? (
          <div className="rounded border border-border px-3 py-2 text-xs text-fg-3">Nessuna edge penalty attiva in configurazione.</div>
        ) : penalties.map((penalty) => {
          const trades = asNum(penalty.stats.trades);
          const pf = penalty.stats.profit_factor == null ? null : asNum(penalty.stats.profit_factor);
          const insufficientMarketEvidence = penalty.source === "market" && trades < minMarketTrades;
          return (
            <div key={`${penalty.source}:${penalty.key}`} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded border border-border px-3 py-2 text-[11px]">
              <div className="min-w-0"><span className="font-mono text-fg">{penalty.key}</span><span className="ml-2 text-fg-3">{penalty.source}</span></div>
              <div className="flex gap-3 font-mono text-fg-3"><span className={insufficientMarketEvidence ? "text-fg-3 line-through" : "text-warn"}>+{penalty.adjustment}</span><span>{trades > 0 ? `${trades} trade` : "manual"}</span><span>PF {pf == null ? "—" : pf.toFixed(2)}</span>{insufficientMarketEvidence && <span className="text-info">observe only</span>}</div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function ControlMetric({ label, value }: { label: string; value: string }) {
  return <div className="rounded border border-border bg-bg-2 px-3 py-2"><div className="text-[9px] uppercase tracking-wider text-fg-3">{label}</div><div className="mt-1 font-mono text-sm font-semibold text-fg">{value}</div></div>;
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
  mode,
  onModeChange,
}: {
  profile: OutcomeProfile | null | undefined;
  mode: "paper" | "live" | null;
  onModeChange: (mode: "paper" | "live" | null) => void;
}) {
  const directionRows = Object.entries(profile?.by_direction ?? {}).filter(([, s]) => s.trades > 0);
  const weakBuckets = weakestRows(profile?.by_score_bucket, 4);
  const weakMarkets = weakestRows(profile?.by_market, 4);
  const tpRows = Object.entries(profile?.by_tp_state ?? {}).filter(([, s]) => s.trades > 0);
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Outcome profile
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Profilo degli ultimi {profile?.filters.lookback ?? 500} trade chiusi
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

  if (aggregated.rows.length === 0) return null;

  const displayCycles = slope?.consecutive_zero_signal_cycles ?? 0;

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
              displayCycles > 0
                ? "border-warn/40 bg-warn/10 text-warn"
                : "border-border text-fg-3",
            )}
          >
            {displayCycles} zero cycles
          </Badge>
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

function pfText(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(2);
}

function edgeTone(classification: string): string {
  if (classification === "robust") return "border-success/40 bg-success/10 text-success";
  if (classification === "promising") return "border-info/40 bg-info/10 text-info";
  if (classification === "negative") return "border-danger/40 bg-danger/10 text-danger";
  if (classification === "inert") return "border-warn/40 bg-warn/10 text-warn";
  return "border-border bg-surface-1 text-fg-3";
}

function eligibilityTone(classification: LiveEligibilityClass): string {
  if (classification === "eligible") return "border-success/40 bg-success/10 text-success";
  if (classification === "watch") return "border-info/40 bg-info/10 text-info";
  if (classification === "quarantine") return "border-danger/40 bg-danger/10 text-danger";
  return "border-border bg-surface-1 text-fg-3";
}

function rText(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}R`;
}

function eligibilitySortValue(cohort: LiveEligibilityCohort) {
  return cohort.recent.avg_net_r ?? cohort.full.avg_net_r ?? Number.NEGATIVE_INFINITY;
}

function LiveEligibilityRow({ cohort }: { cohort: LiveEligibilityCohort }) {
  return (
    <div className="grid grid-cols-[1fr_4rem_4rem] items-center gap-2 rounded border border-border px-3 py-2">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <span
            className={cn(
              "shrink-0 font-mono text-[10px] font-semibold",
              cohort.direction === "LONG" ? "text-success" : "text-danger",
            )}
          >
            {cohort.direction}
          </span>
          <span className="min-w-0 truncate font-mono text-xs text-fg">{cohort.key}</span>
        </div>
        <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-1.5">
          <Badge variant="outline" className={cn("font-mono text-[9px] uppercase", eligibilityTone(cohort.classification))}>
            {cohort.classification}
          </Badge>
          <span className="font-mono text-[10px] text-fg-3">
            n {cohort.full.trades}/{cohort.recent.trades} rec
          </span>
          {cohort.active_risk_overlays.length > 0 && (
            <span className="rounded bg-warn/10 px-1.5 py-0.5 font-mono text-[9px] text-warn">
              risk {cohort.active_risk_overlays.length}
            </span>
          )}
          {cohort.reasons[0] && (
            <span className="min-w-0 truncate text-[10px] text-fg-3">{cohort.reasons[0]}</span>
          )}
        </div>
      </div>
      <div className="font-mono text-[11px] text-fg-2">PF {pfText(cohort.recent.net_profit_factor)}</div>
      <div
        className={cn(
          "text-right font-mono text-[11px]",
          (cohort.recent.avg_net_r ?? cohort.full.avg_net_r ?? 0) >= 0 ? "text-success" : "text-danger",
        )}
      >
        {rText(cohort.recent.avg_net_r ?? cohort.full.avg_net_r)}
      </div>
    </div>
  );
}

function LiveEligibilityPanel({ data }: { data: LiveEligibilityReport | null | undefined }) {
  const cohorts = useMemo(
    () => [...(data?.direction_timeframes ?? []), ...(data?.strategies ?? [])],
    [data],
  );
  const eligible = useMemo(
    () =>
      cohorts
        .filter((cohort) => cohort.classification === "eligible")
        .sort((a, b) => eligibilitySortValue(b) - eligibilitySortValue(a))
        .slice(0, 6),
    [cohorts],
  );
  const watch = useMemo(
    () =>
      cohorts
        .filter((cohort) => cohort.classification === "watch")
        .sort((a, b) => eligibilitySortValue(b) - eligibilitySortValue(a))
        .slice(0, 6),
    [cohorts],
  );
  const quarantine = useMemo(
    () =>
      cohorts
        .filter((cohort) => cohort.classification === "quarantine")
        .sort((a, b) => eligibilitySortValue(a) - eligibilitySortValue(b))
        .slice(0, 6),
    [cohorts],
  );
  const excluded = useMemo(
    () =>
      Object.values(data?.summary.excluded ?? {}).reduce(
        (sum, value) => sum + Number(value || 0),
        0,
      ),
    [data],
  );
  const macroDirection = String(data?.macro_context.direction ?? "unknown");
  const conflicts = data?.setting_conflicts ?? [];

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Live eligibility preview
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Paper-first: R netto stimato dopo fee taker round-trip, senza modificare live execution.
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="font-mono text-[10px] border-border text-fg-3">
            macro {macroDirection}
          </Badge>
          <Badge variant="outline" className="font-mono text-[10px] border-border text-fg-3">
            fee {(Number(data?.filters.taker_fee_rate ?? 0) * 100).toFixed(3)}%
          </Badge>
          <Badge variant="outline" className="font-mono text-[10px] border-border text-fg-3">
            usable {data?.summary.paper_trades_usable ?? "—"}/{data?.summary.paper_trades_loaded ?? "—"}
          </Badge>
          {data && (
            <Badge
              variant="outline"
              className={cn(
                "font-mono text-[10px]",
                conflicts.length > 0 ? "border-warn/40 bg-warn/10 text-warn" : "border-border text-fg-3",
              )}
            >
              conflicts {conflicts.length}
            </Badge>
          )}
        </div>
      </div>

      {!data ? (
        <div className="mt-5 text-xs text-fg-3">Eligibility report non disponibile.</div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
            <MetricChip label="Eligible" value={data.summary.eligible ?? 0} tone="success" />
            <MetricChip label="Watch" value={data.summary.watch ?? 0} tone="info" />
            <MetricChip label="Quarantine" value={data.summary.quarantine ?? 0} tone="danger" />
            <MetricChip label="Insufficient" value={data.summary.insufficient ?? 0} tone="muted" />
            <MetricChip label="Excluded" value={excluded} tone={excluded > 0 ? "muted" : "success"} />
          </div>

          {conflicts.length > 0 && (
            <div className="mt-4 rounded-md border border-warn/30 bg-warn/5 p-3">
              <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-warn">
                Setting conflicts
              </div>
              <div className="space-y-1.5">
                {conflicts.slice(0, 4).map((conflict) => (
                  <div key={`${conflict.type}:${conflict.cohort_key}`} className="flex flex-wrap items-center gap-2 text-[11px]">
                    <Badge variant="outline" className={cn("font-mono text-[9px]", conflict.severity === "warn" ? "border-warn/40 text-warn" : "border-info/40 text-info")}>
                      {conflict.type}
                    </Badge>
                    <span className="font-mono text-fg-2">{conflict.cohort_key}</span>
                    <span className="text-fg-3">{conflict.active_risk_overlays.map((item) => item.key).join(", ")}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-3">
            <div>
              <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Candidati live
              </div>
              <div className="space-y-2">
                {eligible.length > 0 ? (
                  eligible.map((cohort) => <LiveEligibilityRow key={cohort.key} cohort={cohort} />)
                ) : (
                  <div className="rounded border border-border px-3 py-3 text-xs text-fg-3">
                    Nessuna coorte eleggibile con le soglie correnti.
                  </div>
                )}
              </div>
            </div>
            <div>
              <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Watchlist
              </div>
              <div className="space-y-2">
                {watch.length > 0 ? (
                  watch.map((cohort) => <LiveEligibilityRow key={cohort.key} cohort={cohort} />)
                ) : (
                  <div className="rounded border border-border px-3 py-3 text-xs text-fg-3">
                    Nessuna coorte in watchlist.
                  </div>
                )}
              </div>
            </div>
            <div>
              <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Quarantena paper-only
              </div>
              <div className="space-y-2">
                {quarantine.length > 0 ? (
                  quarantine.map((cohort) => <LiveEligibilityRow key={cohort.key} cohort={cohort} />)
                ) : (
                  <div className="rounded border border-border px-3 py-3 text-xs text-fg-3">
                    Nessuna coorte in quarantena.
                  </div>
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

function cellSortValue(cell: StrategyEdgeCell) {
  return cell.test?.realized_pnl ?? cell.full.realized_pnl;
}

function strategySortValue(strategy: StrategyEdgeStrategy) {
  return strategy.recent?.realized_pnl ?? strategy.overall.realized_pnl;
}

function EdgeCellRow({ cell }: { cell: StrategyEdgeCell }) {
  return (
    <div className="grid grid-cols-[1fr_3rem_4rem_4rem] items-center gap-2 rounded border border-border px-3 py-2">
      <div className="min-w-0">
        <div className="truncate font-mono text-xs text-fg">{cell.key}</div>
        <div className="mt-0.5 flex items-center gap-1.5">
          <Badge variant="outline" className={cn("font-mono text-[9px] uppercase", edgeTone(cell.classification))}>
            {cell.classification}
          </Badge>
          <span className="font-mono text-[10px] text-fg-3">{cell.verdict}</span>
          {!cell.enabled && <span className="font-mono text-[10px] text-fg-3">off</span>}
        </div>
      </div>
      <div className="font-mono text-[11px] text-fg-2">n={cell.test?.trades ?? 0}</div>
      <div className="font-mono text-[11px] text-fg-2">PF {pfText(cell.test?.profit_factor)}</div>
      <div
        className={cn(
          "text-right font-mono text-[11px]",
          (cell.test?.realized_pnl ?? 0) >= 0 ? "text-success" : "text-danger",
        )}
      >
        {fmt.pnl(cell.test?.realized_pnl)}
      </div>
    </div>
  );
}

function EdgeStrategyRow({ strategy }: { strategy: StrategyEdgeStrategy }) {
  return (
    <div className="grid grid-cols-[1fr_4rem_4rem] items-center gap-2 rounded border border-border px-3 py-2">
      <div className="min-w-0">
        <div className="truncate font-mono text-xs text-fg">{strategy.name}</div>
        <div className="mt-0.5 flex items-center gap-1.5">
          <Badge variant="outline" className={cn("font-mono text-[9px] uppercase", edgeTone(strategy.classification))}>
            {strategy.classification}
          </Badge>
          <span className="font-mono text-[10px] text-fg-3">
            share {strategy.participation.traded_share != null ? pct(strategy.participation.traded_share) : "—"}
          </span>
        </div>
      </div>
      <div className="font-mono text-[11px] text-fg-2">PF {pfText(strategy.recent?.profit_factor)}</div>
      <div
        className={cn(
          "text-right font-mono text-[11px]",
          (strategy.recent?.realized_pnl ?? 0) >= 0 ? "text-success" : "text-danger",
        )}
      >
        {fmt.pnl(strategy.recent?.realized_pnl)}
      </div>
    </div>
  );
}

function StrategyEdgePanel({ data }: { data: StrategyEdgeReport | null | undefined }) {
  const negativeCells = useMemo(
    () =>
      (data?.cells ?? [])
        .filter((cell) => cell.classification === "negative")
        .sort((a, b) => cellSortValue(a) - cellSortValue(b))
        .slice(0, 6),
    [data],
  );
  const promisingCells = useMemo(
    () =>
      (data?.cells ?? [])
        .filter((cell) => cell.classification === "robust" || cell.classification === "promising")
        .sort((a, b) => cellSortValue(b) - cellSortValue(a))
        .slice(0, 6),
    [data],
  );
  const negativeStrategies = useMemo(
    () =>
      (data?.strategies ?? [])
        .filter((strategy) => strategy.classification === "negative")
        .sort((a, b) => strategySortValue(a) - strategySortValue(b))
        .slice(0, 6),
    [data],
  );

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            OOS / Strategy edge
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Walk-forward 70/30 su {data?.overall.full.trades ?? "—"} trade paper · recent window{" "}
            {data?.filters.recent_window ?? "—"}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className={cn("font-mono uppercase", edgeTone(data?.overall.classification ?? "insufficient"))}>
            {data?.overall.classification ?? "loading"}
          </Badge>
          <Badge variant="outline" className="font-mono text-[10px] border-border text-fg-3">
            test PF {pfText(data?.overall.test?.profit_factor)}
          </Badge>
          <Badge variant="outline" className="font-mono text-[10px] border-border text-fg-3">
            TW PF {pfText(data?.overall.time_weighted.profit_factor)}
          </Badge>
        </div>
      </div>

      {!data ? (
        <div className="mt-5 text-xs text-fg-3">Edge report non disponibile.</div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <MetricChip label="Robuste" value={data.summary.cells.robust ?? 0} tone="success" />
            <MetricChip label="Promettenti" value={data.summary.cells.promising ?? 0} tone="info" />
            <MetricChip label="Negative" value={data.summary.cells.negative ?? 0} tone="danger" />
            <MetricChip label="Strategie neg." value={data.summary.strategies.negative ?? 0} tone="danger" />
          </div>

          <div className="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-3">
            <div>
              <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Celle negative
              </div>
              <div className="space-y-2">
                {negativeCells.length > 0 ? (
                  negativeCells.map((cell) => <EdgeCellRow key={cell.key} cell={cell} />)
                ) : (
                  <div className="rounded border border-border px-3 py-3 text-xs text-fg-3">Nessuna cella negativa.</div>
                )}
              </div>
            </div>
            <div>
              <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Celle promettenti
              </div>
              <div className="space-y-2">
                {promisingCells.length > 0 ? (
                  promisingCells.map((cell) => <EdgeCellRow key={cell.key} cell={cell} />)
                ) : (
                  <div className="rounded border border-border px-3 py-3 text-xs text-fg-3">Nessuna cella robusta.</div>
                )}
              </div>
            </div>
            <div>
              <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Strategie deboli recenti
              </div>
              <div className="space-y-2">
                {negativeStrategies.length > 0 ? (
                  negativeStrategies.map((strategy) => (
                    <EdgeStrategyRow key={strategy.name} strategy={strategy} />
                  ))
                ) : (
                  <div className="rounded border border-border px-3 py-3 text-xs text-fg-3">Nessuna strategia negativa.</div>
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

function MetricChip({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "success" | "info" | "danger" | "muted";
}) {
  const toneClass =
    tone === "success"
      ? "border-success/40 bg-success/10 text-success"
      : tone === "info"
        ? "border-info/40 bg-info/10 text-info"
        : tone === "danger"
          ? "border-danger/40 bg-danger/10 text-danger"
          : "border-border bg-surface-1 text-fg-3";
  return (
    <div className={cn("rounded border px-3 py-2", toneClass)}>
      <div className="text-[10px] uppercase tracking-wider opacity-80">{label}</div>
      <div className="mt-1 font-mono text-lg font-semibold leading-none">{value}</div>
    </div>
  );
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

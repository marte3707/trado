"use client";

import { Activity, AlertTriangle, Info, Loader2, ShieldCheck, TrendingDown, TrendingUp } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { fmt } from "@/components/atoms";
import { useEquitySlope } from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";
import type { EquitySlopeDiagnostics } from "@/lib/api";

type Tone = "success" | "warn" | "danger" | "muted" | "info";

const TONE_CLASS: Record<Tone, string> = {
  success: "border-success/40 bg-success/10 text-success",
  warn: "border-warn/40 bg-warn/10 text-warn",
  danger: "border-danger/40 bg-danger/10 text-danger",
  muted: "border-border bg-bg-2 text-fg-3",
  info: "border-info/40 bg-info/10 text-info",
};

function statusTone(status: EquitySlopeDiagnostics["status"]): Tone {
  if (status === "improving") return "success";
  if (status === "declining") return "danger";
  return "muted";
}

function slopeTone(value: number, threshold: number): Tone {
  if (!Number.isFinite(value)) return "muted";
  if (value >= 0) return "success";
  if (value <= threshold) return "danger";
  return "warn";
}

function formatSlope(value: number | null | undefined, digits = 3) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function formatReason(reason: string | null | undefined) {
  if (!reason) return "Nessuna uscita registrata";
  return reason.replace(/_/g, " ");
}

export function EquitySlopeTab() {
  const { data, isLoading } = useEquitySlope();

  if (isLoading) {
    return (
      <Card className="grid h-64 place-items-center">
        <div className="flex items-center gap-2 text-fg-3">
          <Loader2 className="h-4 w-4 animate-spin" />
          Caricamento equity slope diagnostics...
        </div>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card className="grid h-36 place-items-center text-sm text-fg-3">
        Equity slope diagnostics non disponibili.
      </Card>
    );
  }

  const tone = statusTone(data.status);
  const StatusIcon =
    data.status === "improving" ? TrendingUp : data.status === "declining" ? TrendingDown : Activity;

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-4">
        <Card className="p-0 overflow-hidden">
          <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border px-6 py-5">
            <div>
              <div className="flex items-center gap-2">
                <Badge variant="outline" className={cn("font-mono uppercase", TONE_CLASS[tone])}>
                  {data.status}
                </Badge>
                {data.deep_defensive_active && (
                  <Badge variant="outline" className={cn("font-mono uppercase", TONE_CLASS.warn)}>
                    deep defensive
                  </Badge>
                )}
                {data.starvation_override_active && (
                  <Badge variant="outline" className={cn("font-mono uppercase", TONE_CLASS.warn)}>
                    starvation override
                  </Badge>
                )}
              </div>
              <div className="mt-3 text-lg font-semibold text-fg">Equity slope diagnostics</div>
              <div className="mt-1 text-xs text-fg-3">
                Trend normalizzato su {data.sample_count} trade nel lookback {data.lookback}.
              </div>
            </div>
            <div className={cn("rounded-lg border p-3", TONE_CLASS[tone])}>
              <StatusIcon className="h-5 w-5" />
            </div>
          </div>

          <div className="grid gap-3 p-5 sm:grid-cols-2 xl:grid-cols-4">
            <MetricTile
              label="Normalized slope"
              value={formatSlope(data.normalized_slope)}
              tone={slopeTone(data.normalized_slope, data.threshold)}
              hint={`Soglia ${formatSlope(data.threshold)}`}
            />
            <MetricTile
              label="Recent slope"
              value={formatSlope(data.recent_normalized_slope)}
              tone={slopeTone(data.recent_normalized_slope, data.threshold)}
              hint="Finestra recente"
            />
            <MetricTile
              label="Raw slope"
              value={formatSlope(data.raw_slope, 4)}
              tone={slopeTone(data.normalized_slope, data.threshold)}
              hint="Slope non normalizzato"
            />
            <MetricTile
              label="Median abs PnL"
              value={fmt.money(data.median_abs_pnl)}
              tone="muted"
              hint="Scala usata per normalizzare"
            />
          </div>
        </Card>

        <Card className="p-0 overflow-hidden">
          <div className="border-b border-border px-6 py-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Runtime guardrails
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Stato operativo che influenza deep defensive, starvation e cooldown.
            </div>
          </div>
          <div className="grid gap-3 p-5 sm:grid-cols-2 xl:grid-cols-3">
            <GuardTile
              label="Deep defensive"
              value={data.deep_defensive_active ? "Attivo" : "Normale"}
              tone={data.deep_defensive_active ? "warn" : "success"}
            />
            <GuardTile
              label="Starvation override"
              value={data.starvation_override_active ? "Attivo" : "Inattivo"}
              tone={data.starvation_override_active ? "warn" : "muted"}
            />
            <GuardTile
              label="Zero signal cycles"
              value={`${data.consecutive_zero_signal_cycles}/${data.exit_conditions.starvation_hard_cycles}`}
              tone={
                data.consecutive_zero_signal_cycles >= data.exit_conditions.starvation_hard_cycles
                  ? "warn"
                  : "muted"
              }
            />
            <GuardTile
              label="Cooldown"
              value={String(data.cooldown_remaining)}
              tone={data.cooldown_remaining > 0 ? "warn" : "muted"}
            />
            <GuardTile
              label="Recent positive streak"
              value={`${data.recent_positive_streak}/${data.exit_conditions.recent_slope_non_negative_cycles}`}
              tone={
                data.recent_positive_streak >= data.exit_conditions.recent_slope_non_negative_cycles
                  ? "success"
                  : "muted"
              }
            />
            <GuardTile
              label="Last exit"
              value={formatReason(data.last_exit_reason)}
              tone={data.last_exit_reason ? "info" : "muted"}
            />
          </div>
        </Card>

        <Card className="p-0 overflow-hidden">
          <div className="border-b border-border px-6 py-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Multi-window analysis
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Lettura della slope su finestre diverse per distinguere trend breve e storico.
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[680px] border-separate border-spacing-0">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-fg-3">
                  <th className="px-6 py-3 font-medium">Window</th>
                  <th className="px-4 py-3 font-medium">Samples</th>
                  <th className="px-4 py-3 font-medium">Normalized</th>
                  <th className="px-4 py-3 font-medium">Raw slope</th>
                  <th className="px-4 py-3 font-medium">Median abs PnL</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.windows.map((row) => {
                  const rowTone = slopeTone(row.normalized_slope, data.threshold);
                  return (
                    <tr key={row.window} className="text-sm">
                      <td className="px-6 py-3 font-mono text-fg">{row.window}</td>
                      <td className="px-4 py-3 font-mono text-fg-2 tabular-nums">
                        {row.sample_count}
                      </td>
                      <td className="px-4 py-3">
                        <Badge variant="outline" className={cn("font-mono", TONE_CLASS[rowTone])}>
                          {formatSlope(row.normalized_slope)}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 font-mono text-fg-2 tabular-nums">
                        {formatSlope(row.raw_slope, 4)}
                      </td>
                      <td className="px-4 py-3 font-mono text-fg-2 tabular-nums">
                        {fmt.money(row.median_abs_pnl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      <aside className="space-y-4">
        <Card className="p-5">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <Info className="h-4 w-4 text-fg-3" />
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Come leggere
              </div>
              <div className="mt-1 text-xs text-fg-2">
                La slope guida quanto la calibration deve difendersi.
              </div>
            </div>
          </div>
          <div className="space-y-3 pt-3">
            <GuideLine
              label="Normalized"
              value="Slope divisa per il median abs PnL: rende confrontabili periodi con volatilita diversa."
            />
            <GuideLine
              label="Recent"
              value="Trend della finestra piu recente. Serve a uscire dal deep defensive prima dello storico completo."
            />
            <GuideLine
              label="Deep"
              value="Modalita difensiva attivata quando la slope scende sotto soglia."
            />
            <GuideLine
              label="Starvation"
              value="Se troppi cicli non generano segnali, il sistema allenta il deep defensive."
            />
            <GuideLine
              label="Cooldown"
              value="Periodo di stabilizzazione dopo l'uscita dal deep defensive."
            />
          </div>
        </Card>

        <Card className="p-5">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <AlertTriangle className="h-4 w-4 text-warn" />
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Time-stop impact
              </div>
              <div className="mt-1 text-xs text-fg-2">
                Impatto dei time-stop nelle ultime {data.recent_time_stop_impact.hours}h.
              </div>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3 pt-4">
            <MetricTile
              label="Closed"
              value={String(data.recent_time_stop_impact.closed_count)}
              tone="muted"
            />
            <MetricTile
              label="Realized PnL"
              value={fmt.pnl(data.recent_time_stop_impact.realized_pnl)}
              tone={data.recent_time_stop_impact.realized_pnl >= 0 ? "success" : "danger"}
            />
          </div>
        </Card>

        <Card className="p-5">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <ShieldCheck className="h-4 w-4 text-fg-3" />
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Exit conditions
              </div>
              <div className="mt-1 text-xs text-fg-2">
                Condizioni usate per uscire o attenuare le protezioni.
              </div>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 pt-4">
            <GuideStat
              label="Full slope non-negative"
              value={String(data.exit_conditions.full_slope_non_negative_cycles)}
            />
            <GuideStat
              label="Recent slope non-negative"
              value={String(data.exit_conditions.recent_slope_non_negative_cycles)}
            />
            <GuideStat
              label="Starvation hard cycles"
              value={String(data.exit_conditions.starvation_hard_cycles)}
            />
          </div>
        </Card>
      </aside>
    </div>
  );
}

function MetricTile({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone: Tone;
  hint?: string;
}) {
  return (
    <div className="rounded-md border border-border bg-bg-2 px-4 py-3">
      <div className="text-[10px] uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-1 font-mono text-xl font-semibold tabular-nums",
          tone === "success" && "text-success",
          tone === "warn" && "text-warn",
          tone === "danger" && "text-danger",
          tone === "muted" && "text-fg",
          tone === "info" && "text-info",
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-1 text-[11px] text-fg-3">{hint}</div>}
    </div>
  );
}

function GuardTile({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className="rounded-md border border-border bg-bg-2 px-4 py-3">
      <div className="text-[10px] uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-1 font-mono text-sm font-medium tabular-nums",
          tone === "success" && "text-success",
          tone === "warn" && "text-warn",
          tone === "danger" && "text-danger",
          tone === "muted" && "text-fg",
          tone === "info" && "text-info",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function GuideLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3 text-xs">
      <div className="font-mono text-[11px] text-fg-2">{label}</div>
      <div className="leading-relaxed text-fg-3">{value}</div>
    </div>
  );
}

function GuideStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-xs text-fg-3">{label}</span>
      <span className="font-mono text-sm text-fg tabular-nums">{value}</span>
    </div>
  );
}

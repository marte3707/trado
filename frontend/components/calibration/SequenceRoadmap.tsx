"use client";

import { Check, ChevronRight, MinusCircle, RotateCw, X } from "lucide-react";

import { Card } from "@/components/ui/card";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import type { AutoTunerStatus, TunerStepHistory } from "@/lib/api";

interface Props {
  status: AutoTunerStatus | null;
}

type StatusTone = "success" | "danger" | "warn" | "info" | "muted";

const STEP_META: Record<TunerStepHistory["status"], { tone: StatusTone; label: string; icon: React.FC<{ className?: string }> }> = {
  success: { tone: "success", label: "Success", icon: Check },
  promoted: { tone: "success", label: "Promoted", icon: Check },
  reverted: { tone: "danger", label: "Reverted", icon: RotateCw },
  rejected: { tone: "danger", label: "Rejected", icon: X },
  skipped: { tone: "warn", label: "Skipped", icon: MinusCircle },
};

const TONE_RING: Record<StatusTone, string> = {
  success: "border-success/40 bg-success/10 text-success",
  danger: "border-danger/40 bg-danger/10 text-danger",
  warn: "border-warn/40 bg-warn/10 text-warn",
  info: "border-info/40 bg-info/10 text-info",
  muted: "border-border bg-bg-2 text-fg-3",
};

function formatStepKey(key: string | null): string {
  if (!key) return "—";
  return key.replace(/_/g, " ");
}

function pct(value: number | undefined | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function num(value: unknown, digits = 2): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function abMetric(step: TunerStepHistory, key: string): number | null {
  const payload = step.ab_test;
  if (!payload || typeof payload !== "object") return null;
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function SequenceRoadmap({ status }: Props) {
  if (!status) return null;

  const history = status.step_history ?? [];

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Sequence roadmap
        </div>
        <div className="font-mono text-[10px] text-fg-3">
          {history.length} step storici
        </div>
      </div>

      <ScrollArea className="mt-4">
        <div className="flex items-stretch gap-2 overflow-x-auto pb-2">
          {history.map((step, idx) => {
            const meta = STEP_META[step.status] ?? {
              tone: "muted" as StatusTone,
              label: step.status,
              icon: ChevronRight,
            };
            const Icon = meta.icon;
            const wrDelta =
              step.metrics_before && step.metrics_after
                ? step.metrics_after.win_rate - step.metrics_before.win_rate
                : null;
            const abMeanDelta = abMetric(step, "mean_delta");
            return (
              <Tooltip key={`${step.key}-${idx}`}>
                <TooltipTrigger asChild>
                  <div
                    className={cn(
                      "group flex w-44 shrink-0 flex-col gap-1.5 rounded-md border p-3 transition-colors",
                      TONE_RING[meta.tone],
                    )}
                  >
                    <div className="flex items-center justify-between">
                      <Icon className="h-3.5 w-3.5" />
                      <span className="font-mono text-[10px] uppercase tracking-wider">
                        {meta.label}
                      </span>
                    </div>
                    <div className="truncate font-mono text-sm font-medium text-fg">
                      {formatStepKey(step.key)}
                    </div>
                    {wrDelta != null && (
                      <div className="text-[11px] text-fg-3">
                        WR Δ{" "}
                        <span
                          className={cn(
                            "font-mono",
                            wrDelta > 0 ? "text-success" : wrDelta < 0 ? "text-danger" : "text-fg-3",
                          )}
                        >
                          {wrDelta > 0 ? "+" : ""}
                          {pct(wrDelta)}
                        </span>
                      </div>
                    )}
                    {abMeanDelta != null && (
                      <div className="text-[11px] text-fg-3">
                        Mean Δ{" "}
                        <span
                          className={cn(
                            "font-mono",
                            abMeanDelta > 0
                              ? "text-success"
                              : abMeanDelta < 0
                                ? "text-danger"
                                : "text-fg-3",
                          )}
                        >
                          {abMeanDelta > 0 ? "+" : ""}
                          {num(abMeanDelta)}
                        </span>
                      </div>
                    )}
                  </div>
                </TooltipTrigger>
                <TooltipContent side="bottom" className="max-w-xs">
                  <div className="space-y-1">
                    <div className="font-medium">{step.label || formatStepKey(step.key)}</div>
                    <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
                      <span className="text-fg-3">Trade</span>
                      <span className="font-mono text-right">{step.trades_evaluated ?? "—"}</span>
                      <span className="text-fg-3">WR before</span>
                      <span className="font-mono text-right">{pct(step.metrics_before?.win_rate)}</span>
                      <span className="text-fg-3">WR after</span>
                      <span className="font-mono text-right">{pct(step.metrics_after?.win_rate)}</span>
                      <span className="text-fg-3">PnL after</span>
                      <span className="font-mono text-right">
                        {step.metrics_after?.realized_pnl != null
                          ? step.metrics_after.realized_pnl.toFixed(2)
                          : "—"}
                      </span>
                      {step.mode === "ab_test" && (
                        <>
                          <span className="text-fg-3">Control/Test</span>
                          <span className="font-mono text-right">
                            {num(abMetric(step, "n_control"), 0)}/{num(abMetric(step, "n_test"), 0)}
                          </span>
                          <span className="text-fg-3">Mean C/T</span>
                          <span className="font-mono text-right">
                            {num(abMetric(step, "mean_control"))}/{num(abMetric(step, "mean_test"))}
                          </span>
                          <span className="text-fg-3">Mean delta</span>
                          <span className="font-mono text-right">
                            {num(abMetric(step, "mean_delta"))}
                          </span>
                          <span className="text-fg-3">p-value</span>
                          <span className="font-mono text-right">
                            {num(abMetric(step, "p_one_tailed"), 3)}
                          </span>
                        </>
                      )}
                    </div>
                    {step.manual && (
                      <div className="text-[10px] uppercase tracking-wider text-fg-3">
                        Manuale
                      </div>
                    )}
                  </div>
                </TooltipContent>
              </Tooltip>
            );
          })}

          {/* Current step (corrente, non in history) */}
          {status.current_step_key && (
            <div className="flex w-44 shrink-0 flex-col gap-1.5 rounded-md border border-info/40 bg-info/10 p-3 text-info">
              <div className="flex items-center justify-between">
                <ChevronRight className="h-3.5 w-3.5" />
                <span className="font-mono text-[10px] uppercase tracking-wider">In corso</span>
              </div>
              <div className="truncate font-mono text-sm font-medium text-fg">
                {formatStepKey(status.current_step_key)}
              </div>
              <div className="text-[11px] text-fg-3">
                {status.trades_now} trade accumulati
              </div>
            </div>
          )}

          {/* Next step (futuro, fantasma) */}
          {status.next_step_key && (
            <div className="flex w-44 shrink-0 flex-col gap-1.5 rounded-md border border-dashed border-border p-3 opacity-60">
              <div className="flex items-center justify-between text-fg-3">
                <ChevronRight className="h-3.5 w-3.5" />
                <span className="font-mono text-[10px] uppercase tracking-wider">Next</span>
              </div>
              <div className="truncate font-mono text-sm text-fg-2">
                {formatStepKey(status.next_step_key)}
              </div>
            </div>
          )}

          {history.length === 0 && !status.current_step_key && (
            <div className="grid h-20 w-full place-items-center text-xs text-fg-3">
              Nessuno step ancora valutato.
            </div>
          )}
        </div>
      </ScrollArea>
    </Card>
  );
}

"use client";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { Signal, SignalRejections } from "@/lib/api";

interface SignalFunnelProps {
  signals: Signal[];
  rejections: SignalRejections;
  className?: string;
}

interface FunnelStage {
  key: string;
  label: string;
  count: number;
  tone: "muted" | "info" | "warn" | "success" | "danger";
}

export function SignalFunnel({ signals, rejections, className }: SignalFunnelProps) {
  const generated = signals.length + (rejections.total ?? 0);
  const evaluated = signals.length;
  const eligible = signals.filter((s) => s.status !== "ignored").length;
  const executed = signals.filter((s) => s.status === "executed" || s.status === "closed").length;

  const stages: FunnelStage[] = [
    { key: "generated", label: "Generati", count: generated, tone: "muted" },
    { key: "evaluated", label: "Valutati", count: evaluated, tone: "info" },
    { key: "eligible", label: "Eleggibili", count: eligible, tone: "warn" },
    { key: "executed", label: "Eseguiti", count: executed, tone: "success" },
  ];

  const maxCount = Math.max(...stages.map((s) => s.count), 1);

  const topReasons = Object.entries(rejections.by_reason ?? {})
    .map(([k, v]) => ({ reason: k, count: v as number }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 4);

  return (
    <Card className={cn("flex flex-col p-5", className)}>
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
        Signal Funnel
      </div>
      <div className="mt-4 space-y-2.5">
        {stages.map((s) => {
          const pct = (s.count / maxCount) * 100;
          return (
            <div key={s.key}>
              <div className="flex items-center justify-between text-xs">
                <span className="text-fg-2">{s.label}</span>
                <span className="font-mono tabular-nums text-fg">{s.count}</span>
              </div>
              <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-bg-3">
                <div
                  className={cn(
                    "h-full transition-[width] duration-300",
                    s.tone === "success" && "bg-success",
                    s.tone === "warn" && "bg-warn",
                    s.tone === "info" && "bg-info",
                    s.tone === "muted" && "bg-fg-4",
                    s.tone === "danger" && "bg-danger",
                  )}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>

      {topReasons.length > 0 && (
        <div className="mt-5 border-t border-border pt-4">
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Top rejection
          </div>
          <ul className="mt-2 space-y-1">
            {topReasons.map((r) => (
              <li
                key={r.reason}
                className="flex items-center justify-between gap-2 text-xs"
              >
                <span className="truncate text-fg-2" title={r.reason}>
                  {r.reason}
                </span>
                <span className="shrink-0 font-mono tabular-nums text-fg-3">{r.count}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

"use client";

import { ArrowDownRight, ArrowUpRight, type LucideIcon } from "lucide-react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export interface KpiCardProps {
  label: string;
  value: string;
  delta?: { value: string; tone: "up" | "down" | "muted" };
  spark?: number[];
  icon?: LucideIcon;
  hint?: string;
  className?: string;
}

export function KpiCard({ label, value, delta, spark, icon: Icon, hint, className }: KpiCardProps) {
  return (
    <Card className={cn("relative overflow-hidden p-5", className)}>
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
          {Icon && <Icon className="h-3 w-3" />}
          {label}
        </div>
        {delta && (
          <span
            className={cn(
              "inline-flex items-center gap-0.5 rounded-md px-1.5 py-0.5 font-mono text-[10px]",
              delta.tone === "up" && "bg-success/10 text-success",
              delta.tone === "down" && "bg-danger/10 text-danger",
              delta.tone === "muted" && "bg-bg-2 text-fg-3",
            )}
          >
            {delta.tone === "up" && <ArrowUpRight className="h-2.5 w-2.5" />}
            {delta.tone === "down" && <ArrowDownRight className="h-2.5 w-2.5" />}
            {delta.value}
          </span>
        )}
      </div>
      <div className="mt-2 font-mono text-2xl font-semibold tabular-nums tracking-tight text-fg">
        {value}
      </div>
      {hint && <div className="mt-1 text-[11px] text-fg-3">{hint}</div>}

      {spark && spark.length > 1 && <KpiSpark data={spark} tone={delta?.tone ?? "muted"} />}
    </Card>
  );
}

function KpiSpark({ data, tone }: { data: number[]; tone: "up" | "down" | "muted" }) {
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const w = 100;
  const h = 28;
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((v - min) / range) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const color =
    tone === "up" ? "var(--success)" : tone === "down" ? "var(--danger)" : "var(--fg-3)";
  return (
    <svg
      className="absolute bottom-2 right-3"
      width={w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      aria-hidden
    >
      <polyline
        points={pts}
        fill="none"
        stroke={color}
        strokeOpacity={0.55}
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

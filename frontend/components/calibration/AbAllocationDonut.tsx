"use client";

import * as Recharts from "recharts";

import { Card } from "@/components/ui/card";
import {
  Tooltip as UITooltip,
  TooltipContent as UITooltipContent,
  TooltipTrigger as UITooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { AutoTunerAbProgress } from "@/lib/api";

const PieChart = Recharts.PieChart as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Pie = Recharts.Pie as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Cell = Recharts.Cell as unknown as React.ComponentType<Record<string, unknown>>;
const ResponsiveContainer = Recharts.ResponsiveContainer as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Tooltip = Recharts.Tooltip as unknown as React.ComponentType<Record<string, unknown>>;

interface Props {
  abProgress: AutoTunerAbProgress | null | undefined;
}

const COLOR = {
  control: "#a4a4ad",
  test: "#3ecf8e",
  tooltipBg: "#16161a",
  border: "#1d1d22",
  fg: "#e9e9ec",
  fgMuted: "#a4a4ad",
};

function meanLabel(value: number | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 1000) return value.toFixed(0);
  return value.toFixed(3);
}

export function AbAllocationDonut({ abProgress }: Props) {
  if (!abProgress) {
    return (
      <Card className="p-5">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          A/B allocation
        </div>
        <div className="mt-6 grid h-44 place-items-center text-xs text-fg-3">
          Nessun A/B test attivo.
        </div>
      </Card>
    );
  }

  const tradesControl = abProgress.trades_control ?? 0;
  const tradesTest = abProgress.trades_test ?? 0;
  const total = tradesControl + tradesTest;

  const data = [
    { name: "Control", value: tradesControl, color: COLOR.control },
    { name: "Test", value: tradesTest, color: COLOR.test },
  ];

  const meanDelta = abProgress.mean_delta;
  const liftPct =
    abProgress.mean_control != null && abProgress.mean_control !== 0 && meanDelta != null
      ? (meanDelta / Math.abs(abProgress.mean_control)) * 100
      : null;

  const alpha = abProgress.alpha;
  const significantLevel = 0.05;
  const significant = alpha != null && alpha < significantLevel;

  return (
    <Card className="flex flex-col p-5">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
        A/B allocation
      </div>

      <div className="mt-4 flex items-center gap-5">
        <div className="relative h-40 w-40 shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                innerRadius={48}
                outerRadius={68}
                paddingAngle={2}
                isAnimationActive={false}
              >
                {data.map((d) => (
                  <Cell key={d.name} fill={d.color} stroke="transparent" />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{
                  background: COLOR.tooltipBg,
                  border: `1px solid ${COLOR.border}`,
                  borderRadius: 8,
                  fontSize: 12,
                  color: COLOR.fg,
                  padding: "8px 12px",
                }}
                formatter={(value: number, name: string) => [`${value} trade`, name]}
              />
            </PieChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 grid place-items-center text-center">
            <div>
              <div className="font-mono text-xl font-semibold text-fg">{total}</div>
              <div className="text-[10px] uppercase tracking-wider text-fg-3">trade</div>
            </div>
          </div>
        </div>

        <div className="flex-1 space-y-3">
          <LegendRow
            color={COLOR.control}
            label="Control"
            count={tradesControl}
            mean={abProgress.mean_control}
          />
          <LegendRow
            color={COLOR.test}
            label="Test"
            count={tradesTest}
            mean={abProgress.mean_test}
            highlight
          />
        </div>
      </div>

      <div className="mt-5 grid grid-cols-3 gap-3 border-t border-border pt-4">
        <Metric
          label="Lift"
          value={liftPct != null ? `${liftPct >= 0 ? "+" : ""}${liftPct.toFixed(2)}%` : "—"}
          tone={liftPct == null ? "muted" : liftPct >= 0 ? "success" : "danger"}
        />
        <Metric
          label="Mean delta"
          value={meanLabel(meanDelta)}
          tone={meanDelta == null ? "muted" : meanDelta >= 0 ? "success" : "danger"}
        />
        <Metric
          label="Alpha (p-value)"
          value={alpha != null ? alpha.toFixed(3) : "—"}
          tone={significant ? "success" : "muted"}
          hint={significant ? "Significativo" : alpha != null ? "Non significativo" : undefined}
        />
      </div>
    </Card>
  );
}

function LegendRow({
  color,
  label,
  count,
  mean,
  highlight,
}: {
  color: string;
  label: string;
  count: number;
  mean: number | undefined;
  highlight?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-md border border-border bg-bg p-3",
        highlight && "border-success/30",
      )}
    >
      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: color }} />
      <div className="min-w-0 flex-1">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
        <div className="font-mono text-sm tabular-nums text-fg">{count} trade</div>
      </div>
      <div className="text-right">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">Mean</div>
        <div className="font-mono text-sm tabular-nums text-fg">{meanLabel(mean)}</div>
      </div>
    </div>
  );
}

const METRIC_TOOLTIPS: Record<string, string> = {
  Lift: "Variazione % del PnL medio del braccio test rispetto al controllo. Positivo = il test ha performato meglio.",
  "Mean delta":
    "Differenza assoluta del PnL medio tra braccio test e controllo (test − control).",
  "Alpha (p-value)":
    "Probabilità di osservare questa differenza per puro caso (test di Mann-Whitney). Sotto 0.05 il risultato è considerato statisticamente significativo.",
};

function Metric({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone: "success" | "danger" | "muted";
  hint?: string;
}) {
  const tooltipText = METRIC_TOOLTIPS[label];
  return (
    <div>
      <UITooltip>
        <UITooltipTrigger asChild>
          <div className="cursor-help text-[10px] font-medium uppercase tracking-wider text-fg-3 underline decoration-dotted underline-offset-2">
            {label}
          </div>
        </UITooltipTrigger>
        {tooltipText && (
          <UITooltipContent className="max-w-60">{tooltipText}</UITooltipContent>
        )}
      </UITooltip>
      <div
        className={cn(
          "mt-1 font-mono text-base font-semibold tabular-nums",
          tone === "success" && "text-success",
          tone === "danger" && "text-danger",
          tone === "muted" && "text-fg",
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-0.5 text-[10px] text-fg-3">{hint}</div>}
    </div>
  );
}

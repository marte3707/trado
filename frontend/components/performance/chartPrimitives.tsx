"use client";

import * as Recharts from "recharts";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/** Recharts non espone tipi React puliti: cast condivisi per tutti i card. */
type AnyComp = React.ComponentType<Record<string, unknown> & { children?: React.ReactNode }>;

export const Area = Recharts.Area as unknown as AnyComp;
export const AreaChart = Recharts.AreaChart as unknown as AnyComp;
export const Bar = Recharts.Bar as unknown as AnyComp;
export const BarChart = Recharts.BarChart as unknown as AnyComp;
export const Cell = Recharts.Cell as unknown as AnyComp;
export const CartesianGrid = Recharts.CartesianGrid as unknown as AnyComp;
export const ReferenceLine = Recharts.ReferenceLine as unknown as AnyComp;
export const ResponsiveContainer = Recharts.ResponsiveContainer as unknown as AnyComp;
export const Tooltip = Recharts.Tooltip as unknown as AnyComp;
export const XAxis = Recharts.XAxis as unknown as AnyComp;
export const YAxis = Recharts.YAxis as unknown as AnyComp;

/** Palette dark allineata ai chart esistenti (AccountMonitorScreen / EquityChart). */
export const CHART_COLORS = {
  success: "#00D09C",
  danger: "#f26d6d",
  neutral: "#7aa2f7",
  grid: "#1d1d22",
  axis: "#4b4b53",
  cursor: "#2b2b32",
  tooltipBg: "#16161a",
  fg: "#e9e9ec",
  fgMuted: "#a4a4ad",
} as const;

export const tooltipContentStyle = {
  background: CHART_COLORS.tooltipBg,
  border: `1px solid ${CHART_COLORS.grid}`,
  borderRadius: 8,
  fontSize: 12,
  color: CHART_COLORS.fg,
};

export const tooltipLabelStyle = {
  color: CHART_COLORS.fgMuted,
  fontSize: 10,
  textTransform: "uppercase" as const,
};

/** Wrapper card con header (titolo + extra) e area grafico ad altezza fissa. */
export function ChartCard({
  title,
  subtitle,
  extra,
  empty,
  emptyLabel = "Nessun trade chiuso ancora.",
  height = 240,
  className,
  children,
}: {
  title: string;
  subtitle?: React.ReactNode;
  extra?: React.ReactNode;
  empty?: boolean;
  emptyLabel?: string;
  height?: number;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Card className={cn("flex flex-col overflow-hidden", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-5 py-3">
        <div className="flex items-baseline gap-2">
          <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            {title}
          </span>
          {subtitle}
        </div>
        {extra}
      </div>
      {empty ? (
        <div className="grid place-items-center text-xs text-fg-3" style={{ height }}>
          {emptyLabel}
        </div>
      ) : (
        <div className="p-4" style={{ height }}>
          {children}
        </div>
      )}
    </Card>
  );
}

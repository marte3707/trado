"use client";

import { useMemo } from "react";

import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import type { Position } from "@/lib/api";
import { deriveEquitySeries } from "@/lib/performance";
import {
  Area,
  AreaChart,
  CartesianGrid,
  CHART_COLORS,
  ChartCard,
  ResponsiveContainer,
  Tooltip,
  tooltipContentStyle,
  tooltipLabelStyle,
  XAxis,
  YAxis,
} from "./chartPrimitives";

export function EquityCurveCard({
  positions,
  baseEquity,
  className,
}: {
  positions: Position[];
  baseEquity: number;
  className?: string;
}) {
  const data = useMemo(() => deriveEquitySeries(positions, baseEquity), [positions, baseEquity]);

  const last = data.at(-1)?.equity ?? baseEquity;
  const delta = last - baseEquity;
  const pct = baseEquity !== 0 ? (delta / baseEquity) * 100 : null;
  const trendUp = delta >= 0;
  const color = trendUp ? CHART_COLORS.success : CHART_COLORS.danger;

  const yMin = useMemo(() => {
    if (data.length === 0) return 0;
    const min = Math.min(...data.map((d) => d.equity));
    const max = Math.max(...data.map((d) => d.equity));
    const pad = (max - min) * 0.05 || 1;
    return Math.floor(min - pad);
  }, [data]);

  return (
    <ChartCard
      title="Equity curve"
      className={className}
      empty={data.length === 0}
      height={280}
      subtitle={
        <>
          <span className="font-mono text-sm font-semibold text-fg">{fmt.money(last)}</span>
          <span className={cn("font-mono text-xs", trendUp ? "text-success" : "text-danger")}>
            {fmt.pnl(delta)}
            {pct != null && <> · {fmt.pct(pct)}</>}
          </span>
        </>
      }
      extra={
        <span className="font-mono text-[10px] uppercase tracking-wider text-fg-4">
          {data.length} trade · base {fmt.money(baseEquity)}
        </span>
      }
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="perfEquityFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.3} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="label"
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={24}
          />
          <YAxis
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            width={56}
            domain={[yMin, "auto"]}
            tickFormatter={(v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0))}
          />
          <Tooltip
            cursor={{ stroke: CHART_COLORS.cursor, strokeDasharray: "2 4" }}
            contentStyle={tooltipContentStyle}
            labelStyle={tooltipLabelStyle}
            formatter={(v: number) => [fmt.money(v), "Equity"]}
          />
          <Area
            type="monotone"
            dataKey="equity"
            stroke={color}
            strokeWidth={1.8}
            fill="url(#perfEquityFill)"
            dot={false}
            activeDot={{ r: 3, fill: color }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

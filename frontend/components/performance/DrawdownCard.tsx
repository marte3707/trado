"use client";

import { useMemo } from "react";

import { cn } from "@/lib/utils";
import type { Position } from "@/lib/api";
import { deriveDailyDrawdown } from "@/lib/performance";
import {
  Bar,
  BarChart,
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

export function DrawdownCard({
  positions,
  baseEquity,
  className,
}: {
  positions: Position[];
  baseEquity: number;
  className?: string;
}) {
  const data = useMemo(() => deriveDailyDrawdown(positions, baseEquity), [positions, baseEquity]);
  const maxDd = useMemo(() => data.reduce((m, d) => Math.max(m, d.drawdownPct), 0), [data]);

  return (
    <ChartCard
      title="Drawdown % per giorno"
      className={className}
      empty={data.length === 0}
      subtitle={
        maxDd > 0 ? (
          <span
            className={cn(
              "rounded-full px-2 py-0.5 font-mono text-[10px] font-medium",
              maxDd > 15 ? "bg-danger/10 text-danger" : maxDd > 8 ? "bg-warn/10 text-warn" : "bg-success/10 text-success",
            )}
          >
            Max DD {maxDd.toFixed(2)}%
          </span>
        ) : undefined
      }
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="day"
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={20}
          />
          <YAxis
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            width={42}
            tickFormatter={(v: number) => `${v.toFixed(0)}%`}
          />
          <Tooltip
            cursor={{ fill: "rgba(242,109,109,0.08)" }}
            contentStyle={tooltipContentStyle}
            labelStyle={tooltipLabelStyle}
            formatter={(v: number) => [`${v.toFixed(2)}%`, "Drawdown"]}
          />
          <Bar dataKey="drawdownPct" fill={CHART_COLORS.danger} radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

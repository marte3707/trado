"use client";

import { useMemo } from "react";

import type { Position } from "@/lib/api";
import { deriveTradesPerDay } from "@/lib/performance";
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

export function TradesPerDayCard({
  positions,
  className,
}: {
  positions: Position[];
  className?: string;
}) {
  const data = useMemo(() => deriveTradesPerDay(positions), [positions]);
  const total = useMemo(() => data.reduce((s, d) => s + d.count, 0), [data]);

  return (
    <ChartCard
      title="Trade per giorno"
      subtitle={
        <span className="font-mono text-[10px] uppercase tracking-wider text-fg-4">
          {total} trade totali
        </span>
      }
      className={className}
      empty={data.length === 0}
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
            width={28}
            allowDecimals={false}
          />
          <Tooltip
            cursor={{ fill: "rgba(122,162,247,0.06)" }}
            contentStyle={tooltipContentStyle}
            labelStyle={tooltipLabelStyle}
            formatter={(v: number) => [`${v} trade`, "Eseguiti"]}
          />
          <Bar dataKey="count" fill={CHART_COLORS.neutral} radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

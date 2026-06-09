"use client";

import { useMemo } from "react";

import { fmt } from "@/components/atoms";
import type { Position } from "@/lib/api";
import { deriveMonthlyReturns } from "@/lib/performance";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  CHART_COLORS,
  ChartCard,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  tooltipContentStyle,
  tooltipLabelStyle,
  XAxis,
  YAxis,
} from "./chartPrimitives";

export function MonthlyReturnCard({
  positions,
  baseEquity,
  className,
}: {
  positions: Position[];
  baseEquity: number;
  className?: string;
}) {
  const data = useMemo(() => deriveMonthlyReturns(positions, baseEquity), [positions, baseEquity]);

  return (
    <ChartCard
      title="Rendimento mensile %"
      subtitle={
        <span className="font-mono text-[10px] uppercase tracking-wider text-fg-4">
          {data.length} mesi
        </span>
      }
      className={className}
      empty={data.length === 0}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="month"
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={12}
          />
          <YAxis
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            width={40}
            tickFormatter={(v: number) => `${v.toFixed(0)}%`}
          />
          <ReferenceLine y={0} stroke={CHART_COLORS.axis} />
          <Tooltip
            cursor={{ fill: "rgba(122,162,247,0.06)" }}
            contentStyle={tooltipContentStyle}
            labelStyle={tooltipLabelStyle}
            formatter={(v: number, _name: string, item: { payload?: { pnl?: number } }) => [
              `${fmt.pct(v)} · ${fmt.pnl(item?.payload?.pnl ?? 0)}`,
              "Rendimento",
            ]}
          />
          <Bar dataKey="returnPct" radius={[2, 2, 0, 0]} isAnimationActive={false}>
            {data.map((m, i) => (
              <Cell key={i} fill={m.returnPct < 0 ? CHART_COLORS.danger : CHART_COLORS.success} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

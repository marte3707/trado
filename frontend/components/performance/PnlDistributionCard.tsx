"use client";

import { useMemo } from "react";

import { fmt } from "@/components/atoms";
import type { Position } from "@/lib/api";
import { derivePnlDistribution } from "@/lib/performance";
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

export function PnlDistributionCard({
  positions,
  className,
}: {
  positions: Position[];
  className?: string;
}) {
  const data = useMemo(() => derivePnlDistribution(positions), [positions]);

  return (
    <ChartCard
      title="Distribuzione P&L"
      subtitle={
        <span className="font-mono text-[10px] uppercase tracking-wider text-fg-4">
          per trade · USD
        </span>
      }
      className={className}
      empty={data.length === 0}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="bucketLabel"
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={16}
          />
          <YAxis
            stroke={CHART_COLORS.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
            width={32}
            allowDecimals={false}
          />
          <ReferenceLine x="0" stroke={CHART_COLORS.axis} strokeDasharray="3 3" />
          <Tooltip
            cursor={{ fill: "rgba(122,162,247,0.06)" }}
            contentStyle={tooltipContentStyle}
            labelStyle={tooltipLabelStyle}
            formatter={(v: number, _name: string, item: { payload?: { avgPnl?: number } }) => [
              `${v} trade · medio ${fmt.pnl(item?.payload?.avgPnl ?? 0)}`,
              "Conteggio",
            ]}
            labelFormatter={(label: string) => `bucket ≥ ${fmt.money(Number(label))}`}
          />
          <Bar dataKey="count" radius={[2, 2, 0, 0]} isAnimationActive={false}>
            {data.map((b, i) => (
              <Cell key={i} fill={b.sign < 0 ? CHART_COLORS.danger : CHART_COLORS.success} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

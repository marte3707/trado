"use client";

import { useMemo } from "react";
import * as Recharts from "recharts";

const Area = Recharts.Area as unknown as React.ComponentType<Record<string, unknown>>;
const AreaChart = Recharts.AreaChart as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const CartesianGrid = Recharts.CartesianGrid as unknown as React.ComponentType<Record<string, unknown>>;
const ResponsiveContainer = Recharts.ResponsiveContainer as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Tooltip = Recharts.Tooltip as unknown as React.ComponentType<Record<string, unknown>>;
const XAxis = Recharts.XAxis as unknown as React.ComponentType<Record<string, unknown>>;
const YAxis = Recharts.YAxis as unknown as React.ComponentType<Record<string, unknown>>;

import { Card } from "@/components/ui/card";
import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import type { Position } from "@/lib/api";
import { deriveEquitySeries } from "@/lib/performance";

interface EquityChartProps {
  positions: Position[];
  className?: string;
  initialEquity?: number;
  label?: string;
}

export function EquityChart({
  positions,
  className,
  initialEquity = 10_000,
  label,
}: EquityChartProps) {
  const data = useMemo(() => deriveEquitySeries(positions, initialEquity), [positions, initialEquity]);

  const last = data.at(-1)?.equity ?? initialEquity;
  const totalDelta = last - initialEquity;
  const totalPct = initialEquity !== 0 ? (totalDelta / initialEquity) * 100 : null;
  const trendUp = totalDelta >= 0;
  const isPnlOnly = initialEquity === 0;

  const CHART_COLOR = {
    line: trendUp ? "#3ecf8e" : "#f26d6d",
    grid: "#1d1d22",
    axis: "#4b4b53",
    cursor: "#2b2b32",
    tooltipBg: "#16161a",
    fg: "#e9e9ec",
    fgMuted: "#a4a4ad",
  };

  return (
    <Card className={cn("flex flex-col p-5", className)}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              {isPnlOnly ? "Cumulative realized PnL" : "Equity (cumulative PnL)"}
            </span>
            {label && (
              <span className="inline-flex h-5 items-center rounded-md border border-border bg-bg-2 px-1.5 font-mono text-[10px] uppercase tracking-wider text-fg-2">
                {label}
              </span>
            )}
          </div>
          <div className="mt-1.5 flex items-baseline gap-2">
            <span
              className={cn(
                "font-mono text-2xl font-semibold tabular-nums",
                isPnlOnly
                  ? trendUp
                    ? "text-success"
                    : "text-danger"
                  : "text-fg",
              )}
            >
              {isPnlOnly ? fmt.pnl(last) : fmt.money(last)}
            </span>
            {!isPnlOnly && (
              <span
                className={cn(
                  "font-mono text-xs",
                  trendUp ? "text-success" : "text-danger",
                )}
              >
                {fmt.pnl(totalDelta)}
                {totalPct != null && <> · {fmt.pct(totalPct)}</>}
              </span>
            )}
          </div>
        </div>
        <div className="text-right text-[10px] uppercase tracking-wider text-fg-3">
          <div>{data.length} trade chiusi</div>
          <div className="text-fg-4">
            {isPnlOnly ? "base PnL realizzato" : `base ${fmt.money(initialEquity)}`}
          </div>
        </div>
      </div>

      <div className="mt-4 h-44 flex-1">
        {data.length === 0 ? (
          <div className="grid h-full place-items-center text-xs text-fg-3">
            Nessun trade chiuso ancora.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 5, right: 5, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={CHART_COLOR.line} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={CHART_COLOR.line} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                stroke={CHART_COLOR.grid}
                strokeDasharray="2 4"
                vertical={false}
              />
              <XAxis
                dataKey="label"
                stroke={CHART_COLOR.axis}
                fontSize={10}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                stroke={CHART_COLOR.axis}
                fontSize={10}
                tickLine={false}
                axisLine={false}
                width={50}
                tickFormatter={(v: number) =>
                  v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)
                }
              />
              <Tooltip
                cursor={{ stroke: CHART_COLOR.cursor, strokeDasharray: "2 4" }}
                contentStyle={{
                  background: CHART_COLOR.tooltipBg,
                  border: `1px solid ${CHART_COLOR.grid}`,
                  borderRadius: 8,
                  fontSize: 12,
                  color: CHART_COLOR.fg,
                }}
                labelStyle={{ color: CHART_COLOR.fgMuted, fontSize: 10, textTransform: "uppercase" }}
                formatter={(v: number) => [
                  isPnlOnly ? fmt.pnl(v) : fmt.money(v),
                  isPnlOnly ? "Realized PnL" : "Equity",
                ]}
              />
              <Area
                type="monotone"
                dataKey="equity"
                stroke={CHART_COLOR.line}
                strokeWidth={1.8}
                fill="url(#equityFill)"
                dot={false}
                activeDot={{ r: 3, fill: CHART_COLOR.line }}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </Card>
  );
}

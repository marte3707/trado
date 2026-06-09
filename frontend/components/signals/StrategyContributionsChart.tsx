"use client";

import { useMemo } from "react";
import * as Recharts from "recharts";

const Bar = Recharts.Bar as unknown as React.ComponentType<Record<string, unknown>>;
const BarChart = Recharts.BarChart as unknown as React.ComponentType<Record<string, unknown>>;
const Cell = Recharts.Cell as unknown as React.ComponentType<Record<string, unknown>>;
const ResponsiveContainer = Recharts.ResponsiveContainer as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Tooltip = Recharts.Tooltip as unknown as React.ComponentType<Record<string, unknown>>;
const XAxis = Recharts.XAxis as unknown as React.ComponentType<Record<string, unknown>>;
const YAxis = Recharts.YAxis as unknown as React.ComponentType<Record<string, unknown>>;

import { cn } from "@/lib/utils";
import { normalizeDir } from "@/components/atoms";

interface StrategyBreakdownEntry {
  name: string;
  direction: string;
  confidence: number;
  weight: number;
  contribution: number;
  reason?: string;
}

function asNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function parseBreakdown(breakdown: Record<string, unknown>): StrategyBreakdownEntry[] {
  return Object.entries(breakdown ?? {}).map(([name, raw]) => {
    const obj = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    const direction = typeof obj.direction === "string" ? obj.direction : "NEUTRAL";
    const confidence = asNumber(obj.confidence) ?? 0;
    const weight = asNumber(obj.weight) ?? 0;
    const explicitContribution = asNumber(obj.contribution);
    const contribution = explicitContribution ?? Math.abs(confidence) * weight;
    const reason = typeof obj.reason === "string" ? obj.reason : undefined;
    return { name, direction, confidence, weight, contribution, reason };
  });
}

interface Props {
  breakdown: Record<string, unknown>;
  signalDirection: string;
  className?: string;
}

export function StrategyContributionsChart({ breakdown, signalDirection, className }: Props) {
  const entries = useMemo(() => parseBreakdown(breakdown), [breakdown]);
  const signalDir = normalizeDir(signalDirection);

  const sorted = useMemo(
    () =>
      [...entries].sort((a, b) => b.contribution - a.contribution).slice(0, 10),
    [entries],
  );

  if (sorted.length === 0) {
    return (
      <div className={cn("grid h-32 place-items-center text-xs text-fg-3", className)}>
        Nessun breakdown strategia disponibile.
      </div>
    );
  }

  const height = Math.max(140, sorted.length * 28);
  const COLOR = {
    agree: "#00D09C",
    oppose: "#f26d6d",
    neutral: "#72727b",
    axis: "#4b4b53",
    axisLabel: "#a4a4ad",
    tooltipBg: "#16161a",
    border: "#1d1d22",
    fg: "#e9e9ec",
    fgMuted: "#a4a4ad",
    cursor: "#1c1c22",
  };

  return (
    <div className={cn("w-full", className)} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart layout="vertical" data={sorted} margin={{ top: 4, right: 12, left: 6, bottom: 4 }}>
          <XAxis
            type="number"
            stroke={COLOR.axis}
            fontSize={10}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            type="category"
            dataKey="name"
            stroke={COLOR.axisLabel}
            fontSize={11}
            tickLine={false}
            axisLine={false}
            width={120}
          />
          <Tooltip
            cursor={{ fill: COLOR.cursor }}
            contentStyle={{
              background: COLOR.tooltipBg,
              border: `1px solid ${COLOR.border}`,
              borderRadius: 8,
              fontSize: 12,
              color: COLOR.fg,
              padding: "8px 12px",
            }}
            labelStyle={{ color: COLOR.fgMuted, fontSize: 10, textTransform: "uppercase", marginBottom: 4 }}
            formatter={(value: number, _name: string, item: { payload: StrategyBreakdownEntry }) => {
              const e = item.payload;
              return [
                `${value.toFixed(2)} · ${e.direction} · conf ${(e.confidence * 100).toFixed(0)}% · peso ${e.weight}`,
                e.name,
              ];
            }}
          />
          <Bar dataKey="contribution" radius={[0, 4, 4, 0]} isAnimationActive={false}>
            {sorted.map((entry) => {
              const dir = normalizeDir(entry.direction);
              const agrees = dir !== "NEUTRAL" && dir === signalDir;
              const opposes = dir !== "NEUTRAL" && dir !== signalDir && signalDir !== "NEUTRAL";
              const color = agrees ? COLOR.agree : opposes ? COLOR.oppose : COLOR.neutral;
              return <Cell key={entry.name} fill={color} fillOpacity={0.85} />;
            })}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

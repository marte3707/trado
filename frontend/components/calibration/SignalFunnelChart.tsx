"use client";

import { useMemo } from "react";
import * as Recharts from "recharts";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { Signal, SignalRejections } from "@/lib/api";

const BarChart = Recharts.BarChart as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Bar = Recharts.Bar as unknown as React.ComponentType<Record<string, unknown>>;
const Cell = Recharts.Cell as unknown as React.ComponentType<Record<string, unknown>>;
const ResponsiveContainer = Recharts.ResponsiveContainer as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Tooltip = Recharts.Tooltip as unknown as React.ComponentType<Record<string, unknown>>;
const XAxis = Recharts.XAxis as unknown as React.ComponentType<Record<string, unknown>>;
const YAxis = Recharts.YAxis as unknown as React.ComponentType<Record<string, unknown>>;

interface Props {
  signals: Signal[];
  rejections: SignalRejections;
  className?: string;
}

interface Stage {
  stage: string;
  count: number;
  color: string;
}

const COLOR = {
  generated: "#4b4b53",
  evaluated: "#7aa2f7",
  eligible: "#e0b341",
  executed: "#3ecf8e",
  closed: "#a4a4ad",
  axis: "#4b4b53",
  tooltipBg: "#16161a",
  border: "#1d1d22",
  fg: "#e9e9ec",
};

export function SignalFunnelChart({ signals, rejections, className }: Props) {
  const stages: Stage[] = useMemo(() => {
    const generated = signals.length + (rejections.total ?? 0);
    const evaluated = signals.length;
    const eligible = signals.filter((s) => s.status !== "ignored" && s.status !== "invalidated").length;
    const executed = signals.filter(
      (s) => s.status === "executed" || s.status === "closed",
    ).length;
    const closed = signals.filter((s) => s.status === "closed").length;
    return [
      { stage: "Generati", count: generated, color: COLOR.generated },
      { stage: "Valutati", count: evaluated, color: COLOR.evaluated },
      { stage: "Eleggibili", count: eligible, color: COLOR.eligible },
      { stage: "Eseguiti", count: executed, color: COLOR.executed },
      { stage: "Chiusi", count: closed, color: COLOR.closed },
    ];
  }, [signals, rejections.total]);

  return (
    <Card className={cn("flex flex-col p-5", className)}>
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Signal funnel
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Conversione attraverso le fasi: generazione → esecuzione → chiusura
          </div>
        </div>
        <div className="text-right">
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Conv. eseguiti
          </div>
          <div className="font-mono text-sm text-fg">
            {stages[0].count > 0
              ? `${((stages[3].count / stages[0].count) * 100).toFixed(1)}%`
              : "—"}
          </div>
        </div>
      </div>

      <div className="mt-4 h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={stages} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
            <XAxis
              dataKey="stage"
              stroke={COLOR.axis}
              fontSize={11}
              tickLine={false}
              axisLine={false}
            />
            <YAxis
              stroke={COLOR.axis}
              fontSize={10}
              tickLine={false}
              axisLine={false}
              width={40}
            />
            <Tooltip
              cursor={{ fill: "#1c1c22", fillOpacity: 0.4 }}
              contentStyle={{
                background: COLOR.tooltipBg,
                border: `1px solid ${COLOR.border}`,
                borderRadius: 8,
                fontSize: 12,
                color: COLOR.fg,
                padding: "8px 12px",
              }}
              formatter={(v: number) => [`${v} segnali`, "Conteggio"]}
            />
            <Bar dataKey="count" radius={[6, 6, 0, 0]} isAnimationActive={false}>
              {stages.map((s) => (
                <Cell key={s.stage} fill={s.color} fillOpacity={0.85} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

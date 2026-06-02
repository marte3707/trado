"use client";

import * as Recharts from "recharts";
import { Info, Loader2 } from "lucide-react";

import { Card } from "@/components/ui/card";
import { fmt } from "@/components/atoms";
import { cn } from "@/lib/utils";
import { useScoreCalibration } from "@/lib/hooks/queries";
import type { ScoreCalibrationBucket, ScoreCalibrationPeriod } from "@/lib/api";

const Cell = Recharts.Cell as unknown as React.ComponentType<Record<string, unknown>>;
const BarChart = Recharts.BarChart as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Bar = Recharts.Bar as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const LineChart = Recharts.LineChart as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const Line = Recharts.Line as unknown as React.ComponentType<Record<string, unknown>>;
const XAxis = Recharts.XAxis as unknown as React.ComponentType<Record<string, unknown>>;
const YAxis = Recharts.YAxis as unknown as React.ComponentType<Record<string, unknown>>;
const CartesianGrid = Recharts.CartesianGrid as unknown as React.ComponentType<Record<string, unknown>>;
const Tooltip = Recharts.Tooltip as unknown as React.ComponentType<Record<string, unknown>>;
const ResponsiveContainer = Recharts.ResponsiveContainer as unknown as React.ComponentType<
  Record<string, unknown> & { children?: React.ReactNode }
>;

function correlationTone(c: number | null): string {
  if (c === null) return "text-fg-3";
  if (c >= 0.3) return "text-success";
  if (c >= 0.1) return "text-warn";
  return "text-danger";
}

function bucketColor(winRate: number): string {
  if (winRate >= 0.6) return "#22c55e";
  if (winRate >= 0.45) return "#f59e0b";
  return "#ef4444";
}

function pnlColor(avgPnl: number): string {
  return avgPnl >= 0 ? "#22c55e" : "#ef4444";
}

function formatCorrelation(c: number | null): string {
  if (c === null) return "—";
  return `${c >= 0 ? "+" : ""}${c.toFixed(3)}`;
}

function KpiCard({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="rounded-md border border-border bg-bg-2 px-5 py-4">
      <div className="text-[10px] uppercase tracking-wider text-fg-3">{label}</div>
      <div className={cn("mt-1 font-mono text-2xl font-semibold tabular-nums", tone ?? "text-fg")}>
        {value}
      </div>
      {sub && <div className="mt-1 text-[11px] text-fg-3">{sub}</div>}
    </div>
  );
}

function WinRateTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { value: number }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-border bg-bg-2 px-3 py-2 text-xs shadow-md">
      <div className="font-mono text-fg-3">Score {label}</div>
      <div className="mt-1 font-mono text-fg">
        Win rate: {(payload[0].value * 100).toFixed(1)}%
      </div>
    </div>
  );
}

function PnlTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { value: number }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-border bg-bg-2 px-3 py-2 text-xs shadow-md">
      <div className="font-mono text-fg-3">Score {label}</div>
      <div className="mt-1 font-mono text-fg">Avg PnL: {fmt.pnl(payload[0].value)}</div>
    </div>
  );
}

function DriftTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { name: string; value: number }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-border bg-bg-2 px-3 py-2 text-xs shadow-md">
      <div className="font-mono text-fg-3">{label}</div>
      {payload.map((p) => (
        <div key={p.name} className="mt-1 font-mono text-fg">
          {p.name === "win_rate" ? `Win rate: ${(p.value * 100).toFixed(1)}%` : `Avg score: ${p.value}`}
        </div>
      ))}
    </div>
  );
}

function BucketBarChart({ buckets, field, colorFn, tooltipContent }: {
  buckets: ScoreCalibrationBucket[];
  field: "win_rate" | "avg_pnl";
  colorFn: (v: number) => string;
  tooltipContent: React.ComponentType<{ active?: boolean; payload?: { value: number }[]; label?: string }>;
}) {
  const data = buckets.map((b) => ({
    range: b.range,
    value: b[field],
    color: colorFn(b[field]),
    count: b.count,
  }));

  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" vertical={false} />
        <XAxis
          dataKey="range"
          tick={{ fontSize: 10, fill: "var(--color-fg-3)" }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          tick={{ fontSize: 10, fill: "var(--color-fg-3)" }}
          axisLine={false}
          tickLine={false}
          width={40}
          tickFormatter={(v: number) =>
            field === "win_rate" ? `${(v * 100).toFixed(0)}%` : fmt.pnl(v)
          }
        />
        <Tooltip content={tooltipContent} cursor={{ fill: "var(--color-bg-3)" }} />
        <Bar dataKey="value" radius={[3, 3, 0, 0]}>
          {data.map((d, i) => (
            <Cell key={i} fill={d.color} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function DriftLineChart({ periods }: { periods: ScoreCalibrationPeriod[] }) {
  const data = periods.map((p) => ({
    period: p.period.slice(5),
    win_rate: p.win_rate,
    avg_score: p.avg_score,
    count: p.count,
  }));

  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" vertical={false} />
        <XAxis
          dataKey="period"
          tick={{ fontSize: 10, fill: "var(--color-fg-3)" }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          yAxisId="wr"
          orientation="left"
          tick={{ fontSize: 10, fill: "var(--color-fg-3)" }}
          axisLine={false}
          tickLine={false}
          width={40}
          domain={[0, 1]}
          tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
        />
        <YAxis
          yAxisId="score"
          orientation="right"
          tick={{ fontSize: 10, fill: "var(--color-fg-3)" }}
          axisLine={false}
          tickLine={false}
          width={36}
          domain={[0, 100]}
        />
        <Tooltip content={DriftTooltip} />
        <Line
          yAxisId="wr"
          type="monotone"
          dataKey="win_rate"
          stroke="#22c55e"
          strokeWidth={2}
          dot={{ r: 3, fill: "#22c55e" }}
          name="win_rate"
        />
        <Line
          yAxisId="score"
          type="monotone"
          dataKey="avg_score"
          stroke="#6366f1"
          strokeWidth={2}
          strokeDasharray="4 2"
          dot={{ r: 3, fill: "#6366f1" }}
          name="avg_score"
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function ScoreAlignmentTab() {
  const { data, isLoading } = useScoreCalibration();

  if (isLoading) {
    return (
      <Card className="grid h-64 place-items-center">
        <div className="flex items-center gap-2 text-fg-3">
          <Loader2 className="h-4 w-4 animate-spin" />
          Caricamento score alignment...
        </div>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card className="grid h-36 place-items-center text-sm text-fg-3">
        Score alignment non disponibile.
      </Card>
    );
  }

  if (data.total_trades === 0) {
    return (
      <Card className="grid h-36 place-items-center text-sm text-fg-3">
        Nessun trade chiuso disponibile per l&apos;analisi.
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* KPI */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <KpiCard
          label="Correlazione score↔PnL"
          value={formatCorrelation(data.correlation)}
          sub="Pearson · più alto = più allineato"
          tone={correlationTone(data.correlation)}
        />
        <KpiCard
          label="Trade analizzati"
          value={String(data.total_trades)}
          sub="posizioni paper chiuse"
        />
        <KpiCard
          label="Bucket attivi"
          value={String(data.buckets.length)}
          sub="range di score con dati"
        />
        <KpiCard
          label="Periodi drift"
          value={String(data.by_week.length)}
          sub="finestre bisettimanali"
        />
      </div>

      {/* Win rate per bucket */}
      <Card className="p-0 overflow-hidden">
        <div className="border-b border-border px-6 py-4">
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Win rate per score bucket
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Percentuale di trade in profitto raggruppati per fascia di score (0–9, 10–19, …).
          </div>
        </div>
        <div className="px-4 py-4">
          <BucketBarChart
            buckets={data.buckets}
            field="win_rate"
            colorFn={bucketColor}
            tooltipContent={WinRateTooltip}
          />
        </div>
        <div className="grid grid-cols-2 border-t border-border sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
          {data.buckets.map((b) => (
            <div key={b.range} className="border-r border-border px-4 py-3 last:border-r-0">
              <div className="font-mono text-[10px] text-fg-3">{b.range}</div>
              <div
                className={cn(
                  "mt-1 font-mono text-sm font-semibold tabular-nums",
                  b.win_rate >= 0.6 ? "text-success" : b.win_rate >= 0.45 ? "text-warn" : "text-danger",
                )}
              >
                {(b.win_rate * 100).toFixed(0)}%
              </div>
              <div className="text-[10px] text-fg-3">{b.count} trade</div>
            </div>
          ))}
        </div>
      </Card>

      {/* Avg PnL per bucket */}
      <Card className="p-0 overflow-hidden">
        <div className="border-b border-border px-6 py-4">
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            PnL medio per score bucket
          </div>
          <div className="mt-0.5 text-xs text-fg-3">
            Guadagno medio per trade in dollari, per fascia di score.
          </div>
        </div>
        <div className="px-4 py-4">
          <BucketBarChart
            buckets={data.buckets}
            field="avg_pnl"
            colorFn={pnlColor}
            tooltipContent={PnlTooltip}
          />
        </div>
      </Card>

      {/* Drift bisettimanale */}
      {data.by_week.length >= 2 && (
        <Card className="p-0 overflow-hidden">
          <div className="border-b border-border px-6 py-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Drift bisettimanale
            </div>
            <div className="mt-0.5 text-xs text-fg-3">
              Win rate (verde, asse sx) e score medio (viola tratteggiato, asse dx) per finestre di 14 giorni.
            </div>
          </div>
          <div className="px-4 py-4">
            <DriftLineChart periods={data.by_week} />
          </div>
        </Card>
      )}

      {/* Legenda */}
      <Card className="p-5">
        <div className="flex items-center gap-2 border-b border-border pb-3">
          <Info className="h-4 w-4 text-fg-3" />
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Come leggere questa vista
          </div>
        </div>
        <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
          {[
            ["Correlazione Pearson", "Misura quanto lo score predice il PnL. >0.3 = allineamento buono, <0.1 = bassa predittività."],
            ["Win rate per bucket", "Idealmente cresce al crescere del bucket. Se i bucket alti hanno WR basso, lo score è poco calibrato."],
            ["PnL medio per bucket", "Conferma la direzione del WR. Un bucket ad alto WR ma PnL piatto può indicare RR sbilanciato."],
            ["Drift bisettimanale", "Mostra se l'allineamento cambia nel tempo. Un win rate che scende con score stabile = regime shift."],
          ].map(([label, desc]) => (
            <div key={label} className="grid grid-cols-[7rem_minmax(0,1fr)] gap-3">
              <div className="font-mono text-[11px] text-fg-2">{label}</div>
              <div className="leading-relaxed text-fg-3">{desc}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

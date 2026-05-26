"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useCalibrationStability } from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";
import type { CalibrationStabilityEntry } from "@/lib/api";

function pct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function stddevTone(stddev: number, threshold: number): "success" | "warn" | "danger" | "muted" {
  if (!Number.isFinite(stddev)) return "muted";
  if (stddev <= threshold * 0.5) return "success";
  if (stddev <= threshold) return "warn";
  return "danger";
}

const TONE_BG: Record<string, string> = {
  success: "bg-success/15 text-success border-success/30 hover:bg-success/25",
  warn: "bg-warn/15 text-warn border-warn/30 hover:bg-warn/25",
  danger: "bg-danger/15 text-danger border-danger/30 hover:bg-danger/25",
  muted: "bg-bg-2 text-fg-3 border-border hover:bg-bg-3",
};

export function StabilityMatrix() {
  const { data, isLoading } = useCalibrationStability();

  const { matrix, strategies, timeframes, threshold, candidates } = useMemo(() => {
    if (!data) {
      return {
        matrix: new Map<string, CalibrationStabilityEntry>(),
        strategies: [],
        timeframes: [],
        threshold: 0.25,
        candidates: [],
      };
    }
    const map = new Map<string, CalibrationStabilityEntry>();
    const stratSet = new Set<string>();
    const tfSet = new Set<string>();
    for (const e of data.by_strategy) {
      map.set(`${e.timeframe}|${e.strategy}`, e);
      stratSet.add(e.strategy);
      tfSet.add(e.timeframe);
    }
    const thr = data.settings.factor_history_std_threshold;
    const candidatesArr = data.by_strategy
      .filter((e) => e.factor_stddev > thr)
      .sort((a, b) => b.factor_stddev - a.factor_stddev);
    return {
      matrix: map,
      strategies: Array.from(stratSet).sort(),
      timeframes: Array.from(tfSet).sort(),
      threshold: thr,
      candidates: candidatesArr,
    };
  }, [data]);

  if (isLoading) {
    return (
      <Card className="grid h-64 place-items-center">
        <div className="flex items-center gap-2 text-fg-3">
          <Loader2 className="h-4 w-4 animate-spin" />
          Caricamento stability matrix...
        </div>
      </Card>
    );
  }

  if (!data || strategies.length === 0) {
    return (
      <Card className="grid h-32 place-items-center text-sm text-fg-3">
        Nessun dato di stability disponibile.
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header info */}
      <Card className="flex flex-wrap items-center gap-4 px-6 py-4">
        <div className="flex-1">
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Stability matrix
          </div>
          <div className="mt-1 text-xs text-fg-3">
            Walk-forward stddev dei factor recenti per ogni (timeframe × strategia). Soglia overfitting
            corrente:{" "}
            <span className="font-mono text-fg-2">{threshold.toFixed(2)}</span>
          </div>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-fg-3">
          <LegendDot tone="success" label="Stabile" />
          <LegendDot tone="warn" label="Borderline" />
          <LegendDot tone="danger" label="Overfit" />
        </div>
      </Card>

      {/* Matrix grid */}
      <Card className="p-0 overflow-hidden">
        <ScrollArea>
          <div className="min-w-fit p-4">
            <table className="w-full border-separate border-spacing-1">
              <thead>
                <tr>
                  <th className="sticky left-0 z-10 bg-bg-1 text-left text-[10px] font-medium uppercase tracking-wider text-fg-3">
                    Strategia
                  </th>
                  {timeframes.map((tf) => (
                    <th
                      key={tf}
                      className="font-mono text-[10px] font-medium uppercase tracking-wider text-fg-3"
                    >
                      {tf}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {strategies.map((strategy) => (
                  <tr key={strategy}>
                    <td className="sticky left-0 z-10 bg-bg-1 pr-3 font-mono text-xs text-fg-2">
                      {strategy}
                    </td>
                    {timeframes.map((tf) => {
                      const entry = matrix.get(`${tf}|${strategy}`);
                      if (!entry) {
                        return (
                          <td key={tf} className="p-0">
                            <div className="h-10 w-16 rounded-md border border-dashed border-border" />
                          </td>
                        );
                      }
                      return (
                        <td key={tf} className="p-0">
                          <Cell entry={entry} threshold={threshold} />
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </ScrollArea>
      </Card>

      {/* Overfitting candidates */}
      {candidates.length > 0 && (
        <Card className="p-0 overflow-hidden">
          <div className="flex items-center gap-2 border-b border-border px-6 py-4">
            <AlertTriangle className="h-4 w-4 text-warn" />
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Overfitting candidates
            </div>
            <Badge variant="outline" className="ml-auto font-mono">
              {candidates.length}
            </Badge>
          </div>
          <ul className="divide-y divide-border">
            {candidates.slice(0, 10).map((c) => (
              <li key={c.key} className="flex items-center gap-3 px-6 py-3">
                <span className="font-mono text-sm text-fg">{c.timeframe}</span>
                <span className="font-mono text-sm text-fg-2">{c.strategy}</span>
                <span className="ml-auto flex items-center gap-3">
                  <span className="text-[10px] uppercase tracking-wider text-fg-3">
                    stddev
                  </span>
                  <span className="font-mono text-sm font-medium text-danger tabular-nums">
                    {c.factor_stddev.toFixed(3)}
                  </span>
                  <span className="text-[10px] uppercase tracking-wider text-fg-3">
                    soglia
                  </span>
                  <span className="font-mono text-xs text-fg-3 tabular-nums">
                    {threshold.toFixed(3)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function Cell({ entry, threshold }: { entry: CalibrationStabilityEntry; threshold: number }) {
  const tone = stddevTone(entry.factor_stddev, threshold);
  const wrDelta = entry.holdout?.wr_delta;
  const [open, setOpen] = useState(false);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          className={cn(
            "flex h-10 w-16 flex-col items-center justify-center rounded-md border font-mono text-[10px] tabular-nums transition-colors",
            TONE_BG[tone],
            !entry.enabled && "opacity-40",
          )}
        >
          <span>{entry.factor_stddev.toFixed(2)}</span>
          {wrDelta != null && (
            <span className="opacity-70">
              {wrDelta >= 0 ? "+" : ""}
              {(wrDelta * 100).toFixed(0)}%
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="center" className="w-72">
        <div className="space-y-3">
          <div>
            <div className="font-mono text-sm font-medium text-fg">
              {entry.timeframe} · {entry.strategy}
            </div>
            <div className="text-[11px] text-fg-3">
              {entry.trades} trade nel lookback · peso {entry.weight}
            </div>
          </div>

          {entry.holdout && (
            <div className="grid grid-cols-2 gap-3 border-t border-border pt-3">
              <KvLine label="WR train" value={pct(entry.holdout.train.win_rate)} />
              <KvLine label="WR test" value={pct(entry.holdout.test.win_rate)} />
              <KvLine
                label="PF train"
                value={entry.holdout.train.profit_factor?.toFixed(2) ?? "—"}
              />
              <KvLine
                label="PF test"
                value={entry.holdout.test.profit_factor?.toFixed(2) ?? "—"}
              />
              <KvLine
                label="WR Δ"
                value={
                  entry.holdout.wr_delta != null
                    ? (entry.holdout.wr_delta >= 0 ? "+" : "") + pct(entry.holdout.wr_delta)
                    : "—"
                }
                tone={
                  entry.holdout.wr_delta == null
                    ? "muted"
                    : entry.holdout.wr_delta >= 0
                      ? "success"
                      : "danger"
                }
              />
              <KvLine
                label="Trades"
                value={`${entry.holdout.train.trades}/${entry.holdout.test.trades}`}
              />
            </div>
          )}

          <div className="border-t border-border pt-3">
            <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Factor history (recente → vecchio)
            </div>
            <FactorSpark history={entry.factor_history} />
            <div className="mt-2 flex justify-between text-[11px] text-fg-3">
              <span>stddev</span>
              <span
                className={cn(
                  "font-mono",
                  tone === "success" && "text-success",
                  tone === "warn" && "text-warn",
                  tone === "danger" && "text-danger",
                )}
              >
                {entry.factor_stddev.toFixed(3)}
              </span>
            </div>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function KvLine({ label, value, tone }: { label: string; value: string; tone?: "success" | "danger" | "muted" }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-0.5 font-mono text-sm tabular-nums",
          tone === "success" && "text-success",
          tone === "danger" && "text-danger",
          !tone && "text-fg",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function FactorSpark({ history }: { history: number[] }) {
  if (history.length === 0) {
    return <div className="text-[11px] text-fg-3">Nessuna factor history.</div>;
  }
  const min = Math.min(...history);
  const max = Math.max(...history);
  const range = max - min || 1;
  const w = 240;
  const h = 36;
  const pts = history.map((v, i) => {
    const x = (i / Math.max(history.length - 1, 1)) * w;
    const y = h - ((v - min) / range) * (h - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="block">
      <polyline
        points={pts.join(" ")}
        fill="none"
        stroke="#7aa2f7"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {history.map((v, i) => {
        const x = (i / Math.max(history.length - 1, 1)) * w;
        const y = h - ((v - min) / range) * (h - 4) - 2;
        return <circle key={i} cx={x} cy={y} r={2} fill="#7aa2f7" />;
      })}
    </svg>
  );
}

function LegendDot({ tone, label }: { tone: "success" | "warn" | "danger"; label: string }) {
  const cls =
    tone === "success" ? "bg-success" : tone === "warn" ? "bg-warn" : "bg-danger";
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn("h-2 w-2 rounded-sm", cls)} />
      {label}
    </span>
  );
}

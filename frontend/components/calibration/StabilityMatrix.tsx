"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, Info, Loader2 } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useCalibrationStability, useEquitySlope } from "@/lib/hooks/queries";
import { cn } from "@/lib/utils";
import type { CalibrationStabilityEntry, EquitySlopeDiagnostics } from "@/lib/api";

function pct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function signedPct(value: number | null | undefined, digits = 0) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

function stddevTone(stddev: number, threshold: number): "success" | "warn" | "danger" | "muted" {
  if (!Number.isFinite(stddev)) return "muted";
  if (stddev <= threshold * 0.5) return "success";
  if (stddev <= threshold) return "warn";
  return "danger";
}

function compareTimeframes(a: string, b: string) {
  return timeframeMinutes(a) - timeframeMinutes(b) || a.localeCompare(b);
}

function timeframeMinutes(timeframe: string) {
  const match = /^(\d+)([mhdw])$/.exec(timeframe);
  if (!match) return Number.MAX_SAFE_INTEGER;
  const value = Number(match[1]);
  const unit = match[2];
  if (unit === "m") return value;
  if (unit === "h") return value * 60;
  if (unit === "d") return value * 60 * 24;
  return value * 60 * 24 * 7;
}

const TONE_BG: Record<string, string> = {
  success: "bg-success/15 text-success border-success/30 hover:bg-success/25",
  warn: "bg-warn/15 text-warn border-warn/30 hover:bg-warn/25",
  danger: "bg-danger/15 text-danger border-danger/30 hover:bg-danger/25",
  muted: "bg-bg-2 text-fg-3 border-border hover:bg-bg-3",
};

export function StabilityMatrix() {
  const { data, isLoading } = useCalibrationStability();
  const { data: slopeDiagnostics } = useEquitySlope();

  const { matrix, strategies, timeframes, threshold, candidates, trackedCount, untrackedCount } = useMemo(() => {
    if (!data) {
      return {
        matrix: new Map<string, CalibrationStabilityEntry>(),
        strategies: [],
        timeframes: [],
        threshold: 0.25,
        candidates: [],
        trackedCount: 0,
        untrackedCount: 0,
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
      .filter((e) => e.enabled && e.factor_stddev > thr)
      .sort((a, b) => b.factor_stddev - a.factor_stddev);
    const tracked = data.by_strategy.filter((e) => e.factor_history.length >= 2).length;
    return {
      matrix: map,
      strategies: Array.from(stratSet).sort(),
      timeframes: Array.from(tfSet).sort(compareTimeframes),
      threshold: thr,
      candidates: candidatesArr,
      trackedCount: tracked,
      untrackedCount: data.by_strategy.length - tracked,
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

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_21rem]">
        {/* Matrix grid */}
        <Card className="p-0 overflow-hidden">
          <ScrollArea className="w-full">
            <div className="min-w-max p-5">
              <div
                className="grid gap-1.5"
                style={{ gridTemplateColumns: `minmax(15rem, 18rem) repeat(${timeframes.length}, 5.75rem)` }}
              >
                <div className="sticky left-0 z-10 flex h-9 items-center bg-bg-1 pr-4 text-[10px] font-medium uppercase tracking-wider text-fg-3">
                  Strategia
                </div>
                {timeframes.map((tf) => (
                  <div
                    key={tf}
                    className="flex h-9 w-[5.75rem] items-center justify-center rounded-md bg-bg-2 font-mono text-xs font-medium text-fg-2"
                  >
                    {tf}
                  </div>
                ))}

                {strategies.map((strategy) => (
                  <div key={strategy} className="contents">
                    <div className="sticky left-0 z-10 flex h-14 items-center truncate bg-bg-1 pr-4 font-mono text-sm text-fg-2">
                      {strategy}
                    </div>
                    {timeframes.map((tf) => {
                      const entry = matrix.get(`${tf}|${strategy}`);
                      if (!entry) {
                        return (
                          <div
                            key={tf}
                            className="grid h-14 w-[5.75rem] place-items-center rounded-md border border-dashed border-border text-[10px] uppercase text-fg-3"
                          >
                            n/a
                          </div>
                        );
                      }
                      return <Cell key={tf} entry={entry} threshold={threshold} />;
                    })}
                  </div>
                ))}
              </div>
            </div>
          </ScrollArea>
        </Card>

        <StabilityGuide
          threshold={threshold}
          trackedCount={trackedCount}
          untrackedCount={untrackedCount}
          slopeDiagnostics={slopeDiagnostics}
        />
      </div>

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

function StabilityGuide({
  threshold,
  trackedCount,
  untrackedCount,
  slopeDiagnostics,
}: {
  threshold: number;
  trackedCount: number;
  untrackedCount: number;
  slopeDiagnostics: EquitySlopeDiagnostics | null | undefined;
}) {
  return (
    <Card className="self-start px-5 py-4">
      <div className="flex items-center gap-2 border-b border-border pb-3">
        <Info className="h-4 w-4 text-fg-3" />
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Come leggere
          </div>
          <div className="mt-1 text-xs text-fg-2">
            Stabilita dei factor per timeframe e strategia.
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 border-b border-border py-3">
        <GuideStat label="Soglia" value={threshold.toFixed(2)} />
        <GuideStat label="Tracciate" value={trackedCount.toString()} />
        <GuideStat label="Senza history" value={untrackedCount.toString()} />
      </div>

      <div className="space-y-3 pt-3">
        <GuideItem
          label="Cella"
          value="std e la deviazione dei factor recenti. WR mostra la differenza win-rate test-train quando l'holdout e disponibile."
        />
        <GuideItem
          label="Colori"
          value={`Verde <= ${(threshold * 0.5).toFixed(2)}, giallo <= ${threshold.toFixed(2)}, rosso sopra soglia.`}
        />
        <GuideItem
          label="Tratteggio"
          value="Combinazione timeframe-strategia non configurata nella matrice."
        />
        <GuideItem
          label="—"
          value="Factor history assente o con un solo valore: la stddev non e ancora significativa."
        />
      </div>

      <div className="mt-4 border-t border-border pt-3">
        <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Runtime guards
        </div>
        {slopeDiagnostics ? (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <GuardBadge
                label={slopeDiagnostics.deep_defensive_active ? "Deep defensive" : "Normal"}
                tone={slopeDiagnostics.deep_defensive_active ? "warn" : "success"}
              />
              <GuardBadge
                label={slopeDiagnostics.starvation_override_active ? "Starvation override" : "No override"}
                tone={slopeDiagnostics.starvation_override_active ? "warn" : "muted"}
              />
            </div>
            <div className="grid grid-cols-3 gap-2">
              <GuideStat
                label="Zero cycles"
                value={`${slopeDiagnostics.consecutive_zero_signal_cycles}/${slopeDiagnostics.exit_conditions.starvation_hard_cycles}`}
              />
              <GuideStat
                label="Cooldown"
                value={String(slopeDiagnostics.cooldown_remaining)}
              />
              <GuideStat
                label="Recent +"
                value={`${slopeDiagnostics.recent_positive_streak}/${slopeDiagnostics.exit_conditions.recent_slope_non_negative_cycles}`}
              />
            </div>
            <GuideItem
              label="Exit"
              value={formatGuardReason(slopeDiagnostics.last_exit_reason)}
            />
          </div>
        ) : (
          <div className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-fg-3">
            Stato starvation/deep defensive non disponibile.
          </div>
        )}
      </div>

      <div className="mt-4 border-t border-border pt-3">
        <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Tooltip
        </div>
        <div className="space-y-2">
          <GuideItem label="Trade / peso" value="Numero di posizioni nel lookback e peso strategia attuale." />
          <GuideItem label="WR train/test" value="Win rate sulla parte piu vecchia e piu recente del campione." />
          <GuideItem label="PF train/test" value="Profit factor: profitto lordo diviso perdita lorda." />
          <GuideItem label="WR Delta" value="Test meno train; negativo indica degrado nell'holdout recente." />
          <GuideItem label="Factor history" value="Factor decisi dalle ultime calibrazioni, dal piu recente al piu vecchio." />
        </div>
      </div>
    </Card>
  );
}

function GuardBadge({ label, tone }: { label: string; tone: "success" | "warn" | "muted" }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-mono text-[10px] uppercase",
        tone === "success" && "border-success/40 bg-success/10 text-success",
        tone === "warn" && "border-warn/40 bg-warn/10 text-warn",
        tone === "muted" && "border-border bg-bg-2 text-fg-3",
      )}
    >
      {label}
    </Badge>
  );
}

function GuideStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-fg-3">{label}</div>
      <div className="mt-1 font-mono text-sm font-medium tabular-nums text-fg">{value}</div>
    </div>
  );
}

function formatGuardReason(reason: string | null | undefined) {
  if (!reason) return "Nessuna uscita registrata.";
  return reason.replace(/_/g, " ");
}

function GuideItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3 text-xs">
      <div className="font-mono text-[11px] text-fg-2">{label}</div>
      <div className="leading-relaxed text-fg-3">{value}</div>
    </div>
  );
}

function Cell({ entry, threshold }: { entry: CalibrationStabilityEntry; threshold: number }) {
  const hasEnoughHistory = entry.factor_history.length >= 2;
  const tone = hasEnoughHistory ? stddevTone(entry.factor_stddev, threshold) : "muted";
  const wrDelta = entry.holdout?.wr_delta;
  const [open, setOpen] = useState(false);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          className={cn(
            "relative flex h-14 w-[5.75rem] flex-col justify-center rounded-md border px-2 py-1.5 font-mono tabular-nums transition-colors",
            TONE_BG[tone],
            !entry.enabled && "opacity-60",
          )}
        >
          {!entry.enabled && (
            <span className="absolute right-1 top-1 rounded-sm border border-current/20 px-1 text-[8px] uppercase opacity-70">
              off
            </span>
          )}
          <span className="flex items-baseline justify-between gap-2">
            <span className="text-[9px] uppercase opacity-70">std</span>
            <span className="text-sm font-semibold">
              {hasEnoughHistory ? entry.factor_stddev.toFixed(2) : "—"}
            </span>
          </span>
          <span className="mt-1 flex items-baseline justify-between gap-2">
            <span className="text-[9px] uppercase opacity-70">WR</span>
            <span
              className={cn(
                "text-xs",
                wrDelta == null && "opacity-50",
                wrDelta != null && wrDelta < 0 && "text-danger",
                wrDelta != null && wrDelta >= 0 && "text-success",
              )}
            >
              {wrDelta != null ? signedPct(wrDelta) : "—"}
            </span>
          </span>
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
                  entry.holdout.wr_delta != null ? signedPct(entry.holdout.wr_delta, 1) : "—"
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
                {hasEnoughHistory ? entry.factor_stddev.toFixed(3) : "history insufficiente"}
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

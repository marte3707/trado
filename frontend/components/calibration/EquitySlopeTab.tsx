"use client";

import {
  Activity,
  AlertTriangle,
  Info,
  Loader2,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

import { fmt } from "@/components/atoms";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { useEquitySlope } from "@/lib/hooks/queries";
import type { EquitySlopeDiagnostics } from "@/lib/api";
import { cn } from "@/lib/utils";

type Tone = "success" | "warn" | "danger" | "muted";

const TONE_CLASS: Record<Tone, string> = {
  success: "border-success/40 bg-success/10 text-success",
  warn: "border-warn/40 bg-warn/10 text-warn",
  danger: "border-danger/40 bg-danger/10 text-danger",
  muted: "border-border bg-bg-2 text-fg-3",
};

function statusTone(status: EquitySlopeDiagnostics["status"]): Tone {
  if (status === "improving") return "success";
  if (status === "declining") return "danger";
  return "muted";
}

function slopeTone(value: number, threshold: number): Tone {
  if (!Number.isFinite(value)) return "muted";
  if (value >= 0) return "success";
  if (value <= threshold) return "danger";
  return "warn";
}

function formatSlope(value: number | null | undefined, digits = 3) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function EquitySlopeTab() {
  const { data, isLoading } = useEquitySlope();

  if (isLoading) {
    return (
      <Card className="grid h-64 place-items-center">
        <div className="flex items-center gap-2 text-fg-3">
          <Loader2 className="h-4 w-4 animate-spin" />
          Caricamento curva equity...
        </div>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card className="grid h-36 place-items-center text-sm text-fg-3">
        Diagnostica equity non disponibile.
      </Card>
    );
  }

  const tone = statusTone(data.status);
  const StatusIcon =
    data.status === "improving"
      ? TrendingUp
      : data.status === "declining"
        ? TrendingDown
        : Activity;

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-4">
        <Card className="overflow-hidden p-0">
          <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border px-6 py-5">
            <div>
              <Badge
                variant="outline"
                className={cn("font-mono uppercase", TONE_CLASS[tone])}
              >
                {data.status}
              </Badge>
              <h2 className="mt-3 text-lg font-semibold text-fg">
                Curva equity osservata
              </h2>
              <p className="mt-1 text-xs text-fg-3">
                Trend normalizzato su {data.sample_count} trade paper chiusi.
                Non modifica soglie o sizing.
              </p>
            </div>
            <div className={cn("rounded-lg border p-3", TONE_CLASS[tone])}>
              <StatusIcon className="h-5 w-5" />
            </div>
          </div>

          <div className="grid gap-3 p-5 sm:grid-cols-2 xl:grid-cols-4">
            <MetricTile
              label="Slope normalizzata"
              value={formatSlope(data.normalized_slope)}
              tone={slopeTone(data.normalized_slope, data.threshold)}
              hint={`Riferimento ${formatSlope(data.threshold)}`}
            />
            <MetricTile
              label="Slope recente"
              value={formatSlope(data.recent_normalized_slope)}
              tone={slopeTone(data.recent_normalized_slope, data.threshold)}
              hint="Ultimi 50 trade"
            />
            <MetricTile
              label="Slope grezza"
              value={formatSlope(data.raw_slope, 4)}
              tone={slopeTone(data.normalized_slope, data.threshold)}
            />
            <MetricTile
              label="PnL assoluto mediano"
              value={fmt.money(data.median_abs_pnl)}
              tone="muted"
              hint="Scala di normalizzazione"
            />
          </div>
        </Card>

        <Card className="overflow-hidden p-0">
          <div className="border-b border-border px-6 py-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Finestre temporali
            </div>
            <p className="mt-0.5 text-xs text-fg-3">
              Confronto tra comportamento recente e storico.
            </p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[680px] border-separate border-spacing-0">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-fg-3">
                  <th className="px-6 py-3 font-medium">Finestra</th>
                  <th className="px-4 py-3 font-medium">Campione</th>
                  <th className="px-4 py-3 font-medium">Normalizzata</th>
                  <th className="px-4 py-3 font-medium">Grezza</th>
                  <th className="px-4 py-3 font-medium">PnL abs mediano</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.windows.map((row) => {
                  const rowTone = slopeTone(row.normalized_slope, data.threshold);
                  return (
                    <tr key={row.window} className="text-sm">
                      <td className="px-6 py-3 font-mono text-fg">{row.window}</td>
                      <td className="px-4 py-3 font-mono tabular-nums text-fg-2">
                        {row.sample_count}
                      </td>
                      <td className="px-4 py-3">
                        <Badge
                          variant="outline"
                          className={cn("font-mono", TONE_CLASS[rowTone])}
                        >
                          {formatSlope(row.normalized_slope)}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 font-mono tabular-nums text-fg-2">
                        {formatSlope(row.raw_slope, 4)}
                      </td>
                      <td className="px-4 py-3 font-mono tabular-nums text-fg-2">
                        {fmt.money(row.median_abs_pnl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      <aside className="space-y-4">
        <Card className="p-5">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <Activity className="h-4 w-4 text-fg-3" />
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Continuità del flusso
              </div>
              <p className="mt-1 text-xs text-fg-2">
                Cicli eleggibili consecutivi senza nuovi segnali.
              </p>
            </div>
          </div>
          <div className="pt-4">
            <MetricTile
              label="Zero signal cycles"
              value={String(data.consecutive_zero_signal_cycles)}
              tone={data.consecutive_zero_signal_cycles > 0 ? "warn" : "muted"}
            />
          </div>
        </Card>

        <Card className="p-5">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <AlertTriangle className="h-4 w-4 text-warn" />
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Impatto time-stop
              </div>
              <p className="mt-1 text-xs text-fg-2">
                Ultime {data.recent_time_stop_impact.hours} ore.
              </p>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3 pt-4">
            <MetricTile
              label="Chiusure"
              value={String(data.recent_time_stop_impact.closed_count)}
              tone="muted"
            />
            <MetricTile
              label="PnL"
              value={fmt.pnl(data.recent_time_stop_impact.realized_pnl)}
              tone={
                data.recent_time_stop_impact.realized_pnl >= 0
                  ? "success"
                  : "danger"
              }
            />
          </div>
        </Card>

        <Card className="p-5">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <Info className="h-4 w-4 text-fg-3" />
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Lettura
            </div>
          </div>
          <div className="space-y-3 pt-3 text-xs leading-5 text-fg-3">
            <p>
              La normalizzazione divide la slope per il PnL assoluto mediano,
              rendendo confrontabili periodi con size diverse.
            </p>
            <p>
              Il riferimento negativo classifica il trend; è diagnostico e non
              attiva automaticamente protezioni o variazioni di rischio.
            </p>
          </div>
        </Card>
      </aside>
    </div>
  );
}

function MetricTile({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone: Tone;
  hint?: string;
}) {
  return (
    <div className="rounded-md border border-border bg-bg-2 px-4 py-3">
      <div className="text-[10px] uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-1 font-mono text-xl font-semibold tabular-nums",
          tone === "success" && "text-success",
          tone === "warn" && "text-warn",
          tone === "danger" && "text-danger",
          tone === "muted" && "text-fg",
        )}
      >
        {value}
      </div>
      {hint ? <div className="mt-1 text-[11px] text-fg-3">{hint}</div> : null}
    </div>
  );
}

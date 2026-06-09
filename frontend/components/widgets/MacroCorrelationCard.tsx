"use client";

import { LineChart } from "lucide-react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { useMacroCorrelation } from "@/lib/hooks/queries";

type Tone = "danger" | "warn" | "muted" | "success";

// La magnitudine della correlazione indica quanto BTC è esposto al rischio
// macro azionario: alto = accoppiamento forte (risk-off condiviso), basso =
// regime crypto-nativo (decoupling). Il segno non è di per sé buono/cattivo.
function couplingTone(value: number | null | undefined): Tone {
  if (value == null) return "muted";
  const a = Math.abs(value);
  if (a >= 0.7) return "danger";
  if (a >= 0.4) return "warn";
  return "muted";
}

function couplingLabel(value: number | null | undefined): string {
  if (value == null) return "Dati insufficienti";
  const a = Math.abs(value);
  if (a >= 0.7) return "Accoppiamento forte";
  if (a >= 0.4) return "Accoppiamento moderato";
  return "Debole / decoupling";
}

const toneClass: Record<Tone, string> = {
  danger: "text-danger",
  warn: "text-warn",
  muted: "text-fg",
  success: "text-success",
};

export function MacroCorrelationCard() {
  const { data, isLoading } = useMacroCorrelation();
  const corr = data?.correlation ?? null;
  const beta = data?.beta ?? null;
  const samples = data?.samples ?? 0;
  const tone = couplingTone(corr);

  return (
    <Card className="space-y-3 p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-fg-3">
          <LineChart className="h-3.5 w-3.5" />
          Macro XYZ100 ↔ BTC
        </div>
        <span className="text-[10px] font-medium uppercase tracking-wider text-fg-4">
          1h · rendimenti
        </span>
      </div>

      <div className="flex items-baseline gap-2">
        <span className={cn("font-mono text-2xl font-semibold tabular-nums", toneClass[tone])}>
          {corr != null ? corr.toFixed(2) : "—"}
        </span>
        <span className="truncate text-xs text-fg-3">{couplingLabel(corr)}</span>
      </div>

      <div className="grid grid-cols-2 gap-1.5">
        <MiniMetric label="Beta BTC/Nasdaq" value={beta != null ? beta.toFixed(2) : "—"} />
        <MiniMetric label="Campioni" value={isLoading ? "…" : String(samples)} />
      </div>

      <p className="text-[10px] leading-snug text-fg-4">
        {"Solo monitoraggio: XYZ100 non genera segnali. Correlazione instabile e asimmetrica; fuori dall'orario USA il mark XYZ100 è rumoroso."}
      </p>
    </Card>
  );
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-bg-2/45 px-2 py-1">
      <div className="text-[9px] font-medium uppercase tracking-wider text-fg-4">{label}</div>
      <div className="truncate font-mono text-[11px] tabular-nums text-fg-2">{value}</div>
    </div>
  );
}

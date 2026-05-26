"use client";

import { useEffect, useState } from "react";
import { Filter } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Slider } from "@/components/ui/slider";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSettings } from "@/lib/hooks/queries";
import { useUpdateScoreThreshold } from "@/lib/hooks/mutations";

function asNumber(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function ThresholdMenu() {
  const { data: settings } = useSettings();
  const { mutate: saveThreshold, isPending } = useUpdateScoreThreshold();

  const persisted = asNumber(settings?.risk?.min_signal_score) ?? 60;
  const [local, setLocal] = useState<number>(persisted);

  // Riallinea local quando il valore persistito cambia (es. mutation altrove).
  useEffect(() => {
    setLocal(persisted);
  }, [persisted]);

  const dirty = local !== persisted;

  const commit = () => {
    if (!dirty) return;
    saveThreshold({ threshold: local, currentRisk: (settings?.risk as Record<string, unknown>) ?? {} });
  };

  return (
    <Popover onOpenChange={(open: boolean) => { if (!open) commit(); }}>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              className="inline-flex h-8 items-center gap-2 rounded-md border border-border bg-bg-1 px-3 text-xs text-fg-2 transition-colors hover:bg-bg-2"
              aria-label="Soglia score minima"
            >
              <Filter className="h-3.5 w-3.5" />
              <span className="font-mono">Score ≥ {local}</span>
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">Soglia score minima per i segnali</TooltipContent>
      </Tooltip>

      <PopoverContent align="end" className="w-72">
        <div className="space-y-3">
          <div>
            <div className="text-sm font-medium">Soglia score minima</div>
            <div className="text-xs text-fg-3">
              I segnali sotto questa soglia sono filtrati dalla dashboard.
            </div>
          </div>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-fg-3">Score minimo</span>
              <span className="font-mono text-fg">{local}</span>
            </div>
            <Slider
              value={[local]}
              onValueChange={(v: number[]) => setLocal(v[0])}
              min={0}
              max={100}
              step={5}
              disabled={isPending}
            />
            <div className="flex justify-between font-mono text-[10px] text-fg-4">
              <span>0</span>
              <span>50</span>
              <span>100</span>
            </div>
          </div>
          {dirty && (
            <div className="border-t border-border pt-2 text-[11px] text-fg-3">
              Chiudi questo menu per salvare ({persisted} → {local}).
            </div>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

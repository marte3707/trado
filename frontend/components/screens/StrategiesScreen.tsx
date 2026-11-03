"use client";

import { useMemo, useState } from "react";

import { Card } from "@/components/ui/card";
import { Slider } from "@/components/ui/slider";
import { StrategyTimeframeAccordion } from "@/components/strategies/StrategyTimeframeAccordion";
import { useUpdateScoreThreshold } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { AppSettings, Strategy } from "@/lib/api";

interface Props {
  strategies: Strategy[];
  settings: AppSettings;
  threshold: number;
  onThresholdChange: (t: number) => void | Promise<void>;
  onRefresh: () => void | Promise<void>;
}

export function StrategiesScreen({ strategies, settings, threshold }: Props) {
  const [localThreshold, setLocalThreshold] = useState(threshold);
  const { mutate: saveThreshold } = useUpdateScoreThreshold();

  const enabledCount = useMemo(() => strategies.filter((s) => s.enabled).length, [strategies]);
  const totalCount = strategies.length;
  const timeframesCount = useMemo(
    () => new Set(strategies.map((s) => s.timeframe)).size,
    [strategies],
  );
  const totalActiveWeight = useMemo(
    () => strategies.reduce((sum, s) => sum + (s.enabled ? s.weight : 0), 0),
    [strategies],
  );

  return (
    <div className="space-y-6 px-7 py-7">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Strategie</h1>
          <p className="mt-1 text-xs text-fg-3">
            Pesi e parametri per timeframe · score minimo globale e soglie
          </p>
        </div>
      </div>

      {/* Stats overview */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Strategie attive"
          value={`${enabledCount}/${totalCount}`}
          hint="abilitate / totali"
        />
        <StatTile
          label="Timeframes"
          value={String(timeframesCount)}
          hint="con almeno una strategia"
        />
        <StatTile
          label="Peso totale attivo"
          value={String(totalActiveWeight)}
          hint="somma su tutti i timeframe"
        />
        <StatTile
          label="Score minimo globale"
          value={String(localThreshold)}
          hint="threshold attuale dei segnali"
        />
      </div>

      {/* Threshold control */}
      <Card className="p-6">
        <div className="flex flex-wrap items-center gap-6">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Score minimo globale
            </div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-semibold tabular-nums text-fg">
                {localThreshold}
              </span>
              <span className="text-xs text-fg-3">
                segnali sotto questa soglia sono filtrati
              </span>
            </div>
          </div>
          <div className="w-full sm:w-80">
            <Slider
              value={[localThreshold]}
              min={0}
              max={100}
              step={5}
              onValueChange={(v: number[]) => setLocalThreshold(v[0])}
              onValueCommit={() => {
                if (localThreshold !== threshold) {
                  saveThreshold({
                    threshold: localThreshold,
                    currentRisk: (settings.risk as Record<string, unknown>) ?? {},
                  });
                }
              }}
            />
            <div className="mt-1.5 flex justify-between font-mono text-[10px] text-fg-4">
              <span>0</span>
              <span>50</span>
              <span>100</span>
            </div>
          </div>
        </div>
      </Card>

      {/* Accordion per timeframe */}
      <StrategyTimeframeAccordion strategies={strategies} />
    </div>
  );
}

function StatTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card className={cn("p-5")}>
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div className="mt-2 font-mono text-2xl font-semibold tabular-nums text-fg">{value}</div>
      {hint && <div className="mt-1 text-[11px] text-fg-3">{hint}</div>}
    </Card>
  );
}

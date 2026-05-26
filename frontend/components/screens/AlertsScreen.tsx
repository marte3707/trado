"use client";

import { useState } from "react";
import { toast } from "sonner";

import { AlertConfigPanel } from "@/components/alerts/AlertConfigPanel";
import { AlertFeed } from "@/components/alerts/AlertFeed";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import type { AlertItem } from "@/components/alerts/alertHelpers";
import { useDecisionLogs, usePositions, useSignals } from "@/lib/hooks/queries";
import type { AppSettings, DecisionLog, Market, Position, Signal } from "@/lib/api";

interface Props {
  logs: DecisionLog[];
  markets: Market[];
  settings: AppSettings;
  onRefresh: () => void | Promise<void>;
  onOpenSignal?: (signal: Signal) => void;
  onOpenPosition?: (position: Position) => void;
  onOpenMarket?: (market: Market) => void;
}

export function AlertsScreen({
  logs: initialLogs,
  markets,
  settings,
  onOpenSignal,
  onOpenPosition,
  onOpenMarket,
}: Props) {
  const { data: logs = initialLogs } = useDecisionLogs({ initialData: initialLogs });
  const { data: signals = [] } = useSignals("min_score=0");
  const { data: positions = [] } = usePositions();
  const [selected, setSelected] = useState<AlertItem | null>(null);

  function handleNavigateSignal(signalId: number) {
    const signal = signals.find((s) => s.id === signalId);
    if (!signal) {
      toast.error(`Segnale #${signalId} non disponibile (cache locale).`);
      return;
    }
    setSelected(null);
    onOpenSignal?.(signal);
  }

  function handleNavigatePosition(positionId: number) {
    const position = positions.find((p) => p.id === positionId);
    if (!position) {
      toast.error(`Posizione #${positionId} non disponibile (cache locale).`);
      return;
    }
    setSelected(null);
    onOpenPosition?.(position);
  }

  function handleOpenMarket(market: Market) {
    setSelected(null);
    onOpenMarket?.(market);
  }

  return (
    <div className="flex h-full flex-col gap-4 px-7 py-7">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Alert center</h1>
          <p className="mt-1 text-xs text-fg-3">
            Configura canali e regole, naviga il feed eventi, ispeziona payload completi
          </p>
        </div>
      </div>

      <div
        className="grid min-h-0 flex-1 gap-4"
        style={{ gridTemplateColumns: "320px 1fr" }}
      >
        <AlertConfigPanel settings={settings} />
        <AlertFeed
          logs={logs}
          selectedId={selected?.id ?? null}
          onSelect={setSelected}
        />
      </div>

      <AlertDetailDrawer
        alert={selected}
        markets={markets}
        onClose={() => setSelected(null)}
        onNavigateSignal={handleNavigateSignal}
        onNavigatePosition={handleNavigatePosition}
        onOpenMarket={handleOpenMarket}
      />
    </div>
  );
}

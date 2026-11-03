"use client";

import { useEffect, useState } from "react";
import { Check, Save } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import type { AppSettings } from "@/lib/api";

interface Props {
  settings: AppSettings;
}

const MODE_OPTIONS = [
  { value: "SIGNAL_ONLY", label: "Signal only", description: "Solo segnali, nessuna posizione." },
  { value: "PAPER_TRADING", label: "Paper trading", description: "Esegue posizioni simulate su equity virtuale." },
  { value: "AUTO_TRADE", label: "Auto-trade", description: "Paper + posizioni live su Hyperliquid." },
] as const;

const TF_OPTIONS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"];

export function OperationTab({ settings }: Props) {
  // Struttura backend:
  //   settings.operation_mode = { mode: "AUTO_TRADE", enable_live_trading: bool }
  //   settings.timeframes     = { enabled: ["1m", "15m", ...], monitoring_only: ["5m"] }
  // Patch wrapping: { key: { value: <intero dict> } }
  const operationMode = (settings.operation_mode ?? {}) as Record<string, unknown>;
  const persistedMode = (operationMode.mode as string) ?? "SIGNAL_ONLY";
  const persistedEnableLive = Boolean(operationMode.enable_live_trading);

  const persistedTfs = Array.isArray(settings.timeframes?.enabled)
    ? (settings.timeframes.enabled as string[])
    : [];
  const persistedMonitoringOnlyTfs = Array.isArray(settings.timeframes?.monitoring_only)
    ? (settings.timeframes.monitoring_only as string[])
    : [];

  const [mode, setMode] = useState(persistedMode);
  const [tfs, setTfs] = useState<string[]>(persistedTfs);
  const [monitoringOnlyTfs, setMonitoringOnlyTfs] = useState<string[]>(persistedMonitoringOnlyTfs);

  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setMode(persistedMode);
    setTfs(persistedTfs);
    setMonitoringOnlyTfs(persistedMonitoringOnlyTfs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persistedMode, persistedTfs.join(","), persistedMonitoringOnlyTfs.join(",")]);

  const dirty = (
    mode !== persistedMode
    || tfs.join(",") !== persistedTfs.join(",")
    || monitoringOnlyTfs.join(",") !== persistedMonitoringOnlyTfs.join(",")
  );

  function toggleActiveTf(tf: string) {
    if (!tfs.includes(tf)) {
      setMonitoringOnlyTfs((curr) => curr.filter((value) => value !== tf));
    }
    setTfs((curr) => (curr.includes(tf) ? curr.filter((t) => t !== tf) : [...curr, tf]));
  }

  function toggleMonitoringTf(tf: string) {
    if (!monitoringOnlyTfs.includes(tf)) {
      setTfs((curr) => curr.filter((value) => value !== tf));
    }
    setMonitoringOnlyTfs((curr) => (
      curr.includes(tf) ? curr.filter((t) => t !== tf) : [...curr, tf]
    ));
  }

  function handleSave() {
    const patch: Record<string, { value: Record<string, unknown> }> = {};
    if (mode !== persistedMode) {
      patch.operation_mode = {
        value: { ...operationMode, mode, enable_live_trading: persistedEnableLive },
      };
    }
    if (
      tfs.join(",") !== persistedTfs.join(",")
      || monitoringOnlyTfs.join(",") !== persistedMonitoringOnlyTfs.join(",")
    ) {
      patch.timeframes = {
        value: {
          ...(settings.timeframes ?? {}),
          enabled: tfs,
          monitoring_only: monitoringOnlyTfs,
        },
      };
    }
    if (Object.keys(patch).length === 0) return;
    updateSettings(patch as Partial<AppSettings>);
  }

  return (
    <div className="space-y-5">
      <Card className="p-6">
        <SectionTitle title="Operation mode" hint="Determina cosa fa l'engine al momento della generazione segnale" />
        <div className="mt-3 grid grid-cols-1 gap-2.5 md:grid-cols-3">
          {MODE_OPTIONS.map((opt) => {
            const active = mode === opt.value;
            return (
              <button
                key={opt.value}
                onClick={() => setMode(opt.value)}
                className={cn(
                  "rounded-md border p-4 text-left transition-colors",
                  active
                    ? "border-success bg-success/10 text-fg"
                    : "border-border bg-bg-1 text-fg-2 hover:bg-bg-2",
                )}
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-sm font-semibold">{opt.label}</span>
                  {active && <Check className="h-4 w-4 text-success" />}
                </div>
                <div className="mt-1.5 text-[11px] text-fg-3">{opt.description}</div>
              </button>
            );
          })}
        </div>
      </Card>

      <Card className="p-6">
        <SectionTitle
          title="Active timeframes"
          hint="Timeframe su cui il worker genera segnali ad ogni ciclo"
        />
        <div className="mt-3 flex flex-wrap gap-2">
          {TF_OPTIONS.map((tf) => {
            const active = tfs.includes(tf);
            return (
              <button
                key={tf}
                onClick={() => toggleActiveTf(tf)}
                className={cn(
                  "rounded-md border px-3 py-1.5 font-mono text-xs transition-colors",
                  active
                    ? "border-success/40 bg-success/10 text-success"
                    : "border-border bg-bg-1 text-fg-3 hover:bg-bg-2",
                )}
              >
                {tf}
              </button>
            );
          })}
        </div>
        <div className="mt-3 text-[11px] text-fg-3">
          {tfs.length} timeframe attivi
        </div>
      </Card>

      <Card className="p-6">
        <SectionTitle
          title="Monitoring-only timeframes"
          hint="Sincronizzati per correlazioni e BTC macro protection, senza generare segnali"
        />
        <div className="mt-3 flex flex-wrap gap-2">
          {TF_OPTIONS.map((tf) => {
            const active = monitoringOnlyTfs.includes(tf);
            return (
              <button
                key={tf}
                onClick={() => toggleMonitoringTf(tf)}
                className={cn(
                  "rounded-md border px-3 py-1.5 font-mono text-xs transition-colors",
                  active
                    ? "border-info/40 bg-info/10 text-info"
                    : "border-border bg-bg-1 text-fg-3 hover:bg-bg-2",
                )}
              >
                {tf}
              </button>
            );
          })}
        </div>
        <div className="mt-3 text-[11px] text-fg-3">
          {monitoringOnlyTfs.length} timeframe solo monitoraggio
        </div>
      </Card>

      <SaveBar dirty={dirty} isPending={isPending} onSave={handleSave} />
    </div>
  );
}

function SectionTitle({ title, hint, badge }: { title: string; hint?: string; badge?: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</div>
        {hint && <div className="mt-0.5 text-xs text-fg-3">{hint}</div>}
      </div>
      {badge && (
        <Badge variant="outline" className="font-mono">
          {badge}
        </Badge>
      )}
    </div>
  );
}

function SaveBar({
  dirty,
  isPending,
  onSave,
}: {
  dirty: boolean;
  isPending: boolean;
  onSave: () => void;
}) {
  if (!dirty) return null;
  return (
    <Card className="sticky bottom-4 z-10 flex items-center gap-3 border-warn/40 bg-warn/10 px-5 py-3 backdrop-blur">
      <span className="text-sm text-warn">Modifiche non salvate</span>
      <div className="flex-1" />
      <Button size="sm" onClick={onSave} disabled={isPending}>
        <Save className="mr-1.5 h-3.5 w-3.5" />
        {isPending ? "Salvataggio..." : "Salva modifiche"}
      </Button>
    </Card>
  );
}

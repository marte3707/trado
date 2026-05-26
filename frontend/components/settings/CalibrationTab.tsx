"use client";

import { useEffect, useState } from "react";
import { Save } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { AppSettings } from "@/lib/api";

interface Props {
  settings: AppSettings;
}

function toNum(v: unknown, fallback: number) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}
function toBool(v: unknown, fallback = false) {
  if (v == null) return fallback;
  return Boolean(v);
}

export function CalibrationTab({ settings }: Props) {
  const ac = (settings.auto_calibration ?? {}) as Record<string, unknown>;

  const persisted = {
    enabled: toBool(ac.enabled, true),
    lookbackTrades: toNum(ac.lookback_trades, 500),
    minClosedTrades: toNum(ac.min_closed_trades, 20),
    minStrategyTrades: toNum(ac.min_strategy_trades, 20),
    minWeight: toNum(ac.min_weight, 1),
    maxWeight: toNum(ac.max_weight, 30),
    minScoreFloor: toNum(ac.min_signal_score_floor, 55),
    minScoreCeiling: toNum(ac.min_signal_score_ceiling, 80),
    minRRFloor: toNum(ac.min_risk_reward_floor, 2.0),
    minRRCeiling: toNum(ac.min_risk_reward_ceiling, 3.0),
    minRiskPerTradePct: toNum(ac.min_risk_per_trade_pct, 0.25),
    deepDefensiveSlopeThreshold: toNum(ac.deep_defensive_slope_threshold, -0.1),
    autoDisableRetestAfterTrades: toNum(ac.auto_disable_retest_after_trades, 200),
    walkForwardTrainRatio: toNum(ac.walk_forward_train_ratio, 0.7),
    factorHistoryWindow: toNum(ac.factor_history_window, 5),
    factorHistoryStdThreshold: toNum(ac.factor_history_std_threshold, 0.25),
  };

  const [local, setLocal] = useState(persisted);
  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setLocal(persisted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(persisted)]);

  const dirty = JSON.stringify(local) !== JSON.stringify(persisted);

  function handleSave() {
    updateSettings({
      auto_calibration: {
        value: {
          ...ac,
          enabled: local.enabled,
          lookback_trades: local.lookbackTrades,
          min_closed_trades: local.minClosedTrades,
          min_strategy_trades: local.minStrategyTrades,
          min_weight: local.minWeight,
          max_weight: local.maxWeight,
          min_signal_score_floor: local.minScoreFloor,
          min_signal_score_ceiling: local.minScoreCeiling,
          min_risk_reward_floor: local.minRRFloor,
          min_risk_reward_ceiling: local.minRRCeiling,
          min_risk_per_trade_pct: local.minRiskPerTradePct,
          deep_defensive_slope_threshold: local.deepDefensiveSlopeThreshold,
          auto_disable_retest_after_trades: local.autoDisableRetestAfterTrades,
          walk_forward_train_ratio: local.walkForwardTrainRatio,
          factor_history_window: local.factorHistoryWindow,
          factor_history_std_threshold: local.factorHistoryStdThreshold,
        },
      },
    } as Partial<AppSettings>);
  }

  return (
    <div className="space-y-5">
      <Card className="p-6">
        <SectionHeader
          title="Master switch"
          hint="Quando attivo, la calibrazione automatica gira ad ogni ciclo worker"
        >
          <Switch
            checked={local.enabled}
            onCheckedChange={(v: boolean) => setLocal((s) => ({ ...s, enabled: v }))}
          />
        </SectionHeader>
        <div className="mt-3 text-[11px] text-fg-3">
          La calibrazione modifica weights e parametri delle strategie sulla base degli ultimi trade
          chiusi. Il modulo di auto-tuner (tab Calibration) usa questi stessi parametri.
        </div>
      </Card>

      <Card className={cn("p-6", !local.enabled && "opacity-60")}>
        <SectionHeader title="Lookback & eligibility" hint="Quanti trade osservare e soglie minime per agire" />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
          <Field
            label="Lookback trades"
            hint="Trade chiusi piu' recenti considerati per la calibrazione"
            value={local.lookbackTrades}
            step={50}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, lookbackTrades: v }))}
          />
          <Field
            label="Min closed trades"
            hint="Trade chiusi minimi prima di poter applicare cambi globali"
            value={local.minClosedTrades}
            step={5}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minClosedTrades: v }))}
          />
          <Field
            label="Min strategy trades"
            hint="Trade per strategia minimi prima di toccare il suo peso/params"
            value={local.minStrategyTrades}
            step={5}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minStrategyTrades: v }))}
          />
        </div>
      </Card>

      <Card className={cn("p-6", !local.enabled && "opacity-60")}>
        <SectionHeader title="Weight bounds" hint="Range entro cui il calibratore puo' muovere i pesi delle strategie" />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field
            label="Min weight"
            hint="Peso minimo possibile per una strategia"
            value={local.minWeight}
            step={1}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minWeight: v }))}
          />
          <Field
            label="Max weight"
            hint="Peso massimo possibile per una strategia"
            value={local.maxWeight}
            step={1}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, maxWeight: v }))}
          />
        </div>
      </Card>

      <Card className={cn("p-6", !local.enabled && "opacity-60")}>
        <SectionHeader title="Score & R:R bounds" hint="Range entro cui il calibratore puo' muovere risk thresholds" />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Field
            label="Score floor"
            hint="Score minimo (limite inferiore)"
            value={local.minScoreFloor}
            step={1}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minScoreFloor: v }))}
          />
          <Field
            label="Score ceiling"
            hint="Score massimo (limite superiore)"
            value={local.minScoreCeiling}
            step={1}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minScoreCeiling: v }))}
          />
          <Field
            label="R:R floor"
            hint="R:R minimo applicabile"
            value={local.minRRFloor}
            step={0.1}
            suffix="x"
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minRRFloor: v }))}
          />
          <Field
            label="R:R ceiling"
            hint="R:R massimo applicabile"
            value={local.minRRCeiling}
            step={0.1}
            suffix="x"
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minRRCeiling: v }))}
          />
          <Field
            label="Min risk per trade"
            hint="% di equity minimo rischio applicabile"
            value={local.minRiskPerTradePct}
            step={0.05}
            suffix="%"
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, minRiskPerTradePct: v }))}
          />
        </div>
      </Card>

      <Card className={cn("p-6", !local.enabled && "opacity-60")}>
        <SectionHeader title="Walk-forward & stability" hint="Parametri della stability matrix e overfitting detection" />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
          <Field
            label="Train ratio"
            hint="Quota di trade nella finestra di train (resto = test)"
            value={local.walkForwardTrainRatio}
            step={0.05}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, walkForwardTrainRatio: v }))}
          />
          <Field
            label="Factor history window"
            hint="Ultimi N factor di calibrazione considerati per stddev"
            value={local.factorHistoryWindow}
            step={1}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, factorHistoryWindow: v }))}
          />
          <Field
            label="Factor stddev threshold"
            hint="Stddev oltre cui (strategy × TF) e' considerata overfitted"
            value={local.factorHistoryStdThreshold}
            step={0.05}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, factorHistoryStdThreshold: v }))}
          />
        </div>
      </Card>

      <Card className={cn("p-6", !local.enabled && "opacity-60")}>
        <SectionHeader
          title="Deep defensive & retest"
          hint="Modalita' difensiva e ritmo di retest delle strategie disabilitate"
        />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field
            label="Slope threshold (defensive)"
            hint="Equity slope normalizzato sotto cui scatta deep defensive"
            value={local.deepDefensiveSlopeThreshold}
            step={0.05}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, deepDefensiveSlopeThreshold: v }))}
          />
          <Field
            label="Retest after N trades"
            hint="Numero di trade prima di ritestare una strategia disabilitata"
            value={local.autoDisableRetestAfterTrades}
            step={25}
            disabled={!local.enabled}
            onChange={(v) => setLocal((s) => ({ ...s, autoDisableRetestAfterTrades: v }))}
          />
        </div>
      </Card>

      {dirty && (
        <Card className="sticky bottom-4 z-10 flex items-center gap-3 border-warn/40 bg-warn/10 px-5 py-3">
          <span className="text-sm text-warn">Modifiche non salvate</span>
          <div className="flex-1" />
          <Button size="sm" onClick={handleSave} disabled={isPending}>
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {isPending ? "Salvataggio..." : "Salva calibration config"}
          </Button>
        </Card>
      )}
    </div>
  );
}

function SectionHeader({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</div>
        {hint && <div className="mt-0.5 text-xs text-fg-3">{hint}</div>}
      </div>
      {children}
    </div>
  );
}

function Field({
  label,
  hint,
  value,
  step,
  onChange,
  suffix,
  disabled,
}: {
  label: string;
  hint: string;
  value: number;
  step: number;
  onChange: (v: number) => void;
  suffix?: string;
  disabled?: boolean;
}) {
  return (
    <div>
      <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</Label>
      <div className="mt-2 flex items-center gap-2">
        <Input
          type="number"
          value={value}
          step={step}
          disabled={disabled}
          onChange={(e) => onChange(Number(e.target.value))}
          className="font-mono"
        />
        {suffix && <span className="font-mono text-sm text-fg-3">{suffix}</span>}
      </div>
      <div className="mt-1.5 text-[11px] text-fg-3">{hint}</div>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { Save } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import type { AppSettings } from "@/lib/api";

interface Props {
  settings: AppSettings;
}

function toNum(v: unknown, fallback: number) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export function RiskTab({ settings }: Props) {
  const risk = (settings.risk ?? {}) as Record<string, unknown>;

  const persisted = {
    minScore: toNum(risk.min_signal_score, 60),
    minRR: toNum(risk.min_risk_reward, 2),
    minVolume: toNum(risk.min_average_quote_volume_usd, 50_000),
    maxRiskPct: toNum(risk.max_risk_per_trade_pct, 1),
    maxDailyLossPct: toNum(risk.max_daily_loss_pct, 3),
    maxPositions: toNum(risk.max_open_positions, 5),
    maxExposurePct: toNum(risk.max_total_exposure_pct, 25),
    maxLeverage: toNum(risk.max_leverage, 3),
  };

  const [local, setLocal] = useState(persisted);
  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setLocal(persisted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    persisted.minScore,
    persisted.minRR,
    persisted.minVolume,
    persisted.maxRiskPct,
    persisted.maxDailyLossPct,
    persisted.maxPositions,
    persisted.maxExposurePct,
    persisted.maxLeverage,
  ]);

  const dirty = Object.keys(persisted).some(
    (k) => local[k as keyof typeof local] !== persisted[k as keyof typeof persisted],
  );

  function handleSave() {
    const nextRisk = {
      ...risk,
      min_signal_score: local.minScore,
      min_risk_reward: local.minRR,
      min_average_quote_volume_usd: local.minVolume,
      max_risk_per_trade_pct: local.maxRiskPct,
      max_daily_loss_pct: local.maxDailyLossPct,
      max_open_positions: local.maxPositions,
      max_total_exposure_pct: local.maxExposurePct,
      max_leverage: local.maxLeverage,
    };
    updateSettings({ risk: { value: nextRisk } } as Partial<AppSettings>);
  }

  return (
    <div className="space-y-5">
      {/* Signal entry filters */}
      <Card className="p-6">
        <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
          Score minimo segnale
        </Label>
        <div className="mt-2 flex items-center gap-4">
          <span className="w-12 font-mono text-2xl font-semibold tabular-nums text-fg">
            {local.minScore}
          </span>
          <Slider
            value={[local.minScore]}
            min={0}
            max={100}
            step={5}
            onValueChange={(v: number[]) => setLocal((s) => ({ ...s, minScore: v[0] }))}
          />
        </div>
        <div className="mt-1.5 text-[11px] text-fg-3">
          Soglia globale: segnali con score &lt; {local.minScore} non passano il filtro.
        </div>
      </Card>

      <Card className="p-6">
        <SectionTitle title="Filtri pre-trade" hint="Vincoli minimi affinche' un segnale possa diventare posizione" />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field
            label="Min Risk/Reward"
            hint="Risk:reward minimo richiesto per eseguire"
            value={local.minRR}
            step={0.1}
            suffix="x"
            onChange={(v) => setLocal((s) => ({ ...s, minRR: v }))}
          />
          <Field
            label="Min volume medio (USD)"
            hint="Volume quote medio sul mercato richiesto"
            value={local.minVolume}
            step={1000}
            suffix="$"
            onChange={(v) => setLocal((s) => ({ ...s, minVolume: v }))}
          />
        </div>
      </Card>

      {/* Per-trade & portfolio caps */}
      <Card className="p-6">
        <SectionTitle title="Per-trade & portfolio" hint="Cap globali su rischio e leva" />
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Field
            label="Max risk per trade"
            hint="% di equity rischiata per singola posizione"
            value={local.maxRiskPct}
            step={0.1}
            suffix="%"
            onChange={(v) => setLocal((s) => ({ ...s, maxRiskPct: v }))}
          />
          <Field
            label="Max daily loss"
            hint="Drawdown massimo giornaliero prima del lockout"
            value={local.maxDailyLossPct}
            step={0.5}
            suffix="%"
            onChange={(v) => setLocal((s) => ({ ...s, maxDailyLossPct: v }))}
          />
          <Field
            label="Max esposizione totale"
            hint="% equity in posizioni open simultaneamente"
            value={local.maxExposurePct}
            step={1}
            suffix="%"
            onChange={(v) => setLocal((s) => ({ ...s, maxExposurePct: v }))}
          />
          <Field
            label="Max leverage"
            hint="Leva massima applicabile per posizione"
            value={local.maxLeverage}
            step={0.5}
            suffix="x"
            onChange={(v) => setLocal((s) => ({ ...s, maxLeverage: v }))}
          />
        </div>
      </Card>

      <Card className="p-6">
        <SectionTitle
          title="Posizioni open simultanee"
          hint="Cap di posizioni LIVE contemporanee (non si applica al paper)"
        />
        <div className="mt-4 max-w-xs">
          <Field
            label="Max open positions (live)"
            hint="Limite hard sull'apertura di nuove posizioni live"
            value={local.maxPositions}
            step={1}
            onChange={(v) => setLocal((s) => ({ ...s, maxPositions: v }))}
          />
        </div>
      </Card>

      {dirty && (
        <Card className="sticky bottom-4 z-10 flex items-center gap-3 border-warn/40 bg-warn/10 px-5 py-3">
          <span className="text-sm text-warn">Modifiche non salvate</span>
          <div className="flex-1" />
          <Button size="sm" onClick={handleSave} disabled={isPending}>
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {isPending ? "Salvataggio..." : "Salva risk controls"}
          </Button>
        </Card>
      )}
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
}: {
  label: string;
  hint: string;
  value: number;
  step: number;
  onChange: (v: number) => void;
  suffix?: string;
}) {
  return (
    <div>
      <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</Label>
      <div className="mt-2 flex items-center gap-2">
        <Input
          type="number"
          value={value}
          step={step}
          onChange={(e) => onChange(Number(e.target.value))}
          className="font-mono"
        />
        {suffix && <span className="font-mono text-sm text-fg-3">{suffix}</span>}
      </div>
      <div className="mt-1.5 text-[11px] text-fg-3">{hint}</div>
    </div>
  );
}

function SectionTitle({ title, hint }: { title: string; hint?: string }) {
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</div>
      {hint && <div className="mt-0.5 text-xs text-fg-3">{hint}</div>}
    </div>
  );
}

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

const BTC_PROT_TF_OPTIONS = ["1m", "3m", "5m", "15m", "30m"];

function toNum(v: unknown, fallback: number) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}
function toBool(v: unknown, fallback = false) {
  if (v == null) return fallback;
  return Boolean(v);
}

export function PositionTab({ settings }: Props) {
  const pm = (settings.position_management ?? {}) as Record<string, unknown>;
  const tp = (pm.takeProfit ?? {}) as Record<string, unknown>;
  const ts = (pm.trailingStop ?? {}) as Record<string, unknown>;
  const btc = (settings.btc_macro_protection ?? {}) as Record<string, unknown>;

  const persisted = {
    // Take profit
    tpEnabled: toBool(tp.enabled, true),
    tp1R: toNum(tp.tp1R, 1.0),
    tp1ClosePct: toNum(tp.tp1ClosePct, 30),
    tp2R: toNum(tp.tp2R, 2.0),
    tp2ClosePct: toNum(tp.tp2ClosePct, 30),
    runnerPct: toNum(tp.runnerPct, 40),
    // Trailing stop
    trailingEnabled: toBool(ts.enabled, true),
    activationR: toNum(ts.activationR, 1.0),
    atrMultInit: toNum(ts.atrMultiplierInitial, 3.0),
    atrMultTp1: toNum(ts.atrMultiplierAfterTp1, 2.5),
    atrMultTp2: toNum(ts.atrMultiplierAfterTp2, 2.0),
    moveBeR: toNum(ts.moveToBreakevenAtR, 1.0),
    lockProfitR: toNum(ts.lockProfitAtR, 2.0),
    lockProfitStopR: toNum(ts.lockProfitStopR, 1.0),
    // BTC macro protection
    btcEnabled: toBool(btc.enabled, false),
    btcTimeframe: String(btc.timeframe ?? "5m"),
    btcLookback: toNum(btc.lookback_candles, 6),
    btcPctThreshold: toNum(btc.reversal_pct_threshold, 1.5),
    btcVolMultiplier: toNum(btc.volume_multiplier, 1.2),
    btcBufferPct: toNum(btc.breakeven_buffer_pct, 0.3),
    btcLossExit: toBool(btc.loss_exit_enabled, true),
  };

  const [local, setLocal] = useState(persisted);
  const { mutate: updateSettings, isPending } = useUpdateSettings();

  useEffect(() => {
    setLocal(persisted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(persisted)]);

  const dirty = JSON.stringify(local) !== JSON.stringify(persisted);

  // Validazione sanity: TP1 + TP2 + Runner = 100%
  const tpTotalPct = local.tp1ClosePct + local.tp2ClosePct + local.runnerPct;
  const tpInvalid = local.tpEnabled && Math.abs(tpTotalPct - 100) > 0.1;

  function handleSave() {
    if (tpInvalid) return;
    updateSettings({
      position_management: {
        value: {
          ...pm,
          takeProfit: {
            ...tp,
            enabled: local.tpEnabled,
            tp1R: local.tp1R,
            tp1ClosePct: local.tp1ClosePct,
            tp2R: local.tp2R,
            tp2ClosePct: local.tp2ClosePct,
            runnerPct: local.runnerPct,
          },
          trailingStop: {
            ...ts,
            enabled: local.trailingEnabled,
            activationR: local.activationR,
            atrMultiplierInitial: local.atrMultInit,
            atrMultiplierAfterTp1: local.atrMultTp1,
            atrMultiplierAfterTp2: local.atrMultTp2,
            moveToBreakevenAtR: local.moveBeR,
            lockProfitAtR: local.lockProfitR,
            lockProfitStopR: local.lockProfitStopR,
          },
        },
      },
      btc_macro_protection: {
        value: {
          ...btc,
          enabled: local.btcEnabled,
          timeframe: local.btcTimeframe,
          lookback_candles: local.btcLookback,
          reversal_pct_threshold: local.btcPctThreshold,
          volume_multiplier: local.btcVolMultiplier,
          breakeven_buffer_pct: local.btcBufferPct,
          loss_exit_enabled: local.btcLossExit,
        },
      },
    } as Partial<AppSettings>);
  }

  return (
    <div className="space-y-5">
      {/* Take profit ladder */}
      <Card className="p-6">
        <SectionHeader
          title="Take profit ladder"
          hint="Chiusure parziali a TP1 / TP2 e runner che lascia la coda al trailing"
        >
          <Switch
            checked={local.tpEnabled}
            onCheckedChange={(v: boolean) => setLocal((s) => ({ ...s, tpEnabled: v }))}
          />
        </SectionHeader>

        <div className={cn("mt-4 grid grid-cols-1 gap-4 md:grid-cols-3", !local.tpEnabled && "opacity-50")}>
          <PairField
            title="TP1"
            rLabel="A R"
            rValue={local.tp1R}
            rStep={0.1}
            onRChange={(v) => setLocal((s) => ({ ...s, tp1R: v }))}
            pctValue={local.tp1ClosePct}
            onPctChange={(v) => setLocal((s) => ({ ...s, tp1ClosePct: v }))}
            disabled={!local.tpEnabled}
          />
          <PairField
            title="TP2"
            rLabel="A R"
            rValue={local.tp2R}
            rStep={0.1}
            onRChange={(v) => setLocal((s) => ({ ...s, tp2R: v }))}
            pctValue={local.tp2ClosePct}
            onPctChange={(v) => setLocal((s) => ({ ...s, tp2ClosePct: v }))}
            disabled={!local.tpEnabled}
          />
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Runner %
            </Label>
            <div className="mt-2 flex items-center gap-2">
              <Input
                type="number"
                value={local.runnerPct}
                step={5}
                disabled={!local.tpEnabled}
                onChange={(e) => setLocal((s) => ({ ...s, runnerPct: Number(e.target.value) }))}
                className="font-mono"
              />
              <span className="font-mono text-sm text-fg-3">%</span>
            </div>
            <div className="mt-1.5 text-[11px] text-fg-3">
              Coda lasciata al trailing dopo TP1 + TP2
            </div>
          </div>
        </div>

        {local.tpEnabled && (
          <div
            className={cn(
              "mt-4 flex items-center justify-between rounded-md border px-3 py-2 text-[11px]",
              tpInvalid
                ? "border-danger/40 bg-danger/10 text-danger"
                : "border-success/30 bg-success/10 text-success",
            )}
          >
            <span>TP1 + TP2 + Runner</span>
            <span className="font-mono tabular-nums">
              {tpTotalPct.toFixed(0)}% {tpInvalid ? "≠ 100%" : "= 100%"}
            </span>
          </div>
        )}
      </Card>

      {/* Trailing stop */}
      <Card className="p-6">
        <SectionHeader
          title="Trailing stop"
          hint="Aggiorna lo stop in base ad ATR mentre la posizione corre"
        >
          <Switch
            checked={local.trailingEnabled}
            onCheckedChange={(v: boolean) => setLocal((s) => ({ ...s, trailingEnabled: v }))}
          />
        </SectionHeader>

        <div className={cn("mt-4 space-y-4", !local.trailingEnabled && "opacity-50")}>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <Field
              label="Activation R"
              hint="A quante R di profitto si attiva il trailing"
              value={local.activationR}
              step={0.1}
              suffix="R"
              disabled={!local.trailingEnabled}
              onChange={(v) => setLocal((s) => ({ ...s, activationR: v }))}
            />
            <Field
              label="Break-even at R"
              hint="A quante R lo stop viene mosso a break-even"
              value={local.moveBeR}
              step={0.1}
              suffix="R"
              disabled={!local.trailingEnabled}
              onChange={(v) => setLocal((s) => ({ ...s, moveBeR: v }))}
            />
            <div />
          </div>

          <div>
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              ATR multipliers
            </div>
            <div className="mt-2 grid grid-cols-1 gap-4 md:grid-cols-3">
              <Field
                label="Iniziale"
                hint="ATR × N prima di TP1"
                value={local.atrMultInit}
                step={0.1}
                suffix="x"
                disabled={!local.trailingEnabled}
                onChange={(v) => setLocal((s) => ({ ...s, atrMultInit: v }))}
              />
              <Field
                label="Dopo TP1"
                hint="ATR × N dopo TP1 (piu' stretto)"
                value={local.atrMultTp1}
                step={0.1}
                suffix="x"
                disabled={!local.trailingEnabled}
                onChange={(v) => setLocal((s) => ({ ...s, atrMultTp1: v }))}
              />
              <Field
                label="Dopo TP2"
                hint="ATR × N dopo TP2"
                value={local.atrMultTp2}
                step={0.1}
                suffix="x"
                disabled={!local.trailingEnabled}
                onChange={(v) => setLocal((s) => ({ ...s, atrMultTp2: v }))}
              />
            </div>
          </div>

          <div>
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Lock-in profit
            </div>
            <div className="mt-2 grid grid-cols-1 gap-4 md:grid-cols-2">
              <Field
                label="Lock at R"
                hint="A quante R viene attivato il lock-in profit"
                value={local.lockProfitR}
                step={0.1}
                suffix="R"
                disabled={!local.trailingEnabled}
                onChange={(v) => setLocal((s) => ({ ...s, lockProfitR: v }))}
              />
              <Field
                label="Lock stop at R"
                hint="Posiziona lo stop a questa R (profitto bloccato)"
                value={local.lockProfitStopR}
                step={0.1}
                suffix="R"
                disabled={!local.trailingEnabled}
                onChange={(v) => setLocal((s) => ({ ...s, lockProfitStopR: v }))}
              />
            </div>
          </div>
        </div>
      </Card>

      {/* BTC macro protection */}
      <Card className="p-6">
        <SectionHeader
          title="BTC macro protection"
          hint="Chiude / mette in BE se BTC inverte bruscamente con volume sopra media"
        >
          <Switch
            checked={local.btcEnabled}
            onCheckedChange={(v: boolean) => setLocal((s) => ({ ...s, btcEnabled: v }))}
          />
        </SectionHeader>

        <div className={cn("mt-4 space-y-4", !local.btcEnabled && "opacity-50")}>
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Timeframe di rilevamento
            </Label>
            <div className="mt-2 flex flex-wrap gap-2">
              {BTC_PROT_TF_OPTIONS.map((tf) => {
                const active = local.btcTimeframe === tf;
                return (
                  <button
                    key={tf}
                    disabled={!local.btcEnabled}
                    onClick={() => setLocal((s) => ({ ...s, btcTimeframe: tf }))}
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
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            <Field
              label="Lookback (candele)"
              hint="Quante candele guardare indietro per il reversal"
              value={local.btcLookback}
              step={1}
              disabled={!local.btcEnabled}
              onChange={(v) => setLocal((s) => ({ ...s, btcLookback: v }))}
            />
            <Field
              label="Reversal % threshold"
              hint="% di reversal sul lookback per attivare la protezione"
              value={local.btcPctThreshold}
              step={0.1}
              suffix="%"
              disabled={!local.btcEnabled}
              onChange={(v) => setLocal((s) => ({ ...s, btcPctThreshold: v }))}
            />
            <Field
              label="Volume multiplier"
              hint="Volume × N sulla media per confermare"
              value={local.btcVolMultiplier}
              step={0.1}
              suffix="x"
              disabled={!local.btcEnabled}
              onChange={(v) => setLocal((s) => ({ ...s, btcVolMultiplier: v }))}
            />
            <Field
              label="Break-even buffer"
              hint="% sopra entry per posizionare BE quando si attiva"
              value={local.btcBufferPct}
              step={0.05}
              suffix="%"
              disabled={!local.btcEnabled}
              onChange={(v) => setLocal((s) => ({ ...s, btcBufferPct: v }))}
            />
          </div>

          <div className="flex items-start justify-between gap-3 rounded-md border border-border bg-bg-2/40 p-3">
            <div>
              <div className="text-sm text-fg">Loss exit</div>
              <div className="mt-0.5 text-[11px] text-fg-3">
                Se attivo, chiude in perdita le posizioni quando scatta protezione
              </div>
            </div>
            <Switch
              disabled={!local.btcEnabled}
              checked={local.btcLossExit}
              onCheckedChange={(v: boolean) => setLocal((s) => ({ ...s, btcLossExit: v }))}
            />
          </div>
        </div>
      </Card>

      {dirty && (
        <Card className="sticky bottom-4 z-10 flex items-center gap-3 border-warn/40 bg-warn/10 px-5 py-3">
          <span className="text-sm text-warn">
            Modifiche non salvate {tpInvalid && <span className="text-danger">· TP ladder ≠ 100%</span>}
          </span>
          <div className="flex-1" />
          <Button size="sm" onClick={handleSave} disabled={isPending || tpInvalid}>
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {isPending ? "Salvataggio..." : "Salva position config"}
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

function PairField({
  title,
  rLabel,
  rValue,
  rStep,
  onRChange,
  pctValue,
  onPctChange,
  disabled,
}: {
  title: string;
  rLabel: string;
  rValue: number;
  rStep: number;
  onRChange: (v: number) => void;
  pctValue: number;
  onPctChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <div>
      <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
        {title}
      </Label>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <div className="flex items-center gap-1.5">
          <Input
            type="number"
            value={rValue}
            step={rStep}
            disabled={disabled}
            onChange={(e) => onRChange(Number(e.target.value))}
            className="font-mono"
          />
          <span className="font-mono text-xs text-fg-3">R</span>
        </div>
        <div className="flex items-center gap-1.5">
          <Input
            type="number"
            value={pctValue}
            step={5}
            disabled={disabled}
            onChange={(e) => onPctChange(Number(e.target.value))}
            className="font-mono"
          />
          <span className="font-mono text-xs text-fg-3">%</span>
        </div>
      </div>
      <div className="mt-1.5 text-[11px] text-fg-3">
        {rLabel} {rValue.toFixed(1)} chiude {pctValue}% della size
      </div>
    </div>
  );
}

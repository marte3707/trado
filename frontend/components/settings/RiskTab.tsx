"use client";

import { useEffect, useState } from "react";
import { Save } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { useUpdateSettings } from "@/lib/hooks/mutations";
import type { AppSettings } from "@/lib/api";

interface Props {
  settings: AppSettings;
}

function toNum(v: unknown, fallback: number) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function toBool(v: unknown, fallback: boolean) {
  return typeof v === "boolean" ? v : fallback;
}

function toRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function toCsv(v: unknown, fallback: string[]) {
  return Array.isArray(v) ? v.map(String).join(", ") : fallback.join(", ");
}

function fromCsv(v: string) {
  return v
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function RiskTab({ settings }: Props) {
  const risk = (settings.risk ?? {}) as Record<string, unknown>;
  const portfolioGuard = toRecord(risk.portfolio_guard);
  const maxNewBySide = toRecord(portfolioGuard.max_new_positions_per_cycle_by_side);
  const maxDirectionalOpen = toRecord(portfolioGuard.max_directional_open_positions);
  const maxDirectionalMarkets = toRecord(portfolioGuard.max_directional_markets);
  const maxDirectionalRisk = toRecord(portfolioGuard.max_directional_risk_r);
  const lowTfDirectional = toRecord(portfolioGuard.max_low_tf_directional_positions);
  const lowTfDirectionalOpen = toRecord(portfolioGuard.max_low_tf_directional_open_positions);
  const circuitBreaker = toRecord(risk.circuit_breaker);
  const directionalReversalBreaker = toRecord(risk.directional_reversal_breaker);
  const directionalReversalModes = toRecord(directionalReversalBreaker.modes);

  const persisted = {
    minScore: toNum(risk.min_signal_score, 60),
    minRR: toNum(risk.min_risk_reward, 2),
    minVolume: toNum(risk.min_average_quote_volume_usd, 50_000),
    maxRiskPct: toNum(risk.max_risk_per_trade_pct, 1),
    maxDailyLossPct: toNum(risk.max_daily_loss_pct, 3),
    maxPositions: toNum(risk.max_open_positions, 5),
    maxExposurePct: toNum(risk.max_total_exposure_pct, 25),
    maxLeverage: toNum(risk.max_leverage, 3),
    signalGeneration: {
      participationRatio: toNum(risk.participation_ratio, 0.35),
      edgePenaltiesEnabled: toBool(risk.edge_penalties_enabled, true),
      maxCombinedScoreAdjustment: toNum(risk.max_combined_score_adjustment, 15),
      edgePenaltyMinMarketTrades: toNum(risk.edge_penalty_min_market_trades, 20),
      minSignalExpectedR: toNum(risk.min_signal_expected_r, 0),
    },
    portfolioGuard: {
      enabled: toBool(portfolioGuard.enabled, true),
      maxNewPerCycleLive: toNum(maxNewBySide.live, 3),
      maxDirectionalOpenPaper: toNum(maxDirectionalOpen.paper, 25),
      maxDirectionalOpenLive: toNum(maxDirectionalOpen.live, 13),
      maxDirectionalMarketsLive: toNum(maxDirectionalMarkets.live, 10),
      maxDirectionalRiskPaper: toNum(maxDirectionalRisk.paper, 25),
      maxDirectionalRiskLive: toNum(maxDirectionalRisk.live, 8),
      lowTfTimeframes: toCsv(lowTfDirectional.timeframes, ["5m", "15m"]),
      lowTfMaxPaper: toNum(lowTfDirectional.paper, 25),
      lowTfMaxLive: toNum(lowTfDirectional.live, 13),
      lowTfOpenTimeframes: toCsv(lowTfDirectionalOpen.apply_timeframes, ["5m", "15m"]),
      lowTfMaxOpenLive: toNum(lowTfDirectionalOpen.live, 8),
    },
    circuitBreaker: {
      enabled: toBool(circuitBreaker.enabled, true),
      windowMinutes: toNum(circuitBreaker.window_minutes, 90),
      maxLossR: toNum(circuitBreaker.max_loss_r, 5),
      maxStopLosses: toNum(circuitBreaker.max_stop_losses, 8),
      pauseMinutes: toNum(circuitBreaker.pause_minutes, 180),
    },
    directionalReversalBreaker: {
      enabled: toBool(directionalReversalBreaker.enabled, true),
      liveEnabled: toBool(directionalReversalModes.live, true),
      applyTimeframes: toCsv(directionalReversalBreaker.apply_timeframes, ["5m", "15m"]),
      windowMinutes: toNum(directionalReversalBreaker.window_minutes, 120),
      stopLossCount: toNum(directionalReversalBreaker.stop_loss_count, 4),
      cooldownMinutes: toNum(directionalReversalBreaker.cooldown_minutes, 180),
    },
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
    persisted.portfolioGuard.enabled,
    persisted.portfolioGuard.maxNewPerCycleLive,
    persisted.portfolioGuard.maxDirectionalOpenPaper,
    persisted.portfolioGuard.maxDirectionalOpenLive,
    persisted.portfolioGuard.maxDirectionalMarketsLive,
    persisted.portfolioGuard.maxDirectionalRiskPaper,
    persisted.portfolioGuard.maxDirectionalRiskLive,
    persisted.portfolioGuard.lowTfTimeframes,
    persisted.portfolioGuard.lowTfMaxPaper,
    persisted.portfolioGuard.lowTfMaxLive,
    persisted.portfolioGuard.lowTfOpenTimeframes,
    persisted.portfolioGuard.lowTfMaxOpenLive,
    persisted.circuitBreaker.enabled,
    persisted.circuitBreaker.windowMinutes,
    persisted.circuitBreaker.maxLossR,
    persisted.circuitBreaker.maxStopLosses,
    persisted.circuitBreaker.pauseMinutes,
    persisted.directionalReversalBreaker.enabled,
    persisted.directionalReversalBreaker.liveEnabled,
    persisted.directionalReversalBreaker.applyTimeframes,
    persisted.directionalReversalBreaker.windowMinutes,
    persisted.directionalReversalBreaker.stopLossCount,
    persisted.directionalReversalBreaker.cooldownMinutes,
  ]);

  const dirty = JSON.stringify(local) !== JSON.stringify(persisted);

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
      participation_ratio: local.signalGeneration.participationRatio,
      edge_penalties_enabled: local.signalGeneration.edgePenaltiesEnabled,
      max_combined_score_adjustment: local.signalGeneration.maxCombinedScoreAdjustment,
      edge_penalty_min_market_trades: local.signalGeneration.edgePenaltyMinMarketTrades,
      min_signal_expected_r: local.signalGeneration.minSignalExpectedR,
      portfolio_guard: {
        ...portfolioGuard,
        enabled: local.portfolioGuard.enabled,
        max_new_positions_per_cycle_by_side: {
          ...maxNewBySide,
          live: local.portfolioGuard.maxNewPerCycleLive,
        },
        max_directional_open_positions: {
          ...maxDirectionalOpen,
          paper: local.portfolioGuard.maxDirectionalOpenPaper,
          live: local.portfolioGuard.maxDirectionalOpenLive,
        },
        max_directional_markets: {
          ...maxDirectionalMarkets,
          live: local.portfolioGuard.maxDirectionalMarketsLive,
        },
        max_directional_risk_r: {
          ...maxDirectionalRisk,
          paper: local.portfolioGuard.maxDirectionalRiskPaper,
          live: local.portfolioGuard.maxDirectionalRiskLive,
        },
        max_low_tf_directional_positions: {
          ...lowTfDirectional,
          timeframes: fromCsv(local.portfolioGuard.lowTfTimeframes),
          paper: local.portfolioGuard.lowTfMaxPaper,
          live: local.portfolioGuard.lowTfMaxLive,
        },
        max_low_tf_directional_open_positions: {
          ...lowTfDirectionalOpen,
          apply_timeframes: fromCsv(local.portfolioGuard.lowTfOpenTimeframes),
          live: local.portfolioGuard.lowTfMaxOpenLive,
        },
      },
      circuit_breaker: {
        ...circuitBreaker,
        enabled: local.circuitBreaker.enabled,
        window_minutes: local.circuitBreaker.windowMinutes,
        max_loss_r: local.circuitBreaker.maxLossR,
        max_stop_losses: local.circuitBreaker.maxStopLosses,
        pause_minutes: local.circuitBreaker.pauseMinutes,
        scope: "new_entries_only",
      },
      directional_reversal_breaker: {
        ...directionalReversalBreaker,
        enabled: local.directionalReversalBreaker.enabled,
        modes: {
          ...directionalReversalModes,
          paper: false,
          live: local.directionalReversalBreaker.liveEnabled,
        },
        apply_timeframes: fromCsv(local.directionalReversalBreaker.applyTimeframes),
        window_minutes: local.directionalReversalBreaker.windowMinutes,
        stop_loss_count: local.directionalReversalBreaker.stopLossCount,
        cooldown_minutes: local.directionalReversalBreaker.cooldownMinutes,
      },
    };
    updateSettings({ risk: { value: nextRisk } } as Partial<AppSettings>);
  }

  function setPortfolioGuard(patch: Partial<typeof persisted.portfolioGuard>) {
    setLocal((s) => ({ ...s, portfolioGuard: { ...s.portfolioGuard, ...patch } }));
  }

  function setCircuitBreaker(patch: Partial<typeof persisted.circuitBreaker>) {
    setLocal((s) => ({ ...s, circuitBreaker: { ...s.circuitBreaker, ...patch } }));
  }

  function setDirectionalReversalBreaker(
    patch: Partial<typeof persisted.directionalReversalBreaker>,
  ) {
    setLocal((s) => ({
      ...s,
      directionalReversalBreaker: { ...s.directionalReversalBreaker, ...patch },
    }));
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
        <SectionTitle
          title="Signal generation"
          hint="Leve del filtro score: cambiano quali candidati diventano segnali, non sizing o guardrail live."
        />
        <div className="mt-4 grid grid-cols-1 gap-5 md:grid-cols-2">
          <div>
            <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Participation consensus
            </Label>
            <div className="mt-2 flex items-center gap-4">
              <span className="w-12 font-mono text-2xl font-semibold tabular-nums text-fg">
                {(local.signalGeneration.participationRatio * 100).toFixed(0)}%
              </span>
              <Slider
                value={[local.signalGeneration.participationRatio]}
                min={0.35}
                max={0.8}
                step={0.05}
                onValueChange={(v: number[]) => setLocal((s) => ({
                  ...s,
                  signalGeneration: { ...s.signalGeneration, participationRatio: v[0] },
                }))}
              />
            </div>
            <div className="mt-1.5 text-[11px] text-fg-3">
              Peso minimo delle strategie che deve votare nella stessa direzione. Il minimo UI 35% evita di allentare oltre il baseline conservativo.
            </div>
          </div>
          <div className="space-y-3">
            <ToggleBlock
              label="Edge penalties"
              hint="Applica le penalità direzione/bucket/mercato/timeframe già calcolate. Se sospese, i valori restano salvati ma non alzano la soglia score."
              checked={local.signalGeneration.edgePenaltiesEnabled}
              onChange={(edgePenaltiesEnabled) => setLocal((s) => ({
                ...s,
                signalGeneration: { ...s.signalGeneration, edgePenaltiesEnabled },
              }))}
            />
            <Field
              label="Max edge adjustment"
              hint="Cap totale dei punti che le edge penalties possono aggiungere allo score minimo."
              value={local.signalGeneration.maxCombinedScoreAdjustment}
              step={1}
              onChange={(maxCombinedScoreAdjustment) => setLocal((s) => ({
                ...s,
                signalGeneration: { ...s.signalGeneration, maxCombinedScoreAdjustment: Math.max(0, maxCombinedScoreAdjustment) },
              }))}
            />
            <Field
              label="Min trade per penalty mercato"
              hint="Le penalty automatiche per singolo mercato restano visibili ma non si applicano sotto questa evidenza."
              value={local.signalGeneration.edgePenaltyMinMarketTrades}
              step={1}
              onChange={(edgePenaltyMinMarketTrades) => setLocal((s) => ({
                ...s,
                signalGeneration: { ...s.signalGeneration, edgePenaltyMinMarketTrades: Math.max(1, edgePenaltyMinMarketTrades) },
              }))}
            />
            <Field
              label="Min Expected R"
              hint="Gate empirico dopo score: consente solo bucket con Expected R almeno pari a questa soglia."
              value={local.signalGeneration.minSignalExpectedR}
              step={0.05}
              onChange={(minSignalExpectedR) => setLocal((s) => ({
                ...s,
                signalGeneration: { ...s.signalGeneration, minSignalExpectedR: Math.max(-0.5, Math.min(1, minSignalExpectedR)) },
              }))}
            />
          </div>
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

      <Card className="p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <SectionTitle
            title="Portfolio guard"
            hint="Cap direzionali separati per paper e live prima di nuove aperture"
          />
          <ToggleInline
            label={local.portfolioGuard.enabled ? "Enabled" : "Disabled"}
            checked={local.portfolioGuard.enabled}
            onChange={(enabled) => setPortfolioGuard({ enabled })}
          />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Field
            label="New live/cycle side"
            hint="Max nuove live nella stessa direzione per ciclo worker"
            value={local.portfolioGuard.maxNewPerCycleLive}
            step={1}
            onChange={(v) => setPortfolioGuard({ maxNewPerCycleLive: v })}
          />
          <Field
            label="Directional open paper"
            hint="Max posizioni paper nella stessa direzione"
            value={local.portfolioGuard.maxDirectionalOpenPaper}
            step={1}
            onChange={(v) => setPortfolioGuard({ maxDirectionalOpenPaper: v })}
          />
          <Field
            label="Directional open live"
            hint="Max posizioni live nella stessa direzione"
            value={local.portfolioGuard.maxDirectionalOpenLive}
            step={1}
            onChange={(v) => setPortfolioGuard({ maxDirectionalOpenLive: v })}
          />
          <Field
            label="Directional markets live"
            hint="Max mercati live aperti nella stessa direzione"
            value={local.portfolioGuard.maxDirectionalMarketsLive}
            step={1}
            onChange={(v) => setPortfolioGuard({ maxDirectionalMarketsLive: v })}
          />
          <Field
            label="Directional risk paper"
            hint="Cap R paper nella stessa direzione"
            value={local.portfolioGuard.maxDirectionalRiskPaper}
            step={0.5}
            suffix="R"
            onChange={(v) => setPortfolioGuard({ maxDirectionalRiskPaper: v })}
          />
          <Field
            label="Directional risk live"
            hint="Cap R live nella stessa direzione"
            value={local.portfolioGuard.maxDirectionalRiskLive}
            step={0.5}
            suffix="R"
            onChange={(v) => setPortfolioGuard({ maxDirectionalRiskLive: v })}
          />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-[minmax(0,1fr)_160px_160px]">
          <TextField
            label="Low TF list"
            hint="Timeframe separati da virgola"
            value={local.portfolioGuard.lowTfTimeframes}
            onChange={(v) => setPortfolioGuard({ lowTfTimeframes: v })}
          />
          <Field
            label="Low TF paper"
            hint="Max cluster paper"
            value={local.portfolioGuard.lowTfMaxPaper}
            step={1}
            onChange={(v) => setPortfolioGuard({ lowTfMaxPaper: v })}
          />
          <Field
            label="Low TF live"
            hint="Max cluster live"
            value={local.portfolioGuard.lowTfMaxLive}
            step={1}
            onChange={(v) => setPortfolioGuard({ lowTfMaxLive: v })}
          />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-[minmax(0,1fr)_180px]">
          <TextField
            label="Low TF live list"
            hint="Timeframe live a cui applicare il cap direzionale stretto"
            value={local.portfolioGuard.lowTfOpenTimeframes}
            onChange={(v) => setPortfolioGuard({ lowTfOpenTimeframes: v })}
          />
          <Field
            label="Low TF live open"
            hint="Max live low-TF nella stessa direzione"
            value={local.portfolioGuard.lowTfMaxOpenLive}
            step={1}
            onChange={(v) => setPortfolioGuard({ lowTfMaxOpenLive: v })}
          />
        </div>
      </Card>

      <Card className="p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <SectionTitle
            title="Circuit breaker"
            hint="Pausa nuove aperture dopo sequenze recenti di stop/loss"
          />
          <ToggleInline
            label={local.circuitBreaker.enabled ? "Enabled" : "Disabled"}
            checked={local.circuitBreaker.enabled}
            onChange={(enabled) => setCircuitBreaker({ enabled })}
          />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Field
            label="Window"
            hint="Finestra di osservazione"
            value={local.circuitBreaker.windowMinutes}
            step={5}
            suffix="min"
            onChange={(v) => setCircuitBreaker({ windowMinutes: v })}
          />
          <Field
            label="Max loss"
            hint="Perdita R che attiva la pausa"
            value={local.circuitBreaker.maxLossR}
            step={0.5}
            suffix="R"
            onChange={(v) => setCircuitBreaker({ maxLossR: v })}
          />
          <Field
            label="Max stop losses"
            hint="Numero stop recenti consentiti"
            value={local.circuitBreaker.maxStopLosses}
            step={1}
            onChange={(v) => setCircuitBreaker({ maxStopLosses: v })}
          />
          <Field
            label="Pause"
            hint="Durata blocco nuove entrate"
            value={local.circuitBreaker.pauseMinutes}
            step={15}
            suffix="min"
            onChange={(v) => setCircuitBreaker({ pauseMinutes: v })}
          />
        </div>
      </Card>

      <Card className="p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <SectionTitle
            title="Directional reversal breaker"
            hint="Blocca nuove live low-TF same-side dopo cluster di stop loss"
          />
          <ToggleInline
            label={local.directionalReversalBreaker.enabled ? "Enabled" : "Disabled"}
            checked={local.directionalReversalBreaker.enabled}
            onChange={(enabled) => setDirectionalReversalBreaker({ enabled })}
          />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-5">
          <ToggleBlock
            label="Live mode"
            hint="Applica il breaker alle nuove entrate live"
            checked={local.directionalReversalBreaker.liveEnabled}
            onChange={(liveEnabled) => setDirectionalReversalBreaker({ liveEnabled })}
          />
          <TextField
            label="Apply TF"
            hint="Timeframe separati da virgola"
            value={local.directionalReversalBreaker.applyTimeframes}
            onChange={(applyTimeframes) => setDirectionalReversalBreaker({ applyTimeframes })}
          />
          <Field
            label="Window"
            hint="Finestra stop same-side"
            value={local.directionalReversalBreaker.windowMinutes}
            step={5}
            suffix="min"
            onChange={(windowMinutes) => setDirectionalReversalBreaker({ windowMinutes })}
          />
          <Field
            label="Stop trigger"
            hint="Stop loss necessari"
            value={local.directionalReversalBreaker.stopLossCount}
            step={1}
            onChange={(stopLossCount) => setDirectionalReversalBreaker({ stopLossCount })}
          />
          <Field
            label="Cooldown"
            hint="Durata blocco same-side"
            value={local.directionalReversalBreaker.cooldownMinutes}
            step={15}
            suffix="min"
            onChange={(cooldownMinutes) => setDirectionalReversalBreaker({ cooldownMinutes })}
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

function ToggleInline({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-xs font-medium text-fg-2">
      <span>{label}</span>
      <Switch checked={checked} onCheckedChange={onChange} />
    </label>
  );
}

function ToggleBlock({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-2 px-3 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</Label>
        <Switch checked={checked} onCheckedChange={onChange} />
      </div>
      <div className="mt-1.5 text-[11px] text-fg-3">{hint}</div>
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

function TextField({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <Label className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</Label>
      <div className="mt-2">
        <Input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="font-mono"
        />
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

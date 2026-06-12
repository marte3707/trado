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
  const maxDirectionalOpen = toRecord(portfolioGuard.max_directional_open_positions);
  const maxDirectionalRisk = toRecord(portfolioGuard.max_directional_risk_r);
  const lowTfDirectional = toRecord(portfolioGuard.max_low_tf_directional_positions);
  const circuitBreaker = toRecord(risk.circuit_breaker);
  const macroRiskMode = toRecord(risk.macro_risk_mode);
  const macroRiskMonitor = toRecord(risk.macro_risk_monitor);
  const macroRiskState = toRecord(settings.macro_risk_state);

  const persisted = {
    minScore: toNum(risk.min_signal_score, 60),
    minRR: toNum(risk.min_risk_reward, 2),
    minVolume: toNum(risk.min_average_quote_volume_usd, 50_000),
    maxRiskPct: toNum(risk.max_risk_per_trade_pct, 1),
    maxDailyLossPct: toNum(risk.max_daily_loss_pct, 3),
    maxPositions: toNum(risk.max_open_positions, 5),
    maxExposurePct: toNum(risk.max_total_exposure_pct, 25),
    maxLeverage: toNum(risk.max_leverage, 3),
    portfolioGuard: {
      enabled: toBool(portfolioGuard.enabled, true),
      maxDirectionalOpenPaper: toNum(maxDirectionalOpen.paper, 20),
      maxDirectionalOpenLive: toNum(maxDirectionalOpen.live, 12),
      maxDirectionalRiskPaper: toNum(maxDirectionalRisk.paper, 12),
      maxDirectionalRiskLive: toNum(maxDirectionalRisk.live, 6),
      lowTfTimeframes: toCsv(lowTfDirectional.timeframes, ["5m", "15m"]),
      lowTfMaxPaper: toNum(lowTfDirectional.paper, 12),
      lowTfMaxLive: toNum(lowTfDirectional.live, 6),
    },
    circuitBreaker: {
      enabled: toBool(circuitBreaker.enabled, true),
      windowMinutes: toNum(circuitBreaker.window_minutes, 90),
      maxLossR: toNum(circuitBreaker.max_loss_r, 5),
      maxStopLosses: toNum(circuitBreaker.max_stop_losses, 8),
      pauseMinutes: toNum(circuitBreaker.pause_minutes, 180),
    },
    macroRiskMode: {
      enabled: toBool(macroRiskMode.enabled, false),
      sizeMultiplier: toNum(macroRiskMode.size_multiplier, 0.5),
      minExpectedR: toNum(macroRiskMode.min_expected_r, 0),
      disabledTimeframes: toCsv(macroRiskMode.disable_timeframes, ["5m"]),
      requireMtfConfluence: toBool(macroRiskMode.require_mtf_confluence, true),
    },
    macroRiskMonitor: {
      enabled: toBool(macroRiskMonitor.enabled, true),
      actionsEnabled: toBool(macroRiskMonitor.actions_enabled, true),
      telegramNotifyCooldownMinutes: toNum(
        macroRiskMonitor.telegram_notify_cooldown_minutes,
        180,
      ),
      telegramNotifyOnDowngrade: toBool(
        macroRiskMonitor.telegram_notify_on_downgrade,
        false,
      ),
      newsEnabled: toBool(macroRiskMonitor.news_enabled, true),
      calendarEnabled: toBool(macroRiskMonitor.calendar_enabled, true),
      marketStressEnabled: toBool(macroRiskMonitor.market_stress_enabled, true),
      newsFeedUrls: toCsv(
        macroRiskMonitor.news_feed_urls,
        [
          "https://feeds.bbci.co.uk/news/world/rss.xml",
          "https://www.theguardian.com/world/rss",
          "https://www.aljazeera.com/xml/rss/all.xml",
          "https://www.coindesk.com/arc/outboundfeeds/rss/",
        ],
      ),
      calendarFeedUrls: toCsv(
        macroRiskMonitor.calendar_feed_urls,
        [],
      ),
      newsFetchCooldownMinutes: toNum(macroRiskMonitor.news_fetch_cooldown_minutes, 15),
      calendarFetchCooldownHours: toNum(macroRiskMonitor.calendar_fetch_cooldown_hours, 6),
      newsLookbackHours: toNum(macroRiskMonitor.news_lookback_hours, 12),
      calendarLookaheadHours: toNum(macroRiskMonitor.calendar_lookahead_hours, 48),
      calendarPreEventMinutes: toNum(macroRiskMonitor.calendar_pre_event_minutes, 90),
      calendarPostEventMinutes: toNum(macroRiskMonitor.calendar_post_event_minutes, 45),
      marketLookbackMinutes: toNum(macroRiskMonitor.market_lookback_minutes, 120),
      watchScore: toNum(macroRiskMonitor.watch_score, 40),
      highScore: toNum(macroRiskMonitor.high_score, 60),
      extremeScore: toNum(macroRiskMonitor.extreme_score, 80),
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
    persisted.portfolioGuard.maxDirectionalOpenPaper,
    persisted.portfolioGuard.maxDirectionalOpenLive,
    persisted.portfolioGuard.maxDirectionalRiskPaper,
    persisted.portfolioGuard.maxDirectionalRiskLive,
    persisted.portfolioGuard.lowTfTimeframes,
    persisted.portfolioGuard.lowTfMaxPaper,
    persisted.portfolioGuard.lowTfMaxLive,
    persisted.circuitBreaker.enabled,
    persisted.circuitBreaker.windowMinutes,
    persisted.circuitBreaker.maxLossR,
    persisted.circuitBreaker.maxStopLosses,
    persisted.circuitBreaker.pauseMinutes,
    persisted.macroRiskMode.enabled,
    persisted.macroRiskMode.sizeMultiplier,
    persisted.macroRiskMode.minExpectedR,
    persisted.macroRiskMode.disabledTimeframes,
    persisted.macroRiskMode.requireMtfConfluence,
    persisted.macroRiskMonitor.enabled,
    persisted.macroRiskMonitor.actionsEnabled,
    persisted.macroRiskMonitor.telegramNotifyCooldownMinutes,
    persisted.macroRiskMonitor.telegramNotifyOnDowngrade,
    persisted.macroRiskMonitor.newsEnabled,
    persisted.macroRiskMonitor.calendarEnabled,
    persisted.macroRiskMonitor.marketStressEnabled,
    persisted.macroRiskMonitor.newsFeedUrls,
    persisted.macroRiskMonitor.calendarFeedUrls,
    persisted.macroRiskMonitor.newsFetchCooldownMinutes,
    persisted.macroRiskMonitor.calendarFetchCooldownHours,
    persisted.macroRiskMonitor.newsLookbackHours,
    persisted.macroRiskMonitor.calendarLookaheadHours,
    persisted.macroRiskMonitor.calendarPreEventMinutes,
    persisted.macroRiskMonitor.calendarPostEventMinutes,
    persisted.macroRiskMonitor.marketLookbackMinutes,
    persisted.macroRiskMonitor.watchScore,
    persisted.macroRiskMonitor.highScore,
    persisted.macroRiskMonitor.extremeScore,
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
      portfolio_guard: {
        enabled: local.portfolioGuard.enabled,
        max_directional_open_positions: {
          paper: local.portfolioGuard.maxDirectionalOpenPaper,
          live: local.portfolioGuard.maxDirectionalOpenLive,
        },
        max_directional_risk_r: {
          paper: local.portfolioGuard.maxDirectionalRiskPaper,
          live: local.portfolioGuard.maxDirectionalRiskLive,
        },
        max_low_tf_directional_positions: {
          timeframes: fromCsv(local.portfolioGuard.lowTfTimeframes),
          paper: local.portfolioGuard.lowTfMaxPaper,
          live: local.portfolioGuard.lowTfMaxLive,
        },
      },
      circuit_breaker: {
        enabled: local.circuitBreaker.enabled,
        window_minutes: local.circuitBreaker.windowMinutes,
        max_loss_r: local.circuitBreaker.maxLossR,
        max_stop_losses: local.circuitBreaker.maxStopLosses,
        pause_minutes: local.circuitBreaker.pauseMinutes,
        scope: "new_entries_only",
      },
      macro_risk_mode: {
        enabled: local.macroRiskMode.enabled,
        size_multiplier: local.macroRiskMode.sizeMultiplier,
        min_score_add: 0,
        min_expected_r: local.macroRiskMode.minExpectedR,
        disable_timeframes: fromCsv(local.macroRiskMode.disabledTimeframes),
        require_mtf_confluence: local.macroRiskMode.requireMtfConfluence,
      },
      macro_risk_monitor: {
        ...macroRiskMonitor,
        enabled: local.macroRiskMonitor.enabled,
        actions_enabled: local.macroRiskMonitor.actionsEnabled,
        telegram_notify_cooldown_minutes: local.macroRiskMonitor.telegramNotifyCooldownMinutes,
        telegram_notify_on_downgrade: local.macroRiskMonitor.telegramNotifyOnDowngrade,
        news_enabled: local.macroRiskMonitor.newsEnabled,
        calendar_enabled: local.macroRiskMonitor.calendarEnabled,
        market_stress_enabled: local.macroRiskMonitor.marketStressEnabled,
        news_feed_urls: fromCsv(local.macroRiskMonitor.newsFeedUrls),
        calendar_feed_urls: fromCsv(local.macroRiskMonitor.calendarFeedUrls),
        news_fetch_cooldown_minutes: local.macroRiskMonitor.newsFetchCooldownMinutes,
        calendar_fetch_cooldown_hours: local.macroRiskMonitor.calendarFetchCooldownHours,
        news_lookback_hours: local.macroRiskMonitor.newsLookbackHours,
        calendar_lookahead_hours: local.macroRiskMonitor.calendarLookaheadHours,
        calendar_pre_event_minutes: local.macroRiskMonitor.calendarPreEventMinutes,
        calendar_post_event_minutes: local.macroRiskMonitor.calendarPostEventMinutes,
        market_lookback_minutes: local.macroRiskMonitor.marketLookbackMinutes,
        watch_score: local.macroRiskMonitor.watchScore,
        high_score: local.macroRiskMonitor.highScore,
        extreme_score: local.macroRiskMonitor.extremeScore,
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

  function setMacroRiskMode(patch: Partial<typeof persisted.macroRiskMode>) {
    setLocal((s) => ({ ...s, macroRiskMode: { ...s.macroRiskMode, ...patch } }));
  }

  function setMacroRiskMonitor(patch: Partial<typeof persisted.macroRiskMonitor>) {
    setLocal((s) => ({ ...s, macroRiskMonitor: { ...s.macroRiskMonitor, ...patch } }));
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
            title="Macro risk mode"
            hint="Override manuale difensivo per shock news-driven"
          />
          <ToggleInline
            label={local.macroRiskMode.enabled ? "Enabled" : "Disabled"}
            checked={local.macroRiskMode.enabled}
            onChange={(enabled) => setMacroRiskMode({ enabled })}
          />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Field
            label="Size multiplier"
            hint="Moltiplicatore rischio per trade"
            value={local.macroRiskMode.sizeMultiplier}
            step={0.05}
            suffix="x"
            onChange={(v) => setMacroRiskMode({ sizeMultiplier: v })}
          />
          <Field
            label="Expected R floor"
            hint="Blocca solo setup con expectancy empirica negativa quando la calibrazione è affidabile"
            value={local.macroRiskMode.minExpectedR}
            step={0.05}
            suffix="R"
            onChange={(v) => setMacroRiskMode({ minExpectedR: v })}
          />
          <TextField
            label="Disabled TF"
            hint="Timeframe separati da virgola"
            value={local.macroRiskMode.disabledTimeframes}
            onChange={(v) => setMacroRiskMode({ disabledTimeframes: v })}
          />
          <ToggleBlock
            label="Require MTF"
            hint="Richiede conferma multi-timeframe"
            checked={local.macroRiskMode.requireMtfConfluence}
            onChange={(requireMtfConfluence) => setMacroRiskMode({ requireMtfConfluence })}
          />
        </div>
      </Card>

      <Card className="p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <SectionTitle
            title="Auto macro risk monitor"
            hint="News + stress BTC/ETH aggiornano automaticamente lo stato macro"
          />
          <ToggleInline
            label={local.macroRiskMonitor.enabled ? "Enabled" : "Disabled"}
            checked={local.macroRiskMonitor.enabled}
            onChange={(enabled) => setMacroRiskMonitor({ enabled })}
          />
        </div>

        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-5">
          <StatePill label="Level" value={String(macroRiskState.level ?? "off").toUpperCase()} />
          <StatePill label="Score" value={`${macroRiskState.combined_score ?? 0}/100`} />
          <StatePill label="News" value={`${macroRiskState.news_score ?? 0}/100`} />
          <StatePill label="Calendar" value={`${macroRiskState.calendar_score ?? 0}/100`} />
          <StatePill label="Market" value={`${macroRiskState.market_stress_score ?? 0}/100`} />
        </div>
        <div className="mt-3 rounded-lg border border-border bg-bg-2 px-3 py-2.5 text-xs text-fg-3">
          <div className="text-fg-2">{String(macroRiskState.summary ?? "Macro risk normal")}</div>
          <div className="mt-1 font-mono text-[11px]">
            active_until: {String(macroRiskState.active_until ?? "n/a")}
          </div>
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <ToggleBlock
            label="Auto actions"
            hint="Applica policy a nuove aperture"
            checked={local.macroRiskMonitor.actionsEnabled}
            onChange={(actionsEnabled) => setMacroRiskMonitor({ actionsEnabled })}
          />
          <Field
            label="Telegram cooldown"
            hint="Minuti minimi fra alert attivi ripetuti"
            value={local.macroRiskMonitor.telegramNotifyCooldownMinutes}
            step={15}
            suffix="min"
            onChange={(telegramNotifyCooldownMinutes) =>
              setMacroRiskMonitor({ telegramNotifyCooldownMinutes })
            }
          />
          <ToggleBlock
            label="Notify downgrade"
            hint="Invia alert anche quando il livello scende"
            checked={local.macroRiskMonitor.telegramNotifyOnDowngrade}
            onChange={(telegramNotifyOnDowngrade) =>
              setMacroRiskMonitor({ telegramNotifyOnDowngrade })
            }
          />
          <ToggleBlock
            label="News feed"
            hint="Usa headline RSS per scoring"
            checked={local.macroRiskMonitor.newsEnabled}
            onChange={(newsEnabled) => setMacroRiskMonitor({ newsEnabled })}
          />
          <ToggleBlock
            label="Macro calendar"
            hint="FOMC, CPI, NFP, PCE e altri eventi programmati"
            checked={local.macroRiskMonitor.calendarEnabled}
            onChange={(calendarEnabled) => setMacroRiskMonitor({ calendarEnabled })}
          />
          <ToggleBlock
            label="Market stress"
            hint="Usa stress BTC/ETH da candle locali"
            checked={local.macroRiskMonitor.marketStressEnabled}
            onChange={(marketStressEnabled) => setMacroRiskMonitor({ marketStressEnabled })}
          />
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Field
            label="News cooldown"
            hint="Minuti fra fetch RSS"
            value={local.macroRiskMonitor.newsFetchCooldownMinutes}
            step={1}
            suffix="min"
            onChange={(v) => setMacroRiskMonitor({ newsFetchCooldownMinutes: v })}
          />
          <Field
            label="Calendar cooldown"
            hint="Ore fra fetch calendario"
            value={local.macroRiskMonitor.calendarFetchCooldownHours}
            step={1}
            suffix="h"
            onChange={(v) => setMacroRiskMonitor({ calendarFetchCooldownHours: v })}
          />
          <Field
            label="Before event"
            hint="Minuti difensivi prima evento"
            value={local.macroRiskMonitor.calendarPreEventMinutes}
            step={5}
            suffix="min"
            onChange={(v) => setMacroRiskMonitor({ calendarPreEventMinutes: v })}
          />
          <Field
            label="After event"
            hint="Minuti difensivi dopo evento"
            value={local.macroRiskMonitor.calendarPostEventMinutes}
            step={5}
            suffix="min"
            onChange={(v) => setMacroRiskMonitor({ calendarPostEventMinutes: v })}
          />
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-6">
          <Field
            label="Watch"
            hint="Score stato watch"
            value={local.macroRiskMonitor.watchScore}
            step={1}
            onChange={(v) => setMacroRiskMonitor({ watchScore: v })}
          />
          <Field
            label="High"
            hint="Score stato high"
            value={local.macroRiskMonitor.highScore}
            step={1}
            onChange={(v) => setMacroRiskMonitor({ highScore: v })}
          />
          <Field
            label="Extreme"
            hint="Score stato extreme"
            value={local.macroRiskMonitor.extremeScore}
            step={1}
            onChange={(v) => setMacroRiskMonitor({ extremeScore: v })}
          />
          <Field
            label="News lookback"
            hint="Ore headline rilevanti"
            value={local.macroRiskMonitor.newsLookbackHours}
            step={1}
            suffix="h"
            onChange={(v) => setMacroRiskMonitor({ newsLookbackHours: v })}
          />
          <Field
            label="Calendar lookahead"
            hint="Ore eventi monitorati"
            value={local.macroRiskMonitor.calendarLookaheadHours}
            step={1}
            suffix="h"
            onChange={(v) => setMacroRiskMonitor({ calendarLookaheadHours: v })}
          />
          <Field
            label="Market lookback"
            hint="Minuti candle stress"
            value={local.macroRiskMonitor.marketLookbackMinutes}
            step={5}
            suffix="min"
            onChange={(v) => setMacroRiskMonitor({ marketLookbackMinutes: v })}
          />
        </div>

        <div className="mt-4">
          <TextField
            label="News feeds"
            hint="URL RSS separati da virgola"
            value={local.macroRiskMonitor.newsFeedUrls}
            onChange={(newsFeedUrls) => setMacroRiskMonitor({ newsFeedUrls })}
          />
        </div>
        <div className="mt-4">
          <TextField
            label="Calendar feeds"
            hint="URL ICS opzionali separati da virgola"
            value={local.macroRiskMonitor.calendarFeedUrls}
            onChange={(calendarFeedUrls) => setMacroRiskMonitor({ calendarFeedUrls })}
          />
          <div className="mt-1.5 text-[11px] text-fg-3">
            Vuoto = usiamo solo il calendario statico interno (FOMC, PCE, GDP, Treasury). I feed
            ICS esterni sono opzionali.
          </div>
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

function StatePill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-bg-2 px-3 py-2.5">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div className="mt-1 truncate font-mono text-sm font-semibold text-fg">{value}</div>
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

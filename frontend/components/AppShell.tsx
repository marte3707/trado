"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Sidebar, Topbar, Route, TradingMode } from "@/components/Shell";
import { TweaksPanel, TweakState, TWEAK_DEFAULTS } from "@/components/TweaksPanel";
import { DashboardData } from "@/components/Dashboard";
import { DashboardScreen } from "@/components/screens/DashboardScreen";
import { SignalsScreen } from "@/components/screens/SignalsScreen";
import { CoinScreen } from "@/components/screens/CoinScreen";
import { AccountMonitorScreen } from "@/components/screens/AccountMonitorScreen";
import { StrategiesScreen } from "@/components/screens/StrategiesScreen";
import { CalibrationScreen } from "@/components/screens/CalibrationScreen";
import { HistoryScreen } from "@/components/screens/HistoryScreen";
import { AlertsScreen } from "@/components/screens/AlertsScreen";
import { SettingsScreen } from "@/components/screens/SettingsScreen";
import {
  apiGet, apiPatch, apiPost,
  Health, Market, Signal, Position, Order, AppSettings, Strategy, DecisionLog, MarketIntelligence,
  Calibration, CalibrationHistoryEntry, AutoTunerStatus, WorkerCycle, SignalRejections
} from "@/lib/api";
import { hydrateOpenPositionSourceSignals } from "@/lib/signalHydration";

interface AppShellProps {
  initialData: DashboardData;
}

function envToMode(val: string | undefined): TradingMode {
  if (val === "PAPER_TRADING") return "paper";
  if (val === "AUTO_TRADE") return "live";
  return "signal";
}

function healthToMode(health: Health | null | undefined): TradingMode {
  return envToMode(health?.mode ?? process.env.NEXT_PUBLIC_OPERATION_MODE);
}

function asNumber(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function tweaksFromSettings(settings: AppSettings): TweakState {
  return {
    ...TWEAK_DEFAULTS,
    scoreThreshold: asNumber(settings.risk?.min_signal_score) ?? TWEAK_DEFAULTS.scoreThreshold,
  };
}

function alertCount(logs: DecisionLog[]): number {
  return logs.filter((log) => {
    const eventType = log.event_type.toLowerCase();
    return eventType.includes("error") || eventType.includes("fail") || eventType.includes("warn") || eventType.includes("kill");
  }).length;
}

function marketKey(market: Market) {
  return `${market.market_type}:${market.symbol}`;
}

export function AppShell({ initialData }: AppShellProps) {
  /* ── shell state ─────────────────────────────────────────────────────── */
  const [route, setRoute] = useState<Route>("dash");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const [showTweaks, setShowTweaks] = useState(false);
  const [tweaks, setTweaks] = useState<TweakState>(() => tweaksFromSettings(initialData.settings));

  /* ── data state ──────────────────────────────────────────────────────── */
  const [health, setHealth] = useState(initialData.health);
  const [markets, setMarkets] = useState(initialData.markets);
  const [chartMarkets, setChartMarkets] = useState(initialData.chartMarkets);
  const [signals, setSignals] = useState(initialData.signals);
  const [positions, setPositions] = useState(initialData.positions);
  const [orders, setOrders] = useState(initialData.orders);
  const [settings, setSettings] = useState(initialData.settings);
  const [strategies, setStrategies] = useState(initialData.strategies);
  const [logs, setLogs] = useState(initialData.logs);
  const [calibration, setCalibration] = useState<Calibration>(initialData.calibration);
  const [calibrationHistory, setCalibrationHistory] = useState<CalibrationHistoryEntry[]>(initialData.calibrationHistory);
  const [tunerStatus, setTunerStatus] = useState<AutoTunerStatus | null>(initialData.tunerStatus ?? null);
  const [workerCycles, setWorkerCycles] = useState<WorkerCycle[]>(initialData.workerCycles ?? []);
  const [signalRejections, setSignalRejections] = useState<SignalRejections>(initialData.signalRejections ?? { total: 0, by_reason: {}, recent: [] });
  const [marketIntelligence, setMarketIntelligence] = useState<MarketIntelligence>(
    initialData.marketIntelligence
  );
  const [candles] = useState(initialData.candles);
  const [chartMarket] = useState(initialData.chartMarket);
  const [chartTimeframe] = useState(initialData.chartTimeframe);
  const [coinFocus, setCoinFocus] = useState<{ marketKey: string; timeframe: string } | null>(() =>
    initialData.chartMarket
      ? { marketKey: marketKey(initialData.chartMarket), timeframe: initialData.chartTimeframe }
      : null
  );

  /* ── derived ─────────────────────────────────────────────────────────── */
  const kill = Boolean(settings?.kill_switch?.enabled);
  const newSignals = signals.filter(s => s.status === "new").length;
  const mode = healthToMode(health);

  async function setKill(on: boolean) {
    try {
      await apiPost<unknown>(on ? "/api/kill-switch/enable" : "/api/kill-switch/disable");
      await refresh();
    } catch (e) {
      console.error("kill switch failed", e);
    }
  }

  /* ── refresh ─────────────────────────────────────────────────────────── */
  const refresh = useCallback(async () => {
    try {
      const [h, m, cm, s, p, o, st, str, l, cal, calHistory, tuner, cycles, rejections, mi] = await Promise.all([
        apiGet<Health>("/api/health"),
        apiGet<Market[]>("/api/markets"),
        apiGet<Market[]>("/api/markets?active=true&has_candles=true"),
        apiGet<Signal[]>("/api/signals?min_score=0"),
        apiGet<Position[]>("/api/positions"),
        apiGet<Order[]>("/api/orders"),
        apiGet<AppSettings>("/api/settings"),
        apiGet<Strategy[]>("/api/strategies"),
        apiGet<DecisionLog[]>("/api/logs").catch(() => logs),
        apiGet<Calibration>("/api/analytics/calibration"),
        apiGet<CalibrationHistoryEntry[]>("/api/analytics/calibration/history"),
        apiGet<AutoTunerStatus>("/api/auto-tuner/status").catch(() => null),
        apiGet<WorkerCycle[]>("/api/analytics/worker-cycles?limit=20").catch(() => []),
        apiGet<SignalRejections>("/api/analytics/signal-rejections?limit=50").catch(() => ({ total: 0, by_reason: {}, recent: [] })),
        apiGet<MarketIntelligence>("/api/market-intelligence").catch(() => marketIntelligence),
      ]);
      const hydratedSignals = await hydrateOpenPositionSourceSignals(s, p);
      setHealth(h);
      setMarkets(m);
      setChartMarkets(cm);
      setSignals(hydratedSignals);
      setPositions(p);
      setOrders(o);
      setSettings(st);
      setStrategies(str);
      setLogs(l);
      setCalibration(cal);
      setCalibrationHistory(calHistory);
      setTunerStatus(tuner);
      setWorkerCycles(cycles);
      setSignalRejections(rejections);
      setMarketIntelligence(mi);
    } catch (e) {
      console.error("refresh failed", e);
    }
  }, [logs, marketIntelligence]);

  /* ── auto-poll every 30s ─────────────────────────────────────────────── */
  useEffect(() => {
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  /* ── apply tweaks to :root ───────────────────────────────────────────── */
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-accent", tweaks.accent);
    root.setAttribute("data-density", tweaks.density);
  }, [tweaks.accent, tweaks.density]);

  function setTweak<K extends keyof TweakState>(key: K, value: TweakState[K]) {
    setTweaks(prev => ({ ...prev, [key]: value }));
  }

  async function saveScoreThreshold(threshold: number) {
    setTweak("scoreThreshold", threshold);
    const nextRisk = { ...(settings.risk ?? {}), min_signal_score: threshold };
    try {
      await apiPatch("/api/settings", { risk: { value: nextRisk } });
      await refresh();
    } catch (e) {
      console.error("score threshold save failed", e);
    }
  }

  function openSignalChart(signal: Signal) {
    if (!signal.market) return;
    setCoinFocus({ marketKey: marketKey(signal.market), timeframe: signal.timeframe });
    setRoute("coin");
  }

  function openPositionChart(position: Position) {
    if (!position.market) return;
    const sourceId = position.metadata_json?.source_signal_id;
    const linked = sourceId ? signals.find(s => s.id === Number(sourceId)) : null;
    const tf = linked?.timeframe ?? signals.find(s => s.market && marketKey(s.market) === marketKey(position.market!))?.timeframe ?? "";
    setCoinFocus({ marketKey: marketKey(position.market), timeframe: tf });
    setRoute("coin");
  }

  /* ── ws live check ───────────────────────────────────────────────────── */
  const wsLive = health?.status === "ok";

  /* ── render ──────────────────────────────────────────────────────────── */
  return (
    <div className={"app " + (sidebarCollapsed ? "sidebar-collapsed" : "")}>
      <Sidebar
        route={route}
        setRoute={setRoute}
        counts={{ signals: newSignals, alerts: alertCount(logs) }}
        wsLive={wsLive}
        health={health}
        collapsed={sidebarCollapsed}
        onToggleCollapsed={() => setSidebarCollapsed(v => !v)}
      />

      <Topbar
        route={route}
        mode={mode}
        kill={kill}
        setKill={setKill}
        onToggleTweaks={() => setShowTweaks(v => !v)}
      />

      <div className="main">
          {route === "dash" && (
            <DashboardScreen
              health={health}
              signals={signals}
              positions={positions}
              strategies={strategies}
              logs={logs}
              settings={settings}
              marketIntelligence={marketIntelligence}
              threshold={tweaks.scoreThreshold}
              onRefresh={refresh}
              onOpenPosition={openPositionChart}
            />
          )}

          {route === "signals" && (
            <SignalsScreen
              signals={signals}
              strategies={strategies}
              markets={markets}
              health={health}
              threshold={tweaks.scoreThreshold}
              onRefresh={refresh}
              onOpenChart={openSignalChart}
            />
          )}

          {route === "coin" && (
            <CoinScreen
              markets={markets}
              signals={signals}
              positions={positions}
              health={health}
              chartMarkets={chartMarkets}
              initialCandles={candles}
              initialMarket={chartMarket}
              initialTf={chartTimeframe}
              focusMarketKey={coinFocus?.marketKey}
              focusTf={coinFocus?.timeframe}
            />
          )}

          {route === "account" && (
            <AccountMonitorScreen health={health} />
          )}

          {route === "strategies" && (
              <StrategiesScreen
                strategies={strategies}
                settings={settings}
                threshold={tweaks.scoreThreshold}
              onThresholdChange={saveScoreThreshold}
              onRefresh={refresh}
            />
          )}

          {route === "calibration" && (
            <CalibrationScreen
              calibration={calibration}
              history={calibrationHistory}
              settings={settings}
              tunerStatus={tunerStatus}
              workerCycles={workerCycles}
              signalRejections={signalRejections}
              onRefresh={refresh}
            />
          )}

          {route === "history" && (
            <HistoryScreen
              positions={positions}
              signals={signals}
              orders={orders}
            />
          )}

          {route === "alerts" && (
            <AlertsScreen logs={logs} markets={markets} settings={settings} onRefresh={refresh} />
          )}

          {route === "settings" && (
            <SettingsScreen
              settings={settings}
              health={health}
              onRefresh={refresh}
            />
          )}
      </div>

      <TweaksPanel
        open={showTweaks}
        onClose={() => setShowTweaks(false)}
        tweaks={tweaks}
        onChange={setTweak}
      />
    </div>
  );
}

"use client";

import { useCallback, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

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
import { StrategyAnalysisScreen } from "@/components/screens/StrategyAnalysisScreen";

import { Sidebar } from "@/components/shell/Sidebar";
import { Topbar } from "@/components/shell/Topbar";
import { CommandMenu } from "@/components/shell/CommandMenu";
import { HelpModal } from "@/components/shell/HelpModal";
import type { Route } from "@/components/shell/routes";
import type { TradingMode } from "@/components/shell/ModeBadge";
import { buildNavBindings, useChordHotkey } from "@/lib/hotkeys";

import {
  useCalibration,
  useCalibrationHistory,
  useDecisionLogs,
  useHealth,
  useMarketIntelligence,
  useMarkets,
  useOrders,
  usePositions,
  useSettings,
  useSignalRejections,
  useSignals,
  useStrategies,
  useTunerStatus,
  useWorkerCycles,
} from "@/lib/hooks/queries";
import { hydrateOpenPositionSourceSignals } from "@/lib/signalHydration";
import type {
  AppSettings,
  Market,
  Position,
  Signal,
} from "@/lib/api";

interface AppShellProps {
  initialData: DashboardData;
}

function envToMode(val: string | undefined): TradingMode {
  if (val === "PAPER_TRADING") return "paper";
  if (val === "AUTO_TRADE") return "live";
  return "signal";
}

function asNumber(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function alertCount(logs: { event_type: string }[]): number {
  return logs.filter((log) => {
    const t = log.event_type.toLowerCase();
    return t.includes("error") || t.includes("fail") || t.includes("warn") || t.includes("kill");
  }).length;
}

function marketKey(market: Market) {
  return `${market.market_type}:${market.symbol}`;
}

export function AppShell({ initialData }: AppShellProps) {
  const [route, setRoute] = useState<Route>("dash");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [coinFocus, setCoinFocus] = useState<{ marketKey: string; timeframe: string } | null>(() =>
    initialData.chartMarket
      ? { marketKey: marketKey(initialData.chartMarket), timeframe: initialData.chartTimeframe }
      : null,
  );
  const [hydratedSignals, setHydratedSignals] = useState<Signal[]>(initialData.signals);

  const queryClient = useQueryClient();

  /* ── live queries (poll handled by TanStack staleTime + refetchInterval) ── */
  const { data: health = initialData.health } = useHealth({ initialData: initialData.health });
  const { data: markets = initialData.markets } = useMarkets("", { initialData: initialData.markets });
  const { data: signalsRaw = initialData.signals } = useSignals("min_score=0", {
    initialData: initialData.signals,
  });
  const { data: positions = initialData.positions } = usePositions({ initialData: initialData.positions });
  const { data: orders = initialData.orders } = useOrders({ initialData: initialData.orders });
  const { data: settings = initialData.settings } = useSettings({ initialData: initialData.settings });
  const { data: strategies = initialData.strategies } = useStrategies({ initialData: initialData.strategies });
  const { data: logs = initialData.logs } = useDecisionLogs({ initialData: initialData.logs });
  const { data: calibration = initialData.calibration } = useCalibration({ initialData: initialData.calibration });
  const { data: calibrationHistory = initialData.calibrationHistory } = useCalibrationHistory({
    initialData: initialData.calibrationHistory,
  });
  const { data: tunerStatus = initialData.tunerStatus } = useTunerStatus({
    initialData: initialData.tunerStatus ?? undefined,
  });
  const { data: workerCycles = initialData.workerCycles } = useWorkerCycles(20, {
    initialData: initialData.workerCycles,
  });
  const { data: signalRejections = initialData.signalRejections } = useSignalRejections(50, {
    initialData: initialData.signalRejections,
  });
  const { data: marketIntelligence = initialData.marketIntelligence } = useMarketIntelligence({
    initialData: initialData.marketIntelligence,
  });

  /* ── hydrate signals con open positions (ricalcolato quando signals/positions cambiano) ── */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const hydrated = await hydrateOpenPositionSourceSignals(signalsRaw, positions);
      if (!cancelled) setHydratedSignals(hydrated);
    })();
    return () => {
      cancelled = true;
    };
  }, [signalsRaw, positions]);

  /* ── derived ── */
  const mode = envToMode(health?.mode ?? process.env.NEXT_PUBLIC_OPERATION_MODE);
  const newSignalsCount = hydratedSignals.filter((s) => s.status === "new").length;
  const alertsCount = alertCount(logs ?? []);
  const wsLive = health?.status === "ok";

  const settingsObj = (settings ?? {}) as AppSettings;
  const threshold = asNumber(settingsObj.risk?.min_signal_score) ?? 60;

  /* ── refresh callback (legacy interface per gli screens non ancora migrati) ── */
  const refresh = useCallback(() => {
    queryClient.invalidateQueries();
  }, [queryClient]);

  /* ── chart navigation helpers ── */
  const openMarket = useCallback((market: Market, timeframe?: string) => {
    setCoinFocus({ marketKey: marketKey(market), timeframe: timeframe ?? "" });
    setRoute("coin");
  }, []);

  const openSignalChart = useCallback(
    (signal: Signal) => {
      if (!signal.market) return;
      openMarket(signal.market, signal.timeframe);
    },
    [openMarket],
  );

  const openPositionChart = useCallback(
    (position: Position) => {
      if (!position.market) return;
      const sourceId = position.metadata_json?.source_signal_id;
      const linked = sourceId ? hydratedSignals.find((s) => s.id === Number(sourceId)) : null;
      const tf =
        linked?.timeframe ??
        hydratedSignals.find((s) => s.market && marketKey(s.market) === marketKey(position.market!))?.timeframe ??
        "";
      openMarket(position.market, tf);
    },
    [hydratedSignals, openMarket],
  );

  /* ── nav hotkeys (g + tasto) ── */
  useChordHotkey("g", buildNavBindings(setRoute));

  /* ── command menu ── */
  const [searchOpen, setSearchOpen] = useState(false);
  // Mount the command menu always so it can listen Cmd+K globally;
  // `onOpenSearch` opens it imperatively via a custom keyboard event.
  useEffect(() => {
    if (!searchOpen) return;
    // Dispatch synthetic event to trigger CommandMenu's internal toggle.
    const evt = new KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true });
    window.dispatchEvent(evt);
    setSearchOpen(false);
  }, [searchOpen]);

  /* ── help modal (?) ── */
  const [helpOpen, setHelpOpen] = useState(false);
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "?" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (t instanceof HTMLElement && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      setHelpOpen((v) => !v);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div
      className="grid h-screen overflow-hidden"
      style={{
        gridTemplateColumns: `${sidebarCollapsed ? "56px" : "240px"} 1fr`,
        gridTemplateRows: "56px 1fr",
        gridTemplateAreas: '"side top" "side main"',
        transition: "grid-template-columns 0.2s ease-out",
      }}
    >
      <Sidebar
        route={route}
        setRoute={setRoute}
        counts={{ signals: newSignalsCount, alerts: alertsCount }}
        collapsed={sidebarCollapsed}
        onToggleCollapsed={() => setSidebarCollapsed((v) => !v)}
      />

      <Topbar
        route={route}
        mode={mode}
        wsLive={wsLive}
        onOpenSearch={() => setSearchOpen(true)}
        onOpenHelp={() => setHelpOpen(true)}
      />

      <main className="main overflow-auto bg-bg" style={{ gridArea: "main" }}>
        {route === "dash" && (
          <DashboardScreen
            health={health}
            signals={hydratedSignals}
            positions={positions}
            strategies={strategies}
            logs={logs}
            settings={settingsObj}
            marketIntelligence={marketIntelligence}
            threshold={threshold}
            onRefresh={refresh}
            onOpenPosition={openPositionChart}
            onOpenSignal={openSignalChart}
          />
        )}

        {route === "signals" && (
          <SignalsScreen
            signals={hydratedSignals}
            strategies={strategies}
            markets={markets}
            health={health}
            threshold={threshold}
            onRefresh={refresh}
            onOpenChart={openSignalChart}
          />
        )}

        {route === "coin" && (
          <CoinScreen
            markets={markets}
            signals={hydratedSignals}
            positions={positions}
            health={health}
            chartMarkets={initialData.chartMarkets}
            initialCandles={initialData.candles}
            initialMarket={initialData.chartMarket}
            initialTf={initialData.chartTimeframe}
            focusMarketKey={coinFocus?.marketKey}
            focusTf={coinFocus?.timeframe}
          />
        )}

        {route === "account" && <AccountMonitorScreen health={health} />}

        {route === "strategies" && (
          <StrategiesScreen
            strategies={strategies}
            settings={settingsObj}
            threshold={threshold}
            onThresholdChange={() => {
              /* threshold ora gestito via ThresholdMenu nella topbar */
            }}
            onRefresh={refresh}
          />
        )}

        {route === "calibration" && (
          <CalibrationScreen
            calibration={calibration}
            history={calibrationHistory}
            settings={settingsObj}
            tunerStatus={tunerStatus ?? null}
            workerCycles={workerCycles}
            signalRejections={signalRejections}
            onRefresh={refresh}
          />
        )}

        {route === "history" && <HistoryScreen positions={positions} orders={orders} />}

        {route === "strategy-lab" && (
          <StrategyAnalysisScreen
            signals={hydratedSignals}
            positions={positions}
            orders={orders}
            strategies={strategies}
          />
        )}

        {route === "alerts" && (
          <AlertsScreen
            logs={logs}
            markets={markets}
            settings={settingsObj}
            onRefresh={refresh}
            onOpenSignal={openSignalChart}
            onOpenPosition={openPositionChart}
            onOpenMarket={openMarket}
          />
        )}

        {route === "settings" && (
          <SettingsScreen settings={settingsObj} health={health} onRefresh={refresh} />
        )}
      </main>

      <CommandMenu setRoute={setRoute} onOpenMarket={openMarket} />
      <HelpModal open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}

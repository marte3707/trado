"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import type { DashboardData } from "@/components/dashboard/helpers";
import { DashboardScreen } from "@/components/screens/DashboardScreen";
import { SignalsScreen } from "@/components/screens/SignalsScreen";
import { CoinScreen } from "@/components/screens/CoinScreen";
import { AccountMonitorScreen } from "@/components/screens/AccountMonitorScreen";
import { StrategiesScreen } from "@/components/screens/StrategiesScreen";
import { CalibrationScreen } from "@/components/screens/CalibrationScreen";
import { PerformanceScreen } from "@/components/screens/PerformanceScreen";
import { LeaderboardAlphaScreen } from "@/components/screens/LeaderboardAlphaScreen";
import { HistoryScreen } from "@/components/screens/HistoryScreen";
import { FunnelScreen } from "@/components/screens/FunnelScreen";
import { AlertsScreen } from "@/components/screens/AlertsScreen";
import { SettingsScreen } from "@/components/screens/SettingsScreen";
import { StrategyAnalysisScreen } from "@/components/screens/StrategyAnalysisScreen";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Sidebar } from "@/components/shell/Sidebar";
import { Topbar } from "@/components/shell/Topbar";
import { CommandMenu } from "@/components/shell/CommandMenu";
import { HelpModal } from "@/components/shell/HelpModal";
import type { Route } from "@/components/shell/routes";
import type { TradingMode } from "@/components/shell/ModeBadge";
import { buildNavBindings, useChordHotkey } from "@/lib/hotkeys";
import { unrealizedPnl } from "@/lib/pnl";
import { collectNewLiveAlerts, liveAlertToastContent } from "@/components/alerts/alertHelpers";

import {
  useAccountMonitor,
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
  useWorkerCycles,
} from "@/lib/hooks/queries";
import { hydrateOpenPositionSourceSignals } from "@/lib/signalHydration";
import type {
  AccountPosition,
  AppSettings,
  Market,
  Position,
  Signal,
} from "@/lib/api";

interface AppShellProps {
  initialData: DashboardData;
}

type PnlTitleDirection = "up" | "down" | "flat" | null;

type TradePnlCounts = {
  positive: number;
  negative: number;
};

type RealtimeTitleState = {
  pnl: number | null;
  counts: TradePnlCounts | null;
};

const LIVE_ALERT_DISMISS_ALL_THRESHOLD = 3;

function envToMode(val: string | undefined): TradingMode {
  if (val === "PAPER_TRADING") return "paper";
  if (val === "AUTO_TRADE") return "live";
  return "signal";
}

function asNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function fmtBrowserTitlePnl(value: number): string {
  const abs = Math.abs(value);
  const digits = abs < 1 ? 3 : 2;
  const formatted = abs.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `-${formatted}`;
  return formatted;
}

function hyperliquidWsUrl(network: string | null | undefined): string {
  return network === "mainnet"
    ? "wss://api.hyperliquid.xyz/ws"
    : "wss://api.hyperliquid-testnet.xyz/ws";
}

function positionRealtimePnl(position: AccountPosition, mids: Record<string, number>): number | null {
  const size = asNumber(position.size);
  if (size == null || size <= 0) return asNumber(position.unrealized_pnl);
  const mid = mids[position.coin ?? ""];
  return (
    unrealizedPnl({
      entryPrice: position.entry_price,
      size,
      markPrice: mid,
      side: position.side ?? "",
    }) ?? asNumber(position.unrealized_pnl)
  );
}

function accountRealtimePnl(positions: AccountPosition[], mids: Record<string, number>): number | null {
  if (positions.length === 0) return null;
  let total = 0;
  let hasPnl = false;
  for (const position of positions) {
    const pnl = positionRealtimePnl(position, mids);
    if (pnl != null) {
      total += pnl;
      hasPnl = true;
    }
  }
  return hasPnl ? total : null;
}

function accountTradePnlCounts(positions: AccountPosition[], mids: Record<string, number>): TradePnlCounts {
  let positive = 0;
  let negative = 0;
  for (const position of positions) {
    const pnl = positionRealtimePnl(position, mids);
    if (pnl == null || Math.abs(pnl) < 0.000001) continue;
    if (pnl > 0) positive += 1;
    else negative += 1;
  }
  return { positive, negative };
}

function sameTradePnlCounts(a: TradePnlCounts | null, b: TradePnlCounts | null): boolean {
  if (a == null || b == null) return a == null && b == null;
  return a?.positive === b.positive && a.negative === b.negative;
}

function sameRealtimeTitleState(a: RealtimeTitleState, b: RealtimeTitleState): boolean {
  const samePnl =
    a.pnl === b.pnl ||
    (a.pnl != null && b.pnl != null && Math.abs(a.pnl - b.pnl) < 0.000001);
  return samePnl && sameTradePnlCounts(a.counts, b.counts);
}

function accountRealtimeTitleState(
  positions: AccountPosition[],
  mids: Record<string, number>,
  fallbackPnl: number | null,
): RealtimeTitleState {
  if (positions.length === 0) return { pnl: fallbackPnl, counts: null };
  return {
    pnl: accountRealtimePnl(positions, mids) ?? fallbackPnl,
    counts: accountTradePnlCounts(positions, mids),
  };
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
  const [pnlTitleDirection, setPnlTitleDirection] = useState<PnlTitleDirection>(null);
  const [realtimeTitle, setRealtimeTitle] = useState<RealtimeTitleState>({ pnl: null, counts: null });
  const originalTitleRef = useRef<string | null>(null);
  const titlePnlIdentityRef = useRef<string | null>(null);
  const previousTitlePnlRef = useRef<number | null>(null);
  const seenLiveAlertLogIdsRef = useRef<Set<number> | null>(null);
  const activeLiveToastIdsRef = useRef<Set<number>>(new Set());
  const accountPositionsRef = useRef<AccountPosition[]>([]);
  const snapshotUnrealizedRef = useRef<number | null>(null);
  const titleMidsRef = useRef<Record<string, number>>({});
  const [coinFocus, setCoinFocus] = useState<{ signalId?: number; marketKey: string; timeframe: string } | null>(() =>
    initialData.chartMarket
      ? { marketKey: marketKey(initialData.chartMarket), timeframe: initialData.chartTimeframe }
      : null,
  );
  const [hydratedSignals, setHydratedSignals] = useState<Signal[]>(initialData.signals);

  const queryClient = useQueryClient();

  /* ── live queries (poll handled by TanStack staleTime + refetchInterval) ── */
  const { data: health = initialData.health } = useHealth({ initialData: initialData.health });
  const { data: accountSnapshot } = useAccountMonitor();
  const { data: markets = initialData.markets } = useMarkets("", { initialData: initialData.markets });
  const { data: signalsRaw = initialData.signals } = useSignals("min_score=0", {
    initialData: initialData.signals,
  });
  const { data: positions = initialData.positions } = usePositions({ initialData: initialData.positions });
  const { data: orders = initialData.orders } = useOrders({ initialData: initialData.orders });
  const { data: settings = initialData.settings } = useSettings({ initialData: initialData.settings });
  const { data: strategies = initialData.strategies } = useStrategies({ initialData: initialData.strategies });
  const { data: logs = initialData.logs } = useDecisionLogs({
    initialData: initialData.logs,
    refetchInterval: 5_000,
    refetchIntervalInBackground: true,
    refetchOnMount: "always",
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
  const marketSymbolById = useMemo(() => {
    const entries = markets.map((market) => [market.id, market.symbol] as const);
    return new Map(entries);
  }, [markets]);

  const settingsObj = (settings ?? {}) as AppSettings;
  const threshold = asNumber(settingsObj.risk?.min_signal_score) ?? 60;
  const snapshotUnrealized = useMemo(
    () => accountSnapshot?.positions.reduce((sum, p) => sum + (asNumber(p.unrealized_pnl) ?? 0), 0) ?? null,
    [accountSnapshot],
  );
  const pnlTitleIdentity = useMemo(() => {
    const network = health?.network ?? "unknown";
    const accountKey = accountSnapshot?.account.address ?? "unconfigured";
    return `trado:account-monitor:unrealized-pnl:${network}:${accountKey}`;
  }, [accountSnapshot?.account.address, health?.network]);
  const titlePnl = realtimeTitle.pnl ?? snapshotUnrealized;
  const pnlTitleText = useMemo(
    () => (titlePnl == null ? null : fmtBrowserTitlePnl(titlePnl)),
    [titlePnl],
  );
  const pnlTitleCountsText = realtimeTitle.counts == null
    ? null
    : `${realtimeTitle.counts.positive}▲ ${realtimeTitle.counts.negative}▼`;

  useEffect(() => {
    const seenIds = seenLiveAlertLogIdsRef.current;
    if (seenIds == null) {
      seenLiveAlertLogIdsRef.current = new Set((logs ?? []).map((log) => log.id));
      return;
    }

    const alerts = collectNewLiveAlerts(logs ?? [], seenIds);
    const showDismissAll =
      activeLiveToastIdsRef.current.size + alerts.length >= LIVE_ALERT_DISMISS_ALL_THRESHOLD;
    for (const alert of alerts) {
      const { title, description } = liveAlertToastContent(alert, {
        marketSymbol: alert.marketId == null ? null : marketSymbolById.get(alert.marketId),
      });
      const duration = alert.level === "risk" ? 12_000 : 8_000;
      const options = {
        id: `live-alert-${alert.id}`,
        description,
        duration,
        action: showDismissAll
          ? {
              label: "Chiudi tutte",
              onClick: () => {
                activeLiveToastIdsRef.current.clear();
                toast.dismiss();
              },
            }
          : undefined,
      };
      activeLiveToastIdsRef.current.add(alert.id);
      window.setTimeout(() => {
        activeLiveToastIdsRef.current.delete(alert.id);
      }, duration + 1_000);

      if (alert.level === "risk") {
        toast.error(title, options);
      } else if (alert.level === "good" || alert.level === "strong") {
        toast.success(title, options);
      } else {
        toast(title, options);
      }
    }
  }, [logs, marketSymbolById]);

  useEffect(() => {
    snapshotUnrealizedRef.current = snapshotUnrealized;
    accountPositionsRef.current = accountSnapshot?.positions ?? [];
    const nextTitle = accountRealtimeTitleState(accountPositionsRef.current, titleMidsRef.current, snapshotUnrealized);
    setRealtimeTitle((current) => (sameRealtimeTitleState(current, nextTitle) ? current : nextTitle));
  }, [accountSnapshot?.positions, snapshotUnrealized]);

  useEffect(() => {
    if (titlePnlIdentityRef.current !== pnlTitleIdentity) {
      titlePnlIdentityRef.current = pnlTitleIdentity;
      previousTitlePnlRef.current = null;
      setPnlTitleDirection(null);
    }
    if (titlePnl == null) {
      previousTitlePnlRef.current = null;
      setPnlTitleDirection(null);
      return;
    }
    const previous = previousTitlePnlRef.current;
    previousTitlePnlRef.current = titlePnl;
    if (previous == null || Math.abs(titlePnl - previous) < 0.000001) {
      if (previous == null) setPnlTitleDirection(null);
      else setPnlTitleDirection("flat");
    } else if (titlePnl > previous) {
      setPnlTitleDirection("up");
    } else {
      setPnlTitleDirection("down");
    }
  }, [pnlTitleIdentity, titlePnl]);

  useEffect(() => {
    const socket = new WebSocket(hyperliquidWsUrl(health?.network));
    let active = true;

    socket.addEventListener("open", () => {
      if (!active) return;
      socket.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids" } }));
    });
    socket.addEventListener("message", (event) => {
      if (!active) return;
      try {
        const data = JSON.parse(event.data as string) as { data?: { mids?: Record<string, string> } };
        if (!data?.data?.mids) return;
        const nextMids = { ...titleMidsRef.current };
        for (const [coin, price] of Object.entries(data.data.mids)) {
          const mid = Number(price);
          if (Number.isFinite(mid)) nextMids[coin] = mid;
        }
        titleMidsRef.current = nextMids;
        const nextTitle = accountRealtimeTitleState(accountPositionsRef.current, nextMids, snapshotUnrealizedRef.current);
        setRealtimeTitle((current) => (sameRealtimeTitleState(current, nextTitle) ? current : nextTitle));
      } catch {
        // Ignore malformed WS frames; the polled snapshot remains the fallback.
      }
    });

    return () => {
      active = false;
      socket.close();
    };
  }, [health?.network]);

  useEffect(() => {
    if (originalTitleRef.current == null) {
      originalTitleRef.current = document.title;
    }
    return () => {
      document.title = originalTitleRef.current ?? "Trado";
    };
  }, []);

  useEffect(() => {
    if (pnlTitleText == null) {
      document.title = originalTitleRef.current ?? "Trado";
      return;
    }
    const arrow =
      pnlTitleDirection === "up"
        ? "▲"
        : pnlTitleDirection === "down"
          ? "▼"
          : "";
    const tradeCounts = pnlTitleCountsText ? ` ${pnlTitleCountsText}` : "";
    document.title = arrow ? `${pnlTitleText} ${arrow} Trado${tradeCounts}` : `${pnlTitleText} Trado${tradeCounts}`;
  }, [pnlTitleCountsText, pnlTitleDirection, pnlTitleText]);

  /* ── refresh callback (legacy interface per gli screens non ancora migrati) ── */
  const refresh = useCallback(() => {
    queryClient.invalidateQueries();
  }, [queryClient]);

  /* ── chart navigation helpers ── */
  const openMarket = useCallback((market: Market, timeframe?: string, signalId?: number) => {
    setCoinFocus({ signalId, marketKey: marketKey(market), timeframe: timeframe ?? "" });
    setRoute("coin");
  }, []);

  const openSignalChart = useCallback(
    (signal: Signal) => {
      if (!signal.market) return;
      openMarket(signal.market, signal.timeframe, signal.id);
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
      openMarket(position.market, tf, linked?.id);
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
            focusSignalId={coinFocus?.signalId}
            focusMarketKey={coinFocus?.marketKey}
            focusTf={coinFocus?.timeframe}
          />
        )}

        {route === "account" && (
          <Tabs defaultValue="live" className="w-full">
            <TabsList className="mx-7 mt-7">
              <TabsTrigger value="live">Live</TabsTrigger>
              <TabsTrigger value="history">Storico</TabsTrigger>
            </TabsList>
            <TabsContent value="live">
              <AccountMonitorScreen health={health} />
            </TabsContent>
            <TabsContent value="history">
              <HistoryScreen fallbackPositions={positions} fallbackOrders={orders} />
            </TabsContent>
          </Tabs>
        )}

        {route === "strategies" && (
          <Tabs defaultValue="config" className="w-full">
            <TabsList className="mx-7 mt-7">
              <TabsTrigger value="config">Configurazione</TabsTrigger>
              <TabsTrigger value="analysis">Analisi</TabsTrigger>
            </TabsList>
            <TabsContent value="config">
              <StrategiesScreen
                strategies={strategies}
                settings={settingsObj}
                threshold={threshold}
                onThresholdChange={() => {
                  /* threshold ora gestito via ThresholdMenu nella topbar */
                }}
                onRefresh={refresh}
              />
            </TabsContent>
            <TabsContent value="analysis">
              <StrategyAnalysisScreen
                signals={hydratedSignals}
                positions={positions}
                orders={orders}
                strategies={strategies}
              />
            </TabsContent>
          </Tabs>
        )}

        {route === "calibration" && (
          <CalibrationScreen
            settings={settingsObj}
            workerCycles={workerCycles}
            signalRejections={signalRejections}
            onRefresh={refresh}
          />
        )}

        {route === "performance" && <PerformanceScreen health={health} />}

        {route === "leaderboard" && <LeaderboardAlphaScreen />}

        {route === "funnel" && <FunnelScreen />}

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

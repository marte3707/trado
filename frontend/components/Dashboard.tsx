"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bell,
  CheckCircle2,
  Database,
  Info,
  LayoutDashboard,
  ListChecks,
  RefreshCcw,
  Shield,
  Target,
  X,
  XCircle
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ChartPanel, ChartOverlays } from "@/components/ChartPanel";
import {
  AppSettings,
  Calibration,
  DecisionLog,
  Health,
  Market,
  Order,
  Position,
  Signal,
  Strategy,
  apiGet,
  apiPost
} from "@/lib/api";


import {
  DashboardData,
  DashboardToast,
  DashboardView,
  Filters,
  ICON_BTN,
  INPUT_CLS,
  SELECT_CLS,
  SIGNAL_SORT_DEFAULTS,
  SignalSort,
  SignalSortKey,
  TableKey,
  TablePageState,
  asNumber,
  buildLogToast,
  buildSignalsPath,
  compareSignals,
  isClosedSignal,
  marketKey,
  marketLabel,
  parseError,
  positionSourceSignalId,
  signalSearchText,
} from "@/components/dashboard/helpers";
import type { Candle } from "@/components/dashboard/helpers";
import { PerformanceView } from "@/components/dashboard/PerformanceView";
import { SignalsSection } from "@/components/dashboard/SignalsSection";
import { SystemView } from "@/components/dashboard/SystemView";
import { TradingView } from "@/components/dashboard/TradingView";

// Re-export per i consumer esistenti (AppShell importa DashboardData da qui).
export type { DashboardData } from "@/components/dashboard/helpers";

export function Dashboard({ initialData }: { initialData: DashboardData }) {
  const [health, setHealth] = useState(initialData.health);
  const [markets, setMarkets] = useState(initialData.markets);
  const [chartMarkets, setChartMarkets] = useState(initialData.chartMarkets);
  const [signals, setSignals] = useState(initialData.signals);
  const [chartSignals, setChartSignals] = useState(initialData.chartSignals);
  const [candles, setCandles] = useState(initialData.candles);
  const [positions, setPositions] = useState(initialData.positions);
  const [orders, setOrders] = useState(initialData.orders);
  const [settings, setSettings] = useState(initialData.settings);
  const [strategies, setStrategies] = useState(initialData.strategies);
  const [logs, setLogs] = useState(initialData.logs);
  const [calibration, setCalibration] = useState(initialData.calibration);
  const [chartMarketKey, setChartMarketKey] = useState(
    initialData.chartMarket ? marketKey(initialData.chartMarket) : ""
  );
  const [chartTimeframe, setChartTimeframe] = useState(initialData.chartTimeframe);
  const [filters, setFilters] = useState<Filters>({
    status: "all",
    direction: "all",
    timeframe: "all",
    minScore: "0",
    marketType: "all"
  });
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chartSearch, setChartSearch] = useState("");
  const [chartOverlays, setChartOverlays] = useState<ChartOverlays>({
    ema: false, bollinger: false, vwap: false, supertrend: false,
    levels: false, fibonacci: false, signals: true, rsi: false, macd: false, candlestick_patterns: false,
  });
  const [signalSearch, setSignalSearch] = useState("");
  const [signalSort, setSignalSort] = useState<SignalSort>(null);
  const [activeView, setActiveView] = useState<DashboardView>("overview");
  const [tablePages, setTablePages] = useState<TablePageState>({
    signals: { page: 1, pageSize: 10 },
    positions: { page: 1, pageSize: 10 },
    orders: { page: 1, pageSize: 10 },
    closedPositions: { page: 1, pageSize: 10 }
  });
  const [dashboardToasts, setDashboardToasts] = useState<DashboardToast[]>([]);
  const [tooltip, setTooltip] = useState<{ text: string; x: number; y: number } | null>(null);
  const tooltipHideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seenSignalIds = useRef<Set<number>>(
    new Set([...initialData.signals, ...initialData.chartSignals].map((signal) => signal.id))
  );
  const seenLogIds = useRef<Set<number>>(new Set(initialData.logs.map((log) => log.id)));

  function showTooltip(e: React.MouseEvent, text: string) {
    if (tooltipHideTimer.current) clearTimeout(tooltipHideTimer.current);
    setTooltip({ text, x: e.clientX, y: e.clientY });
  }

  function hideTooltip() {
    tooltipHideTimer.current = setTimeout(() => setTooltip(null), 80);
  }

  const chartMarket = useMemo(
    () => markets.find((m) => marketKey(m) === chartMarketKey) ?? null,
    [chartMarketKey, markets]
  );

  const killSwitchEnabled = Boolean(settings.kill_switch?.enabled);
  const openPositions = positions.filter((p) => p.status === "open");
  const closedSignalIds = useMemo(() => {
    const ids = new Set<number>();
    for (const position of positions) {
      if (position.status.toLowerCase() !== "closed") continue;
      const signalId = positionSourceSignalId(position);
      if (signalId !== null) ids.add(signalId);
    }
    return ids;
  }, [positions]);
  const completedOrders = orders.filter((o) =>
    ["filled", "executed", "closed"].includes(o.status.toLowerCase())
  );
  const riskSettings = settings.risk ?? {};
  const minSignalScore = asNumber(riskSettings.min_signal_score) ?? 0;
  const configuredTimeframes = Array.isArray(settings.timeframes?.enabled)
    ? settings.timeframes.enabled.filter((value): value is string => typeof value === "string")
    : [];
  const timeframeOptions = Array.from(new Set([...configuredTimeframes, ...signals.map((signal) => signal.timeframe)]));
  const strongSignals = signals.filter((s) => s.score >= minSignalScore);
  const filteredSignals = useMemo(() => {
    const query = signalSearch.trim().toLowerCase();
    return signals.filter((s) => {
        if (isClosedSignal(s) || closedSignalIds.has(s.id)) return false;
        if (query && !signalSearchText(s).includes(query)) return false;
        if (filters.marketType === "all") return true;
        return s.market?.market_type === filters.marketType;
      });
  }, [signals, filters.marketType, closedSignalIds, signalSearch]);

  const sortedSignals = useMemo(
    () => signalSort ? [...filteredSignals].sort((a, b) => compareSignals(a, b, signalSort)) : filteredSignals,
    [filteredSignals, signalSort]
  );

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 3000);
    return () => clearTimeout(t);
  }, [notice]);

  useEffect(() => {
    if (dashboardToasts.length === 0) return;
    const timer = setTimeout(() => {
      setDashboardToasts((current) => current.filter((toast) => Date.now() - toast.createdAt < 7000));
    }, 1000);
    return () => clearTimeout(timer);
  }, [dashboardToasts]);

  async function loadCandles(market: Market | null, timeframe: string) {
    if (!market) { setCandles([]); return; }
    const path = `/api/candles?symbol=${encodeURIComponent(market.symbol)}&market_type=${encodeURIComponent(market.market_type)}&timeframe=${encodeURIComponent(timeframe)}`;
    setCandles(await apiGet<Candle[]>(path).catch(() => []));
  }

  function updateTablePage(table: TableKey, patch: Partial<{ page: number; pageSize: number }>) {
    setTablePages((current) => ({
      ...current,
      [table]: {
        ...current[table],
        ...patch,
        page: patch.page ?? (patch.pageSize ? 1 : current[table].page)
      }
    }));
  }

  function registerNewSignals(nextSignals: Signal[]) {
    const incoming = nextSignals.filter((signal) => !seenSignalIds.current.has(signal.id));
    for (const signal of nextSignals) seenSignalIds.current.add(signal.id);
    if (incoming.length === 0) return false;
    setDashboardToasts((current) => [
      ...incoming.map((signal) => ({
        id: `signal-${signal.id}`,
        tone: signal.direction.includes("STRONG") || signal.score >= 90 ? "strong" as const : "signal" as const,
        title: `Nuovo segnale ${signal.market?.symbol ?? "Mercato"} ${signal.timeframe}`,
        body: `${signal.direction.replaceAll("_", " ")} · score ${signal.score} · stato ${signal.status}`,
        createdAt: Date.now()
      })),
      ...current
    ].slice(0, 8));
    return true;
  }

  function registerAlertLogs(nextLogs: DecisionLog[], nextMarkets: Market[]) {
    const incoming = nextLogs.filter((log) => !seenLogIds.current.has(log.id));
    for (const log of nextLogs) seenLogIds.current.add(log.id);
    const toasts = incoming
      .map((log) => {
        const toast = buildLogToast(log, nextMarkets);
        return toast ? { id: `log-${log.id}`, ...toast, createdAt: Date.now() } : null;
      })
      .filter((toast): toast is DashboardToast => toast !== null);
    if (toasts.length === 0) return;
    setDashboardToasts((current) => [...toasts, ...current].slice(0, 8));
  }

  async function refresh(nextFilters = filters, options: { silent?: boolean } = {}) {
    const markBusy = !options.silent;
    if (markBusy) setBusy("refresh");
    setError(null);
    try {
      const [
        nextHealth,
        nextMarkets,
        nextChartMarkets,
        nextSignals,
        nextChartSignals,
        nextPositions,
        nextOrders,
        nextSettings,
        nextStrategies,
        nextLogs,
        nextCalibration
      ] =
        await Promise.all([
          apiGet<Health>("/api/health"),
          apiGet<Market[]>("/api/markets"),
          apiGet<Market[]>("/api/markets?active=true&has_candles=true"),
          apiGet<Signal[]>(buildSignalsPath(nextFilters)),
          apiGet<Signal[]>("/api/signals?min_score=0"),
          apiGet<Position[]>("/api/positions"),
          apiGet<Order[]>("/api/orders"),
          apiGet<AppSettings>("/api/settings"),
          apiGet<Strategy[]>("/api/strategies"),
          apiGet<DecisionLog[]>("/api/logs").catch(() => logs),
          apiGet<Calibration>("/api/analytics/calibration")
      ]);
      setHealth(nextHealth);
      setMarkets(nextMarkets);
      setChartMarkets(nextChartMarkets);
      const hasNewSignals = registerNewSignals(nextChartSignals);
      registerAlertLogs(nextLogs, nextMarkets);
      setSignals(nextSignals);
      setChartSignals(nextChartSignals);
      setPositions(nextPositions);
      setOrders(nextOrders);
      setSettings(nextSettings);
      setStrategies(nextStrategies);
      setLogs(nextLogs);
      setCalibration(nextCalibration);

      const currentMarket = nextMarkets.find((m) => marketKey(m) === chartMarketKey);
      const fallbackMarket = nextSignals[0]?.market ?? nextMarkets[0] ?? null;
      const nextChartMarket = currentMarket ?? fallbackMarket;
      if (nextChartMarket) setChartMarketKey(marketKey(nextChartMarket));
      await loadCandles(nextChartMarket, chartTimeframe);
      if (!options.silent) setNotice(hasNewSignals ? "Nuovi segnali ricevuti" : "Dashboard aggiornata");
    } catch (refreshError) {
      setError(parseError(refreshError));
    } finally {
      if (markBusy) setBusy(null);
    }
  }

  async function runAction(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await action();
      setNotice("Operazione completata");
      await refresh();
    } catch (actionError) {
      setError(parseError(actionError));
    } finally {
      setBusy(null);
    }
  }

  async function updateFilters(nextFilters: Filters) {
    setFilters(nextFilters);
    updateTablePage("signals", { page: 1 });
    await refresh(nextFilters);
  }

  function updateSignalSearch(value: string) {
    setSignalSearch(value);
    updateTablePage("signals", { page: 1 });
  }

  function toggleSignalSort(key: SignalSortKey) {
    setSignalSort((current) => {
      if (current?.key !== key) return { key, direction: SIGNAL_SORT_DEFAULTS[key] };
      return { key, direction: current.direction === "asc" ? "desc" : "asc" };
    });
  }

  async function updateChart(nextMarketKey: string, nextTimeframe = chartTimeframe) {
    setChartMarketKey(nextMarketKey);
    setChartTimeframe(nextTimeframe);
    const found = chartMarkets.find((m) => marketKey(m) === nextMarketKey)
      ?? markets.find((m) => marketKey(m) === nextMarketKey)
      ?? null;
    await loadCandles(found, nextTimeframe);
  }

  useEffect(() => {
    const timer = setInterval(() => {
      refresh(filters, { silent: true });
    }, 30000);
    return () => clearInterval(timer);
  });

  return (
    <main className="w-full max-w-360 mx-auto px-6">

      {/* ── Header ── */}
      <header className="flex items-center justify-between gap-4 pt-6 pb-5 max-md:flex-col max-md:items-start">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-text">Trado</h1>
          <p className="text-xs text-muted mt-0.5">Hyperliquid spot + perpetual</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            className={`${ICON_BTN}`}
            onClick={() => refresh()}
            disabled={busy === "refresh"}
            title="Aggiorna dashboard"
          >
            <RefreshCcw size={15} className={busy === "refresh" ? "animate-spin" : ""} />
          </button>
          <button
            className="inline-flex items-center gap-1.5 px-3 h-8 rounded-lg bg-surface-2 text-muted text-xs font-medium hover:text-text hover:bg-white/8 transition-colors disabled:opacity-40"
            onClick={() =>
              runAction("sync", () => apiPost<{ synced: number }>("/api/markets/sync").then(() => undefined))
            }
            disabled={busy === "sync"}
          >
            <Database size={13} />
            Sync mercati
          </button>
          <div
            className={`inline-flex items-center gap-1.5 px-3 h-8 rounded-lg text-xs font-semibold ${
              health.live_trading_allowed
                ? "bg-red/15 text-red ring-1 ring-red/25"
                : "bg-surface-2 text-muted"
            }`}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                health.live_trading_allowed ? "bg-red animate-pulse" : "bg-muted"
              }`}
            />
            {health.mode} · {health.network}
          </div>
          <button
            className={`inline-flex items-center gap-1.5 px-3 h-8 rounded-lg text-xs font-semibold transition-colors disabled:opacity-40 ${
              killSwitchEnabled
                ? "bg-red/20 text-red hover:bg-red/30 ring-1 ring-red/30"
                : "bg-surface-2 text-muted hover:text-text hover:bg-white/8"
            }`}
            onClick={() =>
              runAction("kill-switch", () =>
                apiPost<{ enabled: boolean }>(
                  killSwitchEnabled ? "/api/kill-switch/disable" : "/api/kill-switch/enable"
                ).then(() => undefined)
              )
            }
            disabled={busy === "kill-switch"}
          >
            {killSwitchEnabled ? <XCircle size={13} /> : <Shield size={13} />}
            {killSwitchEnabled ? "Kill switch ON" : "Kill switch"}
          </button>
        </div>
      </header>

      {/* ── Kill switch banner ── */}
      {killSwitchEnabled && (
        <div className="flex items-center gap-3 mb-4 px-4 py-3 rounded-xl bg-red/10 ring-1 ring-red/25 text-red text-sm font-medium">
          <XCircle size={15} className="shrink-0" />
          <span className="flex-1 text-xs">Kill switch attivo — tutti i nuovi segnali sono bloccati</span>
        </div>
      )}

      <nav className="flex items-center gap-1 mb-5 overflow-x-auto rounded-xl bg-surface p-1 shadow-card">
        {[
          { id: "overview", label: "Dashboard", icon: LayoutDashboard },
          { id: "trading", label: "Trading", icon: ListChecks },
          { id: "performance", label: "Performance", icon: BarChart3 },
          { id: "system", label: "Sistema", icon: Activity }
        ].map((item) => {
          const Icon = item.icon;
          const selected = activeView === item.id;
          return (
            <button
              key={item.id}
              className={`inline-flex h-9 items-center gap-2 rounded-lg px-3 text-xs font-semibold transition-colors ${
                selected ? "bg-blue/15 text-blue" : "text-muted hover:bg-white/5 hover:text-text"
              }`}
              onClick={() => setActiveView(item.id as DashboardView)}
            >
              <Icon size={14} />
              {item.label}
            </button>
          );
        })}
      </nav>

      {/* ── Notice / Error ── */}
      {(notice || error) && (
        <div
          className={`flex items-center gap-3 mb-4 px-4 py-2.5 rounded-xl text-sm ${
            error ? "bg-red/10 text-red" : "bg-green/10 text-green"
          }`}
        >
          {error
            ? <XCircle size={14} className="shrink-0" />
            : <CheckCircle2 size={14} className="shrink-0" />
          }
          <span className="flex-1 text-xs">{error ?? notice}</span>
          <button
            className="shrink-0 opacity-60 hover:opacity-100 transition-opacity"
            onClick={() => { setNotice(null); setError(null); }}
          >
            <X size={13} />
          </button>
        </div>
      )}

      {activeView === "overview" && (
      <>
      {/* ── Metriche ── */}
      <section className="grid grid-cols-4 gap-3 mb-5 max-md:grid-cols-2 max-sm:grid-cols-1">
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Mercati</span>
          <strong className="text-2xl font-bold text-text">{markets.length}</strong>
          <span className="text-xs text-muted">
            {markets.filter((m) => m.market_type === "spot").length} spot
            {" · "}
            {markets.filter((m) => m.market_type === "perp").length} perp
          </span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Segnali forti</span>
          <strong className="text-2xl font-bold text-green">{strongSignals.length}</strong>
          <span className="text-xs text-muted">score ≥ {minSignalScore} · {signals.length} totali</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Posizioni aperte</span>
          <strong className="text-2xl font-bold text-text">{openPositions.length}</strong>
          <span className="text-xs text-muted">{positions.length} totali</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Ordini recenti</span>
          <strong className="text-2xl font-bold text-text">{orders.length}</strong>
          <span className="text-xs text-muted">{completedOrders.length} completati</span>
        </div>
      </section>

      {/* ── Grafico ── */}
      <section className="bg-surface rounded-2xl shadow-card overflow-hidden mb-5">
        <div className="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-divider max-md:flex-col max-md:items-start">
          <h2 className="text-sm font-semibold text-text">Grafico</h2>
          <div className="flex items-center gap-2 flex-wrap">
            <input
              type="text"
              placeholder="Cerca…"
              value={chartSearch}
              onChange={(e) => {
                const q = e.target.value;
                setChartSearch(q);
                const filtered = markets.filter((m) => m.symbol.toLowerCase().includes(q.toLowerCase()));
                if (filtered.length > 0 && !filtered.some((m) => marketKey(m) === chartMarketKey)) {
                  updateChart(marketKey(filtered[0]));
                }
              }}
              className={`w-28 ${INPUT_CLS}`}
            />
            <select
              value={chartMarketKey}
              onChange={(e) => { updateChart(e.target.value); setChartSearch(""); }}
              className={SELECT_CLS}
            >
              {chartMarkets
                .filter((m) => m.symbol.toLowerCase().includes(chartSearch.toLowerCase()))
                .map((m) => (
                  <option key={marketKey(m)} value={marketKey(m)}>
                    {marketLabel(m)}
                  </option>
                ))}
            </select>
            <select
              value={chartTimeframe}
              onChange={(e) => updateChart(chartMarketKey, e.target.value)}
              className={SELECT_CLS}
            >
              {timeframeOptions.map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="flex items-center gap-1 px-5 py-2 border-b border-divider flex-wrap">
          {(["signals","ema","bollinger","vwap","supertrend","rsi","macd"] as (keyof ChartOverlays)[]).map((key) => (
            <button
              key={key}
              onClick={() => setChartOverlays(o => ({ ...o, [key]: !o[key] }))}
              className={`px-2 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide border transition-colors ${
                chartOverlays[key]
                  ? "bg-accent/20 border-accent/50 text-accent"
                  : "bg-transparent border-divider text-muted hover:border-muted/50"
              }`}
            >
              {key === "candlestick_patterns" ? "patterns" : key}
            </button>
          ))}
        </div>
        <ChartPanel
          candles={candles}
          signals={chartSignals}
          market={chartMarket}
          timeframe={chartTimeframe}
          network={health.network}
          overlays={chartOverlays}
        />
      </section>

      <SignalsSection
        signals={sortedSignals}
        page={tablePages.signals.page}
        pageSize={tablePages.signals.pageSize}
        onTablePageChange={(patch) => updateTablePage("signals", patch)}
        signalSearch={signalSearch}
        onSignalSearchChange={updateSignalSearch}
        filters={filters}
        onFiltersChange={setFilters}
        onFiltersCommit={updateFilters}
        timeframeOptions={timeframeOptions}
        signalSort={signalSort}
        onToggleSort={toggleSignalSort}
        onSelectMarket={(key) => updateChart(key)}
        showTooltip={showTooltip}
        hideTooltip={hideTooltip}
        runAction={runAction}
        liveTradingAllowed={health.live_trading_allowed}
        killSwitchEnabled={killSwitchEnabled}
      />
      </>
      )}

      {activeView === "trading" && (
      <>
      <TradingView
        positions={positions}
        orders={orders}
        positionsPage={tablePages.positions}
        ordersPage={tablePages.orders}
        onTablePageChange={updateTablePage}
        runAction={runAction}
      />
      </>
      )}

      {activeView === "performance" && (
      <>
      <PerformanceView
        signals={signals}
        positions={positions}
        completedOrdersCount={completedOrders.length}
        calibration={calibration}
        onCalibrationChange={setCalibration}
        settings={settings}
        busy={busy}
        runAction={runAction}
        closedPositionsPage={tablePages.closedPositions}
        onTablePageChange={updateTablePage}
      />
      </>
      )}

      {activeView === "system" && (
      <>
      <SystemView
        strategies={strategies}
        onStrategiesChange={setStrategies}
        logs={logs}
        runAction={runAction}
      />
      </>
      )}

      {dashboardToasts.length > 0 && (
        <div className="fixed right-4 top-4 z-50 grid w-80 max-w-[calc(100vw-2rem)] gap-2">
          {dashboardToasts.map((toast) => {
            const Icon = toast.tone === "risk" ? AlertTriangle : toast.tone === "info" ? Info : toast.tone === "strong" ? Target : Bell;
            const toneClass = toast.tone === "risk"
              ? "border-red/25 bg-red/15 text-red"
              : toast.tone === "strong"
                ? "border-green/25 bg-green/15 text-green"
                : "border-blue/25 bg-blue/15 text-blue";
            return (
            <div
              key={toast.id}
              className="rounded-xl border border-divider bg-surface shadow-deep p-3"
            >
              <div className="flex items-start gap-2">
                <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${toneClass}`}>
                  <Icon size={14} />
                </span>
                <div className="min-w-0 flex-1">
                  <strong className="block truncate text-sm font-semibold text-text">{toast.title}</strong>
                  <span className="text-xs text-muted">{toast.body}</span>
                </div>
                <button
                  className="text-muted hover:text-text"
                  onClick={() => setDashboardToasts((current) => current.filter((item) => item.id !== toast.id))}
                  title="Chiudi notifica"
                >
                  <X size={13} />
                </button>
              </div>
            </div>
            );
          })}
        </div>
      )}

      {/* ── Tooltip ── */}
      {tooltip && (
        <div
          className="fixed z-50 pointer-events-none max-w-72 rounded-xl border border-divider bg-surface-2 px-3 py-2.5 text-xs text-muted shadow-deep whitespace-pre-wrap leading-relaxed"
          style={{ left: tooltip.x + 14, top: tooltip.y - 8 }}
        >
          {tooltip.text}
        </div>
      )}
    </main>
  );
}

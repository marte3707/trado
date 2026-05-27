"use client";

import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Ban,
  BarChart3,
  Bell,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  Info,
  LayoutDashboard,
  ListChecks,
  Play,
  RefreshCcw,
  RotateCcw,
  Shield,
  Square,
  Target,
  Trash2,
  X,
  XCircle
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ChartPanel } from "@/components/ChartPanel";
import {
  AppSettings,
  AutoTunerStatus,
  Calibration,
  CalibrationHistoryEntry,
  DecisionLog,
  Health,
  Market,
  MarketIntelligence,
  Order,
  Position,
  Signal,
  SignalRejections,
  Strategy,
  WorkerCycle,
  apiDelete,
  apiGet,
  apiPatch,
  apiPost
} from "@/lib/api";

type Candle = {
  open_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};

type Filters = {
  status: string;
  direction: string;
  timeframe: string;
  minScore: string;
  marketType: string;
};

type DashboardView = "overview" | "trading" | "performance" | "system";
type TableKey = "signals" | "positions" | "orders" | "closedPositions";
type TablePageState = Record<TableKey, { page: number; pageSize: number }>;
type ToastTone = "signal" | "strong" | "risk" | "info";
type DashboardToast = { id: string; tone: ToastTone; title: string; body: string; createdAt: number };
type SignalSortKey = "id" | "symbol" | "timeframe" | "direction" | "score" | "entry" | "riskReward" | "status" | "createdAt";
type SortDirection = "asc" | "desc";
type SignalSort = { key: SignalSortKey; direction: SortDirection } | null;

export type DashboardData = {
  health: Health;
  markets: Market[];
  chartMarkets: Market[];
  signals: Signal[];
  chartSignals: Signal[];
  candles: Candle[];
  chartMarket: Market | null;
  chartTimeframe: string;
  positions: Position[];
  orders: Order[];
  settings: AppSettings;
  strategies: Strategy[];
  logs: DecisionLog[];
  calibration: Calibration;
  calibrationHistory: CalibrationHistoryEntry[];
  tunerStatus: AutoTunerStatus | null;
  workerCycles: WorkerCycle[];
  signalRejections: SignalRejections;
  marketIntelligence: MarketIntelligence;
};

const TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"];

function marketKey(market: Market) {
  return `${market.market_type}:${market.symbol}`;
}

function marketLabel(market?: Market | null) {
  if (!market) return "—";
  return `${market.symbol} ${market.market_type.toUpperCase()}`;
}

function isClosedSignal(signal: Signal) {
  return signal.status.toLowerCase() === "closed";
}

function positionSourceSignalId(position: Position) {
  const parsed = asNumber(position.metadata_json?.source_signal_id);
  return parsed === null ? null : Math.trunc(parsed);
}

function scoreColor(score: number): string {
  if (score >= 75) return "text-green";
  if (score >= 60) return "text-blue";
  if (score >= 40) return "text-amber";
  return "text-muted";
}

function scoreBgClass(score: number): string {
  if (score >= 75) return "bg-green";
  if (score >= 60) return "bg-blue";
  if (score >= 40) return "bg-amber";
  return "bg-muted";
}

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

function relativeTime(value: string | null): string {
  if (!value) return "—";
  const diff = Date.now() - new Date(value).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "ora";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return formatDate(value);
}

function directionBadge(direction: string) {
  const base = "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold whitespace-nowrap";
  switch (direction) {
    case "STRONG_LONG":  return <span className={`${base} bg-green/20 text-green`}>▲▲ Strong Long</span>;
    case "LONG":         return <span className={`${base} bg-green/12 text-green`}>▲ Long</span>;
    case "STRONG_SHORT": return <span className={`${base} bg-red/20 text-red`}>▼▼ Strong Short</span>;
    case "SHORT":        return <span className={`${base} bg-red/12 text-red`}>▼ Short</span>;
    default:             return <span className={`${base} bg-muted/10 text-muted`}>{direction}</span>;
  }
}

function statusBadge(status: string) {
  const base = "inline-flex px-2 py-0.5 rounded-full text-xs font-medium";
  switch (status) {
    case "new":      return <span className={`${base} bg-blue/15 text-blue`}>Nuovo</span>;
    case "executed": return <span className={`${base} bg-green/15 text-green`}>Eseguito</span>;
    case "ignored":  return <span className={`${base} bg-muted/10 text-muted`}>Ignorato</span>;
    default:         return <span className={`${base} bg-muted/10 text-muted`}>{status}</span>;
  }
}

function sideBadge(side: string) {
  const base = "inline-flex px-2 py-0.5 rounded-full text-xs font-semibold";
  const normalized = side.toUpperCase();
  if (normalized === "LONG")  return <span className={`${base} bg-green/15 text-green`}>Long</span>;
  if (normalized === "SHORT") return <span className={`${base} bg-red/15 text-red`}>Short</span>;
  return <span className={`${base} bg-muted/10 text-muted`}>{side}</span>;
}

function orderStatusBadge(status: string) {
  const base = "inline-flex px-2 py-0.5 rounded-full text-xs font-medium";
  const s = status.toLowerCase();
  if (s === "filled" || s === "executed" || s === "closed") return <span className={`${base} bg-green/15 text-green`}>{status}</span>;
  if (s === "cancelled" || s === "rejected" || s === "failed") return <span className={`${base} bg-red/15 text-red`}>{status}</span>;
  if (s === "pending" || s === "open" || s === "new") return <span className={`${base} bg-blue/15 text-blue`}>{status}</span>;
  return <span className={`${base} bg-muted/10 text-muted`}>{status}</span>;
}

function logEventColor(eventType: string): string {
  const t = eventType.toLowerCase();
  if (t.includes("error") || t.includes("fail")) return "text-red";
  if (t.includes("execut") || t.includes("trade") || t.includes("open")) return "text-green";
  if (t.includes("signal")) return "text-blue";
  if (t.includes("warn") || t.includes("kill")) return "text-amber";
  return "text-text";
}

function logBorderClass(eventType: string): string {
  const t = eventType.toLowerCase();
  if (t.includes("error") || t.includes("fail")) return "border-l-red";
  if (t.includes("execut") || t.includes("trade") || t.includes("open")) return "border-l-green";
  if (t.includes("signal")) return "border-l-blue";
  if (t.includes("warn") || t.includes("kill")) return "border-l-amber";
  return "border-l-muted";
}

function directionTooltip(direction: string, reason?: string): string {
  const descriptions: Record<string, string> = {
    STRONG_LONG:  "Segnale rialzista forte\nTutti gli indicatori allineati al rialzo. Score ≥ 75.",
    LONG:         "Segnale rialzista\nPrevalenza di indicatori positivi. Score 60–74.",
    STRONG_SHORT: "Segnale ribassista forte\nTutti gli indicatori allineati al ribasso. Score ≥ 75.",
    SHORT:        "Segnale ribassista\nPrevalenza di indicatori negativi. Score 60–74.",
  };
  const desc = descriptions[direction] ?? direction;
  return reason ? `${desc}\n\nMotivo: ${reason}` : desc;
}

function scoreSummary(signal: Signal): string {
  const lines: string[] = [`Score ${signal.score} · ${signal.confidence_label}`];
  const bd = signal.strategy_breakdown;
  if (bd && typeof bd === "object" && Object.keys(bd).length > 0) {
    lines.push("───────────────────");
    for (const [name, raw] of Object.entries(bd)) {
      const d = raw as Record<string, unknown>;
      const dir = String(d.direction ?? "");
      if (dir && dir !== "NEUTRAL") {
        lines.push(`${name}: +${d.contribution} (${dir}, conf ${d.confidence}%, peso ${d.weight})`);
      } else {
        lines.push(`${name}: neutro`);
      }
      if (d.reason) lines.push(`  ${d.reason}`);
    }
  }
  return lines.join("\n");
}

function buildSignalsPath(filters: Filters) {
  const params = new URLSearchParams();
  params.set("min_score", filters.minScore || "0");
  if (filters.status !== "all") params.set("status", filters.status);
  if (filters.direction !== "all") params.set("direction", filters.direction);
  if (filters.timeframe !== "all") params.set("timeframe", filters.timeframe);
  return `/api/signals?${params.toString()}`;
}

function parseError(error: unknown) {
  if (error instanceof Error) return error.message;
  return "Operazione non riuscita";
}

function asNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function isWorkerCycleCompleted(log: DecisionLog) {
  return log.message === "Worker cycle completed";
}

function decisionLogAlertLevel(log: DecisionLog): "strong" | "good" | "info" | "warn" | "risk" {
  const eventType = log.event_type.toLowerCase();
  const score = asNumber(log.data?.score);
  if (eventType.includes("error") || eventType.includes("fail")) return "risk";
  if (eventType.includes("strong") || (score !== null && score >= 90)) return "strong";
  if (eventType.includes("signal")) return "good";
  if (eventType.includes("warn") || eventType.includes("kill")) return "warn";
  return "info";
}

function exitTriggerLabel(log: DecisionLog): string | null {
  const text = `${log.event_type} ${log.message}`.toLowerCase();
  if (text.includes("stop_loss") || text.includes("stop loss")) return "SL raggiunto";
  if (text.includes("tp1")) return "TP1 raggiunto";
  if (text.includes("tp2")) return "TP2 raggiunto";
  if (text.includes("tp3")) return "TP3 raggiunto";
  if (text.includes("take_profit") || text.includes("take profit")) return "TP raggiunto";
  return null;
}

function marketLabelById(markets: Market[], marketId: number | null | undefined) {
  if (marketId === null || marketId === undefined) return null;
  const market = markets.find((item) => item.id === marketId);
  return market ? marketLabel(market) : null;
}

function buildLogToast(log: DecisionLog, markets: Market[]): Omit<DashboardToast, "id" | "createdAt"> | null {
  if (isWorkerCycleCompleted(log)) return null;

  const triggerLabel = exitTriggerLabel(log);
  const market = marketLabelById(markets, log.market_id);
  const context = [
    market,
    log.position_id ? `posizione #${log.position_id}` : null,
    log.signal_id ? `segnale #${log.signal_id}` : null,
    typeof log.data?.exit_price === "string" ? `prezzo ${formatCompactNumber(log.data.exit_price, 8)}` : null
  ].filter(Boolean).join(" · ");

  if (triggerLabel) {
    return {
      tone: triggerLabel.startsWith("SL") ? "risk" : "strong",
      title: triggerLabel,
      body: context || log.message
    };
  }

  const level = decisionLogAlertLevel(log);
  if (level !== "strong" && level !== "risk" && level !== "info") return null;
  return {
    tone: level,
    title: level === "strong" ? "Alert Strong" : level === "risk" ? "Alert Risk" : "Alert Info",
    body: context ? `${context} · ${log.message}` : log.message
  };
}

const SIGNAL_SORT_DEFAULTS: Record<SignalSortKey, SortDirection> = {
  id: "desc",
  symbol: "asc",
  timeframe: "asc",
  direction: "asc",
  score: "desc",
  entry: "desc",
  riskReward: "desc",
  status: "asc",
  createdAt: "desc"
};

function signalSearchText(signal: Signal) {
  const strategyNames = Object.keys(signal.strategy_breakdown ?? {});
  return [
    String(signal.id),
    signal.market?.symbol,
    signal.market?.base,
    signal.market?.quote,
    signal.market ? marketLabel(signal.market) : null,
    signal.market?.market_type,
    signal.timeframe,
    signal.direction,
    signal.score,
    signal.confidence_label,
    signal.status,
    signal.reason,
    ...strategyNames
  ].filter(Boolean).join(" ").toLowerCase();
}

function signalSortValue(signal: Signal, key: SignalSortKey): string | number | null {
  if (key === "id") return signal.id;
  if (key === "symbol") return signal.market?.symbol ?? null;
  if (key === "timeframe") return signal.timeframe;
  if (key === "direction") return signal.direction;
  if (key === "score") return signal.score;
  if (key === "entry") return asNumber(signal.entry_price);
  if (key === "riskReward") return asNumber(signal.risk_reward);
  if (key === "createdAt") return new Date(signal.created_at).getTime();
  return signal.status;
}

function compareSignals(a: Signal, b: Signal, sort: Exclude<SignalSort, null>) {
  const aValue = signalSortValue(a, sort.key);
  const bValue = signalSortValue(b, sort.key);
  const aEmpty = aValue === null || aValue === "";
  const bEmpty = bValue === null || bValue === "";

  if (aEmpty && bEmpty) return b.id - a.id;
  if (aEmpty) return 1;
  if (bEmpty) return -1;

  const result = typeof aValue === "number" && typeof bValue === "number"
    ? aValue - bValue
    : String(aValue).localeCompare(String(bValue), undefined, { numeric: true, sensitivity: "base" });

  if (result === 0) return b.id - a.id;
  return sort.direction === "asc" ? result : -result;
}

function formatCompactNumber(value: string | number | null | undefined, maximumFractionDigits = 2): string {
  const parsed = asNumber(value);
  if (parsed === null) return "—";
  return parsed.toLocaleString("it-IT", { maximumFractionDigits });
}

function formatEur(value: string | number | null | undefined): string {
  const parsed = asNumber(value);
  if (parsed === null) return "—";
  return parsed.toLocaleString("it-IT", { style: "currency", currency: "EUR", maximumFractionDigits: 2 });
}

function formatPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}

function getExitPrice(position: Position): string | null {
  const raw = position.metadata_json?.exit_price;
  return typeof raw === "string" ? raw : null;
}

function calibrationChangeLabel(change: { target: string; key: string }) {
  if (change.target === "strategy_weight") return `Peso ${change.key}`;
  if (change.target === "strategy_params") return `Parametri ${change.key}`;
  if (change.key === "min_signal_score") return "Score minimo";
  if (change.key === "min_risk_reward") return "Risk/reward minimo";
  if (change.key === "max_risk_per_trade_pct") return "Rischio per trade";
  return change.key;
}

function calibrationChangeValue(value: unknown) {
  if (value == null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const SELECT_CLS =
  "min-h-[30px] pl-2.5 pr-6 rounded-lg bg-surface-2 text-text text-xs cursor-pointer focus:outline-none focus:ring-2 focus:ring-blue/30 disabled:opacity-40 border-0";

const INPUT_CLS =
  "min-h-[30px] px-2.5 rounded-lg bg-surface-2 text-text text-xs focus:outline-none focus:ring-2 focus:ring-blue/30 border-0 placeholder:text-muted/50";

const ICON_BTN =
  "inline-flex items-center justify-center w-8 h-8 rounded-full text-muted hover:bg-white/8 hover:text-text transition-colors disabled:opacity-30 disabled:cursor-not-allowed";

const ICON_BTN_RED =
  "inline-flex items-center justify-center w-8 h-8 rounded-full text-red/60 hover:bg-red/10 hover:text-red transition-colors disabled:opacity-30 disabled:cursor-not-allowed";

const TH = "px-3 py-2.5 border-b border-divider text-left text-xs font-semibold uppercase tracking-wider text-muted/70";
const TD = "px-3 py-2.5 border-b border-divider/50 text-sm";

function PillFilter({
  value,
  options,
  onChange
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-0.5 bg-surface-2 rounded-lg p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`px-2.5 h-6 rounded-md text-xs font-medium transition-colors ${
            value === opt.value ? "bg-blue/20 text-blue" : "text-muted hover:text-text"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function EmptyRow({ colSpan, message }: { colSpan: number; message: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-10 text-center text-sm text-muted">
        {message}
      </td>
    </tr>
  );
}

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];

function PaginationControls({
  total,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange
}: {
  total: number;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, totalPages);
  const start = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const end = Math.min(total, safePage * pageSize);
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-divider/60 max-sm:flex-col max-sm:items-start">
      <span className="text-xs text-muted">
        {start}-{end} di {total}
      </span>
      <div className="flex items-center gap-2">
        <select
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
          className={SELECT_CLS}
          aria-label="Elementi per pagina"
        >
          {PAGE_SIZE_OPTIONS.map((size) => (
            <option key={size} value={size}>
              {size} / pagina
            </option>
          ))}
        </select>
        <div className="flex items-center gap-1">
          <button
            className={ICON_BTN}
            onClick={() => onPageChange(safePage - 1)}
            disabled={safePage <= 1}
            title="Pagina precedente"
          >
            <ChevronLeft size={14} />
          </button>
          <span className="min-w-12 text-center text-xs text-muted">
            {safePage}/{totalPages}
          </span>
          <button
            className={ICON_BTN}
            onClick={() => onPageChange(safePage + 1)}
            disabled={safePage >= totalPages}
            title="Pagina successiva"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

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
  const [collapsedTfs, setCollapsedTfs] = useState<Record<string, boolean>>({});
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
  const closedPositions = positions.filter((p) => p.status !== "open");
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
  const minAverageQuoteVolumeUsd = asNumber(riskSettings.min_average_quote_volume_usd);
  const configuredTimeframes = Array.isArray(settings.timeframes?.enabled)
    ? settings.timeframes.enabled.filter((value): value is string => typeof value === "string")
    : [];
  const timeframeOptions = Array.from(new Set([...configuredTimeframes, ...signals.map((signal) => signal.timeframe)]));
  const strongSignals = signals.filter((s) => s.score >= minSignalScore);
  const autoCalibrationSettings = settings.auto_calibration ?? {};
  const autoCalibrationEnabled = autoCalibrationSettings.enabled !== false;
  const lastCalibrationRun =
    typeof calibration.state?.last_run_at === "string" ? calibration.state.last_run_at : null;
  const realizedPnls = closedPositions
    .map((position) => asNumber(position.realized_pnl))
    .filter((value): value is number => value !== null);
  const totalPnl = realizedPnls.reduce((sum, value) => sum + value, 0);
  const winningTrades = realizedPnls.filter((value) => value > 0).length;
  const losingTrades = realizedPnls.filter((value) => value < 0).length;
  const winRate = realizedPnls.length > 0 ? (winningTrades / realizedPnls.length) * 100 : null;
  const averagePnl = realizedPnls.length > 0 ? totalPnl / realizedPnls.length : null;
  const bestTrade = realizedPnls.length > 0 ? Math.max(...realizedPnls) : null;
  const worstTrade = realizedPnls.length > 0 ? Math.min(...realizedPnls) : null;
  const averageHoldMs = closedPositions
    .map((position) => {
      if (!position.closed_at) return null;
      return new Date(position.closed_at).getTime() - new Date(position.opened_at).getTime();
    })
    .filter((value): value is number => value !== null && Number.isFinite(value))
    .reduce(
      (acc, value, _index, array) => ({
        total: acc.total + value,
        count: array.length
      }),
      { total: 0, count: 0 }
    );
  const averageHoldHours = averageHoldMs.count > 0 ? averageHoldMs.total / averageHoldMs.count / 3600000 : null;

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

  const strategiesByTf = useMemo(() => {
    const groups: Record<string, Strategy[]> = {};
    for (const s of strategies) {
      if (!groups[s.timeframe]) groups[s.timeframe] = [];
      groups[s.timeframe].push(s);
    }
    const sorted: Record<string, Strategy[]> = {};
    const allTfs = [
      ...TF_ORDER.filter((tf) => tf in groups),
      ...Object.keys(groups).filter((tf) => !TF_ORDER.includes(tf))
    ];
    for (const tf of allTfs) sorted[tf] = groups[tf];
    return sorted;
  }, [strategies]);

  const signalPage = Math.min(
    tablePages.signals.page,
    Math.max(1, Math.ceil(filteredSignals.length / tablePages.signals.pageSize))
  );
  const positionPage = Math.min(
    tablePages.positions.page,
    Math.max(1, Math.ceil(positions.length / tablePages.positions.pageSize))
  );
  const orderPage = Math.min(
    tablePages.orders.page,
    Math.max(1, Math.ceil(orders.length / tablePages.orders.pageSize))
  );
  const closedPositionPage = Math.min(
    tablePages.closedPositions.page,
    Math.max(1, Math.ceil(closedPositions.length / tablePages.closedPositions.pageSize))
  );
  const paginatedSignals = sortedSignals.slice(
    (signalPage - 1) * tablePages.signals.pageSize,
    signalPage * tablePages.signals.pageSize
  );
  const paginatedPositions = positions.slice(
    (positionPage - 1) * tablePages.positions.pageSize,
    positionPage * tablePages.positions.pageSize
  );
  const paginatedOrders = orders.slice(
    (orderPage - 1) * tablePages.orders.pageSize,
    orderPage * tablePages.orders.pageSize
  );
  const paginatedClosedPositions = closedPositions.slice(
    (closedPositionPage - 1) * tablePages.closedPositions.pageSize,
    closedPositionPage * tablePages.closedPositions.pageSize
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

  function signalSortIcon(key: SignalSortKey) {
    if (signalSort?.key !== key) return <ArrowUpDown size={12} className="opacity-40" />;
    return signalSort.direction === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />;
  }

  function signalHeader(key: SignalSortKey, label: string) {
    return (
      <button
        type="button"
        onClick={() => toggleSignalSort(key)}
        className={`flex items-center gap-1 uppercase tracking-wider ${signalSort?.key === key ? "text-text" : "text-muted/70"}`}
        title={`Ordina per ${label}`}
      >
        <span>{label}</span>
        {signalSortIcon(key)}
      </button>
    );
  }

  async function updateChart(nextMarketKey: string, nextTimeframe = chartTimeframe) {
    setChartMarketKey(nextMarketKey);
    setChartTimeframe(nextTimeframe);
    const found = chartMarkets.find((m) => marketKey(m) === nextMarketKey)
      ?? markets.find((m) => marketKey(m) === nextMarketKey)
      ?? null;
    await loadCandles(found, nextTimeframe);
  }

  function toggleTf(tf: string) {
    setCollapsedTfs((prev) => ({ ...prev, [tf]: !prev[tf] }));
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
        <ChartPanel
          candles={candles}
          signals={chartSignals}
          market={chartMarket}
          timeframe={chartTimeframe}
          network={health.network}
          overlays={{
            ema: false,
            bollinger: false,
            vwap: false,
            supertrend: false,
            levels: false,
            fibonacci: false,
            signals: false,
            rsi: false,
            macd: false,
            candlestick_patterns: false
          }}
        />
      </section>

      {/* ── Segnali ── */}
      <section className="bg-surface rounded-2xl shadow-card overflow-hidden mb-5">
        <div className="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-divider flex-wrap gap-y-2">
          <div className="flex items-center gap-2.5">
            <h2 className="text-sm font-semibold text-text">Segnali</h2>
            {filteredSignals.length > 0 && (
              <span className="text-xs text-muted bg-surface-2 px-2 py-0.5 rounded-full">
                {filteredSignals.length}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <input
              type="text"
              value={signalSearch}
              onChange={(e) => updateSignalSearch(e.target.value)}
              placeholder="Cerca segnale..."
              className={`${INPUT_CLS} w-44`}
            />
            {/* Status pill-tabs */}
            <PillFilter
              value={filters.status}
              options={[
                { value: "all",      label: "Tutti"    },
                { value: "new",      label: "Nuovi"    },
                { value: "executed", label: "Eseguiti" },
                { value: "ignored",  label: "Ignorati" },
              ]}
              onChange={(v) => updateFilters({ ...filters, status: v })}
            />
            {/* Market type */}
            <PillFilter
              value={filters.marketType}
              options={[
                { value: "all",  label: "Spot+Perp" },
                { value: "spot", label: "Spot"      },
                { value: "perp", label: "Perp"      },
              ]}
              onChange={(v) => {
                setFilters({ ...filters, marketType: v });
                updateTablePage("signals", { page: 1 });
              }}
            />
            {/* Direction */}
            <select
              value={filters.direction}
              onChange={(e) => updateFilters({ ...filters, direction: e.target.value })}
              className={SELECT_CLS}
            >
              <option value="all">Direzione</option>
              <option value="LONG">Long</option>
              <option value="STRONG_LONG">Strong Long</option>
              <option value="SHORT">Short</option>
              <option value="STRONG_SHORT">Strong Short</option>
            </select>
            {/* Timeframe */}
            <select
              value={filters.timeframe}
              onChange={(e) => updateFilters({ ...filters, timeframe: e.target.value })}
              className={SELECT_CLS}
            >
              <option value="all">TF</option>
              {timeframeOptions.map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
            {/* Min score */}
            <div className="flex items-center gap-1.5 bg-surface-2 rounded-lg px-2 h-7">
              <span className="text-xs text-muted/70">Score ≥</span>
              <input
                min="0"
                max="100"
                type="number"
                value={filters.minScore}
                onChange={(e) => setFilters({ ...filters, minScore: e.target.value })}
                onBlur={() => updateFilters(filters)}
                className="w-10 bg-transparent text-text text-xs focus:outline-none border-0 p-0"
              />
            </div>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse min-w-240">
            <thead>
              <tr>
                <th className={TH}>{signalHeader("id", "#")}</th>
                <th className={TH}>{signalHeader("symbol", "Coppia")}</th>
                <th className={TH}>{signalHeader("timeframe", "TF")}</th>
                <th className={TH}>{signalHeader("direction", "Direzione")}</th>
                <th className={TH}>{signalHeader("score", "Score")}</th>
                <th className={TH}>{signalHeader("entry", "Entry")}</th>
                <th className={TH}>SL / TP</th>
                <th className={TH}>{signalHeader("riskReward", "R/R")}</th>
                <th className={TH}>{signalHeader("status", "Stato")}</th>
                <th className={TH}>{signalHeader("createdAt", "Ora")}</th>
                <th className={TH} />
              </tr>
            </thead>
            <tbody>
              {filteredSignals.length === 0 && (
                <EmptyRow colSpan={11} message="Nessun segnale corrisponde ai filtri selezionati" />
              )}
              {paginatedSignals.map((sig) => (
                <tr key={sig.id} className="hover:bg-white/2 transition-colors group">
                  <td className={`${TD} text-muted/50 text-xs`}>{sig.id}</td>
                  <td className={`${TD} font-semibold`}>
                    <button
                      className="bg-transparent border-none p-0 text-text text-xs font-semibold cursor-pointer hover:text-blue transition-colors"
                      onClick={() => sig.market && updateChart(marketKey(sig.market))}
                    >
                      {sig.market?.symbol ?? "—"}
                      <span className="ml-1 text-muted/60 font-normal text-[10px]">
                        {sig.market?.market_type?.toUpperCase()}
                      </span>
                    </button>
                  </td>
                  <td className={`${TD} text-muted text-xs`}>{sig.timeframe}</td>
                  <td className={TD}>
                    <span
                      className="cursor-help"
                      onMouseEnter={(e) => showTooltip(e, directionTooltip(sig.direction, sig.reason))}
                      onMouseLeave={hideTooltip}
                    >
                      {directionBadge(sig.direction)}
                    </span>
                  </td>
                  <td className={TD}>
                    <span
                      className="cursor-help flex flex-col gap-1"
                      onMouseEnter={(e) => showTooltip(e, scoreSummary(sig))}
                      onMouseLeave={hideTooltip}
                    >
                      <span className={`text-sm font-bold leading-none ${scoreColor(sig.score)}`}>{sig.score}</span>
                      <span className="h-0.5 w-10 rounded-full bg-white/8 overflow-hidden">
                        <span
                          className={`block h-full rounded-full ${scoreBgClass(sig.score)}`}
                          style={{ width: `${sig.score}%` }}
                        />
                      </span>
                    </span>
                  </td>
                  <td className={`${TD} text-text font-medium text-xs`}>{sig.entry_price}</td>
                  <td className={`${TD} text-xs`}>
                    <div className="flex flex-col gap-0.5 leading-tight">
                      {sig.stop_loss && (
                        <span className="text-red/80">SL {sig.stop_loss}</span>
                      )}
                      {sig.take_profit_1 && (
                        <span className="text-green/70">TP1 {sig.take_profit_1}</span>
                      )}
                      {sig.take_profit_2 && (
                        <span className="text-green/90">TP2 {sig.take_profit_2}</span>
                      )}
                      {sig.take_profit_3 && (
                        <span className="text-green/60">TP3 {sig.take_profit_3}</span>
                      )}
                      {!sig.stop_loss && !sig.take_profit_1 && !sig.take_profit_2 && !sig.take_profit_3 && (
                        <span className="text-muted">—</span>
                      )}
                    </div>
                  </td>
                  <td className={`${TD} text-muted text-xs`}>{sig.risk_reward ?? "—"}</td>
                  <td className={TD}>{statusBadge(sig.status)}</td>
                  <td
                    className={`${TD} text-muted text-xs`}
                    title={formatDate(sig.created_at)}
                  >
                    {relativeTime(sig.created_at)}
                  </td>
                  <td className={TD}>
                    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        className={ICON_BTN}
                        title="Paper trade"
                        disabled={sig.status !== "new"}
                        onClick={() =>
                          runAction(`paper-${sig.id}`, () =>
                            apiPost(`/api/signals/${sig.id}/execute-paper`).then(() => undefined)
                          )
                        }
                      >
                        <Play size={13} />
                      </button>
                      <button
                        className={ICON_BTN}
                        title="Ignora"
                        disabled={sig.status !== "new"}
                        onClick={() =>
                          runAction(`ignore-${sig.id}`, () =>
                            apiPost(`/api/signals/${sig.id}/ignore`).then(() => undefined)
                          )
                        }
                      >
                        <Ban size={13} />
                      </button>
                      <button
                        className={ICON_BTN}
                        title="Ripristina"
                        disabled={sig.status !== "ignored"}
                        onClick={() =>
                          runAction(`reopen-${sig.id}`, () =>
                            apiPost(`/api/signals/${sig.id}/reopen`).then(() => undefined)
                          )
                        }
                      >
                        <RotateCcw size={13} />
                      </button>
                      <button
                        className={ICON_BTN_RED}
                        title="Esegui live"
                        disabled={!health.live_trading_allowed || killSwitchEnabled || sig.status !== "new"}
                        onClick={() => {
                          const symbol = sig.market?.symbol ?? `#${sig.id}`;
                          const dir = sig.direction.toUpperCase();
                          if (!window.confirm(`Eseguire segnale LIVE ${dir} ${symbol}?\nQuesta operazione invierà un ordine reale su Hyperliquid.`)) return;
                          runAction(`live-${sig.id}`, () =>
                            apiPost(`/api/signals/${sig.id}/execute-live`).then(() => undefined)
                          );
                        }}
                      >
                        <Shield size={13} />
                      </button>
                      <button
                        className={ICON_BTN_RED}
                        title="Elimina segnale"
                        onClick={() => {
                          if (!window.confirm(`Eliminare il segnale #${sig.id}?`)) return;
                          runAction(`delete-signal-${sig.id}`, () =>
                            apiDelete(`/api/signals/${sig.id}`).then(() => undefined)
                          );
                        }}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <PaginationControls
          total={filteredSignals.length}
          page={signalPage}
          pageSize={tablePages.signals.pageSize}
          onPageChange={(page) => updateTablePage("signals", { page })}
          onPageSizeChange={(pageSize) => updateTablePage("signals", { pageSize })}
        />
      </section>
      </>
      )}

      {activeView === "trading" && (
      <>
      {/* ── Posizioni + Ordini ── */}
      <div className="flex flex-col gap-4 mb-5">

        {/* Posizioni */}
        <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
          <div className="flex items-center justify-between gap-2 px-5 py-3.5 border-b border-divider">
            <h2 className="text-sm font-semibold text-text">Posizioni</h2>
            <div className="flex items-center gap-2">
              {openPositions.length > 0 && (
                <span className="w-1.5 h-1.5 rounded-full bg-green animate-pulse" />
              )}
              <span className="text-xs text-muted">{openPositions.length} aperte · {positions.length} totali</span>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  {["Coppia", "Lato", "Entry", "P/L live", "SL / TP", "Size", "Stato", ""].map((h, i) => (
                    <th key={i} className={TH}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.length === 0 && (
                  <EmptyRow colSpan={8} message="Nessuna posizione" />
                )}
                {paginatedPositions.map((pos) => {
                  const livePnl = asNumber(pos.unrealized_pnl ?? pos.realized_pnl);
                  const livePnlClass = livePnl === null ? "text-muted" : livePnl >= 0 ? "text-green" : "text-red";

                  return (
                    <tr key={pos.id} className="hover:bg-white/2 transition-colors group">
                      <td className={`${TD} text-text text-xs font-medium`}>{marketLabel(pos.market)}</td>
                      <td className={TD}>{sideBadge(pos.side)}</td>
                      <td className={`${TD} text-text text-xs`}>{pos.entry_price}</td>
                      <td className={`${TD} text-xs font-semibold ${livePnlClass}`}>{formatEur(livePnl)}</td>
                      <td className={`${TD} text-xs`}>
                        <div className="flex flex-col gap-0.5 leading-tight">
                          <span className="text-red/80">SL {pos.stop_loss ?? "—"}</span>
                          <span className="text-green/80">TP {pos.take_profit ?? "—"}</span>
                        </div>
                      </td>
                      <td className={`${TD} text-text text-xs`}>{pos.size}</td>
                      <td className={TD}>
                        <span
                          className={
                            pos.status === "open"
                              ? "text-green text-xs font-medium"
                              : "text-muted text-xs"
                          }
                        >
                          {pos.status === "open" ? "Aperta" : "Chiusa"}
                        </span>
                      </td>
                      <td className={TD}>
                        <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                          <button
                            className={`${ICON_BTN} w-7 h-7`}
                            title="Chiudi posizione"
                            disabled={pos.status !== "open"}
                            onClick={() => {
                              const label = marketLabel(pos.market);
                              const side = pos.side.toUpperCase();
                              const pnl = asNumber(pos.unrealized_pnl ?? pos.realized_pnl);
                              const pnlStr = pnl !== null ? ` (P/L: ${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} $)` : "";
                              if (!window.confirm(`Chiudere ${side} ${label}${pnlStr}?\nVerrà inviato un ordine di chiusura a mercato.`)) return;
                              runAction(`close-${pos.id}`, () =>
                                apiPost(`/api/positions/${pos.id}/close`).then(() => undefined)
                              );
                            }}
                          >
                            <Square size={12} />
                          </button>
                          <button
                            className={`${ICON_BTN_RED} w-7 h-7`}
                            title="Elimina posizione"
                            onClick={() => {
                              const message = pos.status === "open"
                                ? `Eliminare la posizione aperta #${pos.id}? Questo rimuove il tracking, non invia un ordine di chiusura.`
                                : `Eliminare la posizione #${pos.id}?`;
                              if (!window.confirm(message)) return;
                              runAction(`delete-position-${pos.id}`, () =>
                                apiDelete(`/api/positions/${pos.id}`).then(() => undefined)
                              );
                            }}
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <PaginationControls
            total={positions.length}
            page={positionPage}
            pageSize={tablePages.positions.pageSize}
            onPageChange={(page) => updateTablePage("positions", { page })}
            onPageSizeChange={(pageSize) => updateTablePage("positions", { pageSize })}
          />
        </div>

        {/* Ordini */}
        <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
          <div className="flex items-center justify-between gap-2 px-5 py-3.5 border-b border-divider">
            <h2 className="text-sm font-semibold text-text">Ordini</h2>
            <span className="text-xs text-muted">{orders.length} recenti · {completedOrders.length} completati</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  {["#", "Coppia", "Sig/Pos", "Tipo", "Modo", "Lato", "Prezzo", "Size", "Stato", "Ora"].map((h) => (
                    <th key={h} className={TH}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {orders.length === 0 && (
                  <EmptyRow colSpan={10} message="Nessun ordine" />
                )}
                {paginatedOrders.map((order) => (
                  <tr key={order.id} className="hover:bg-white/2 transition-colors">
                    <td className={`${TD} text-muted/50 text-xs`}>{order.id}</td>
                    <td className={`${TD} text-text text-xs font-medium`}>{marketLabel(order.market)}</td>
                    <td className={`${TD} text-muted text-xs`}>
                      <div className="flex flex-col gap-0.5 leading-tight">
                        <span>Sig {order.signal_id ?? "—"}</span>
                        <span>Pos {order.position_id ?? "—"}</span>
                      </div>
                    </td>
                    <td className={`${TD} text-text text-xs`}>{order.order_type}</td>
                    <td className={`${TD} text-text text-xs`}>{order.mode}</td>
                    <td className={TD}>{sideBadge(order.side)}</td>
                    <td className={`${TD} text-text text-xs font-medium`}>{order.price ?? "—"}</td>
                    <td className={`${TD} text-text text-xs`}>{formatCompactNumber(order.size, 6)}</td>
                    <td className={TD}>{orderStatusBadge(order.status)}</td>
                    <td className={`${TD} text-muted text-xs`} title={formatDate(order.created_at)}>
                      {relativeTime(order.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <PaginationControls
            total={orders.length}
            page={orderPage}
            pageSize={tablePages.orders.pageSize}
            onPageChange={(page) => updateTablePage("orders", { page })}
            onPageSizeChange={(pageSize) => updateTablePage("orders", { pageSize })}
          />
        </div>
      </div>
      </>
      )}

      {activeView === "performance" && (
      <>
      <section className="grid grid-cols-4 gap-3 mb-5 max-md:grid-cols-2 max-sm:grid-cols-1">
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">P&L realizzato</span>
          <strong className={`text-2xl font-bold ${totalPnl >= 0 ? "text-green" : "text-red"}`}>
            {formatEur(totalPnl)}
          </strong>
          <span className="text-xs text-muted">{realizedPnls.length} trade con P&L</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Win rate</span>
          <strong className="text-2xl font-bold text-text">{formatPercent(winRate)}</strong>
          <span className="text-xs text-muted">{winningTrades} vincenti · {losingTrades} perdenti</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">P&L medio</span>
          <strong className={`text-2xl font-bold ${(averagePnl ?? 0) >= 0 ? "text-green" : "text-red"}`}>
            {formatEur(averagePnl)}
          </strong>
          <span className="text-xs text-muted">Best {formatEur(bestTrade)} · worst {formatEur(worstTrade)}</span>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4 flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-widest text-muted/70">Durata media</span>
          <strong className="text-2xl font-bold text-text">
            {averageHoldHours === null ? "—" : `${averageHoldHours.toFixed(1)}h`}
          </strong>
          <span className="text-xs text-muted">{closedPositions.length} posizioni chiuse</span>
        </div>
      </section>

      <section className="grid grid-cols-3 gap-3 mb-5 max-md:grid-cols-1">
        <div className="bg-surface rounded-2xl shadow-card p-4">
          <h2 className="text-sm font-semibold text-text mb-3">Distribuzione</h2>
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Long chiusi</span>
              <strong className="text-text">
                {closedPositions.filter((position) => position.side.toLowerCase() === "long").length}
              </strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Short chiusi</span>
              <strong className="text-text">
                {closedPositions.filter((position) => position.side.toLowerCase() === "short").length}
              </strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Auto TP/SL</span>
              <strong className="text-text">
                {closedPositions.filter((position) => typeof position.metadata_json?.exit_reason === "string").length}
              </strong>
            </div>
          </div>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4">
          <h2 className="text-sm font-semibold text-text mb-3">Rischio</h2>
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Posizioni aperte</span>
              <strong className="text-text">{openPositions.length}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Max posizioni</span>
              <strong className="text-text">{String(riskSettings.max_open_positions ?? "—")}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Liquidità min.</span>
              <strong className="text-text">{formatEur(minAverageQuoteVolumeUsd)}</strong>
            </div>
          </div>
        </div>
        <div className="bg-surface rounded-2xl shadow-card p-4">
          <h2 className="text-sm font-semibold text-text mb-3">Operatività</h2>
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Ordini completati</span>
              <strong className="text-text">{completedOrders.length}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Segnali eseguiti</span>
              <strong className="text-text">{signals.filter((signal) => signal.status === "executed").length}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Conversione</span>
              <strong className="text-text">
                {formatPercent(signals.length > 0 ? (positions.length / signals.length) * 100 : null)}
              </strong>
            </div>
          </div>
        </div>
      </section>

      <section className="bg-surface rounded-2xl shadow-card overflow-hidden mb-5">
        <div className="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-divider max-sm:flex-col max-sm:items-start">
          <div>
            <h2 className="text-sm font-semibold text-text">Auto-calibrazione</h2>
            <p className="text-xs text-muted mt-0.5">
              {autoCalibrationEnabled ? "attiva" : "disattivata"} · {calibration.reason}
            </p>
          </div>
          <button
            className="inline-flex items-center gap-1.5 px-3 h-8 rounded-lg bg-blue/15 text-blue text-xs font-semibold hover:bg-blue/20 transition-colors disabled:opacity-40"
            onClick={() =>
              runAction("auto-calibration", () =>
                apiPost<Calibration>("/api/auto-calibration/run").then((result) => {
                  setCalibration(result);
                })
              )
            }
            disabled={busy === "auto-calibration"}
            title="Esegui auto-calibrazione"
          >
            <RefreshCcw size={13} className={busy === "auto-calibration" ? "animate-spin" : ""} />
            Calibra
          </button>
        </div>
        <div className="grid grid-cols-[0.9fr_1.1fr] gap-4 p-4 max-lg:grid-cols-1">
          <div className="grid gap-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Trade analizzati</span>
              <strong className="text-text">{calibration.overall.trades}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Win rate storico</span>
              <strong className="text-text">{formatPercent(calibration.overall.win_rate * 100)}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Profit factor</span>
              <strong className="text-text">{formatCompactNumber(calibration.overall.profit_factor)}</strong>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted">Ultima esecuzione</span>
              <strong className="text-text">{lastCalibrationRun ? relativeTime(lastCalibrationRun) : "—"}</strong>
            </div>
          </div>
          <div className="grid gap-2">
            {calibration.changes.length === 0 && (
              <p className="text-xs text-muted py-2">Nessuna variazione proposta con lo storico disponibile.</p>
            )}
            {calibration.changes.slice(0, 6).map((change) => (
              <div
                key={`${change.target}:${change.key}`}
                className="grid grid-cols-[1fr_auto] gap-2 rounded-xl bg-surface-2 px-3 py-2"
                title={change.reason}
              >
                <span className="text-xs text-text truncate">{calibrationChangeLabel(change)}</span>
                <strong className="text-xs text-blue whitespace-nowrap">
                  {calibrationChangeValue(change.before)} → {calibrationChangeValue(change.after)}
                </strong>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="bg-surface rounded-2xl shadow-card overflow-hidden mb-5">
        <div className="flex items-center justify-between gap-2 px-5 py-3.5 border-b border-divider">
          <h2 className="text-sm font-semibold text-text">Posizioni chiuse</h2>
          <span className="text-xs text-muted">{closedPositions.length} chiuse</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse min-w-200">
            <thead>
              <tr>
                {["Coppia", "Lato", "Entry", "Exit", "P&L", "Aperta", "Chiusa", ""].map((h) => (
                  <th key={h} className={TH}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {closedPositions.length === 0 && (
                <EmptyRow colSpan={8} message="Nessuna posizione chiusa" />
              )}
              {paginatedClosedPositions.map((position) => {
                const pnl = asNumber(position.realized_pnl);
                return (
                  <tr key={position.id} className="hover:bg-white/2 transition-colors group">
                    <td className={`${TD} text-text text-xs font-medium`}>{marketLabel(position.market)}</td>
                    <td className={TD}>{sideBadge(position.side)}</td>
                    <td className={`${TD} text-text text-xs`}>{position.entry_price}</td>
                    <td className={`${TD} text-text text-xs`}>{getExitPrice(position) ?? "—"}</td>
                    <td className={`${TD} text-xs font-semibold ${pnl === null ? "text-muted" : pnl >= 0 ? "text-green" : "text-red"}`}>
                      {formatEur(position.realized_pnl)}
                    </td>
                    <td className={`${TD} text-muted text-xs`}>{relativeTime(position.opened_at)}</td>
                    <td className={`${TD} text-muted text-xs`}>{relativeTime(position.closed_at)}</td>
                    <td className={TD}>
                      <button
                        className={`${ICON_BTN_RED} w-7 h-7 opacity-0 group-hover:opacity-100 transition-opacity`}
                        title="Elimina posizione"
                        onClick={() => {
                          if (!window.confirm(`Eliminare la posizione #${position.id}?`)) return;
                          runAction(`delete-position-${position.id}`, () =>
                            apiDelete(`/api/positions/${position.id}`).then(() => undefined)
                          );
                        }}
                      >
                        <Trash2 size={12} />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <PaginationControls
          total={closedPositions.length}
          page={closedPositionPage}
          pageSize={tablePages.closedPositions.pageSize}
          onPageChange={(page) => updateTablePage("closedPositions", { page })}
          onPageSizeChange={(pageSize) => updateTablePage("closedPositions", { pageSize })}
        />
      </section>
      </>
      )}

      {activeView === "system" && (
      <>
      {/* ── Strategie + Log ── */}
      <div className="grid grid-cols-2 gap-4 mb-5 max-md:grid-cols-1">

        {/* Strategie raggruppate per TF */}
        <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
          <div className="flex items-center justify-between px-5 py-3.5 border-b border-divider">
            <h2 className="text-sm font-semibold text-text">Strategie</h2>
            <span className="text-xs text-muted">
              {strategies.filter((s) => s.enabled).length}/{strategies.length} abilitate
            </span>
          </div>
          <div className="divide-y divide-divider/40">
            {Object.keys(strategiesByTf).length === 0 && (
              <p className="text-sm text-muted text-center py-6">Nessuna strategia configurata</p>
            )}
            {Object.entries(strategiesByTf).map(([tf, group]) => {
              const isCollapsed = collapsedTfs[tf];
              const enabledCount = group.filter((s) => s.enabled).length;
              return (
                <div key={tf}>
                  <button
                    className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-white/3 transition-colors text-left"
                    onClick={() => toggleTf(tf)}
                  >
                    <div className="flex items-center gap-2">
                      {isCollapsed
                        ? <ChevronRight size={12} className="text-muted/60 shrink-0" />
                        : <ChevronDown size={12} className="text-muted/60 shrink-0" />
                      }
                      <span className="text-xs font-bold text-text tracking-wide">{tf}</span>
                      <span className="text-xs text-muted/60">{group.length} str.</span>
                    </div>
                    <span className={`text-xs font-semibold ${enabledCount > 0 ? "text-green" : "text-muted/50"}`}>
                      {enabledCount}/{group.length}
                    </span>
                  </button>
                  {!isCollapsed && (
                    <div className="grid gap-1 px-3 pb-3 pt-1">
                      {group.map((strategy) => (
                        <div
                          key={strategy.id}
                          className="grid grid-cols-[1fr_52px] items-center gap-2 px-2.5 py-2 rounded-xl bg-surface-2"
                        >
                          <label className="flex items-center gap-2 min-w-0 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={strategy.enabled}
                              onChange={() =>
                                runAction(`strategy-${strategy.id}`, () =>
                                  apiPatch(`/api/strategies/${strategy.id}`, {
                                    enabled: !strategy.enabled
                                  }).then(() => undefined)
                                )
                              }
                              className="w-3.5 h-3.5 shrink-0 accent-blue cursor-pointer"
                            />
                            <span
                              className={`text-xs truncate ${strategy.enabled ? "text-text" : "text-muted/60"}`}
                            >
                              {strategy.strategy_name}
                            </span>
                          </label>
                          <input
                            type="number"
                            min="0"
                            max="100"
                            value={strategy.weight}
                            title={`Peso ${strategy.strategy_name}`}
                            aria-label={`Peso ${strategy.strategy_name}`}
                            onChange={(e) =>
                              setStrategies((current) =>
                                current.map((item) =>
                                  item.id === strategy.id
                                    ? { ...item, weight: Number(e.target.value) }
                                    : item
                                )
                              )
                            }
                            onBlur={(e) =>
                              runAction(`weight-${strategy.id}`, () =>
                                apiPatch(`/api/strategies/${strategy.id}`, {
                                  weight: Number(e.currentTarget.value)
                                }).then(() => undefined)
                              )
                            }
                            className={`w-full text-center ${INPUT_CLS}`}
                          />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Log */}
        <div className="bg-surface rounded-2xl shadow-card overflow-hidden">
          <div className="flex items-center justify-between px-5 py-3.5 border-b border-divider">
            <h2 className="text-sm font-semibold text-text">Log</h2>
            <span className="text-xs text-muted">{logs.length} eventi</span>
          </div>
          <div className="grid gap-1.5 p-3">
            {logs.length === 0 && (
              <p className="text-sm text-muted text-center py-6">Nessun evento registrato</p>
            )}
            {logs.slice(0, 16).map((log) => (
              <div
                key={log.id}
                className={`grid grid-cols-[1fr_auto] gap-x-2 gap-y-0.5 px-2.5 py-2 rounded-lg bg-surface-2 border-l-2 ${logBorderClass(log.event_type)}`}
              >
                <strong className={`text-xs font-semibold truncate ${logEventColor(log.event_type)}`}>
                  {log.event_type}
                </strong>
                <span
                  className="text-[10px] text-muted text-right cursor-default whitespace-nowrap"
                  title={formatDate(log.timestamp)}
                >
                  {relativeTime(log.timestamp)}
                </span>
                <p className="col-span-2 text-xs text-muted m-0 leading-relaxed">{log.message}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
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

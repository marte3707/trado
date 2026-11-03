import {
  AppSettings,
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
} from "@/lib/api";

export type Candle = {
  open_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};

export type Filters = {
  status: string;
  direction: string;
  timeframe: string;
  minScore: string;
  marketType: string;
};

export type DashboardView = "overview" | "trading" | "performance" | "system";
export type TableKey = "signals" | "positions" | "orders" | "closedPositions";
export type TablePageState = Record<TableKey, { page: number; pageSize: number }>;
export type ToastTone = "signal" | "strong" | "risk" | "info";
export type DashboardToast = { id: string; tone: ToastTone; title: string; body: string; createdAt: number };
export type SignalSortKey = "id" | "symbol" | "timeframe" | "direction" | "score" | "entry" | "riskReward" | "status" | "createdAt";
export type SortDirection = "asc" | "desc";
export type SignalSort = { key: SignalSortKey; direction: SortDirection } | null;

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
  workerCycles: WorkerCycle[];
  signalRejections: SignalRejections;
  marketIntelligence: MarketIntelligence;
};

export const TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"];

export function marketKey(market: Market) {
  return `${market.market_type}:${market.symbol}`;
}

export function marketLabel(market?: Market | null) {
  if (!market) return "—";
  return `${market.symbol} ${market.market_type.toUpperCase()}`;
}

export function isClosedSignal(signal: Signal) {
  return signal.status.toLowerCase() === "closed";
}

export function positionSourceSignalId(position: Position) {
  const parsed = asNumber(position.metadata_json?.source_signal_id);
  return parsed === null ? null : Math.trunc(parsed);
}

export function scoreColor(score: number): string {
  if (score >= 75) return "text-green";
  if (score >= 60) return "text-blue";
  if (score >= 40) return "text-amber";
  return "text-muted";
}

export function scoreBgClass(score: number): string {
  if (score >= 75) return "bg-green";
  if (score >= 60) return "bg-blue";
  if (score >= 40) return "bg-amber";
  return "bg-muted";
}

export function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

export function relativeTime(value: string | null): string {
  if (!value) return "—";
  const diff = Date.now() - new Date(value).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "ora";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return formatDate(value);
}

export function directionBadge(direction: string) {
  const base = "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold whitespace-nowrap";
  switch (direction) {
    case "STRONG_LONG":  return <span className={`${base} bg-green/20 text-green`}>▲▲ Strong Long</span>;
    case "LONG":         return <span className={`${base} bg-green/12 text-green`}>▲ Long</span>;
    case "STRONG_SHORT": return <span className={`${base} bg-red/20 text-red`}>▼▼ Strong Short</span>;
    case "SHORT":        return <span className={`${base} bg-red/12 text-red`}>▼ Short</span>;
    default:             return <span className={`${base} bg-muted/10 text-muted`}>{direction}</span>;
  }
}

export function statusBadge(status: string) {
  const base = "inline-flex px-2 py-0.5 rounded-full text-xs font-medium";
  switch (status) {
    case "new":      return <span className={`${base} bg-blue/15 text-blue`}>Nuovo</span>;
    case "executed": return <span className={`${base} bg-green/15 text-green`}>Eseguito</span>;
    case "ignored":  return <span className={`${base} bg-muted/10 text-muted`}>Ignorato</span>;
    default:         return <span className={`${base} bg-muted/10 text-muted`}>{status}</span>;
  }
}

export function sideBadge(side: string) {
  const base = "inline-flex px-2 py-0.5 rounded-full text-xs font-semibold";
  const normalized = side.toUpperCase();
  if (normalized === "LONG")  return <span className={`${base} bg-green/15 text-green`}>Long</span>;
  if (normalized === "SHORT") return <span className={`${base} bg-red/15 text-red`}>Short</span>;
  return <span className={`${base} bg-muted/10 text-muted`}>{side}</span>;
}

export function orderStatusBadge(status: string) {
  const base = "inline-flex px-2 py-0.5 rounded-full text-xs font-medium";
  const s = status.toLowerCase();
  if (s === "filled" || s === "executed" || s === "closed") return <span className={`${base} bg-green/15 text-green`}>{status}</span>;
  if (s === "cancelled" || s === "rejected" || s === "failed") return <span className={`${base} bg-red/15 text-red`}>{status}</span>;
  if (s === "pending" || s === "open" || s === "new") return <span className={`${base} bg-blue/15 text-blue`}>{status}</span>;
  return <span className={`${base} bg-muted/10 text-muted`}>{status}</span>;
}

export function logEventColor(eventType: string): string {
  const t = eventType.toLowerCase();
  if (t.includes("error") || t.includes("fail")) return "text-red";
  if (t.includes("execut") || t.includes("trade") || t.includes("open")) return "text-green";
  if (t.includes("signal")) return "text-blue";
  if (t.includes("warn") || t.includes("kill")) return "text-amber";
  return "text-text";
}

export function logBorderClass(eventType: string): string {
  const t = eventType.toLowerCase();
  if (t.includes("error") || t.includes("fail")) return "border-l-red";
  if (t.includes("execut") || t.includes("trade") || t.includes("open")) return "border-l-green";
  if (t.includes("signal")) return "border-l-blue";
  if (t.includes("warn") || t.includes("kill")) return "border-l-amber";
  return "border-l-muted";
}

export function directionTooltip(direction: string, reason?: string): string {
  const descriptions: Record<string, string> = {
    STRONG_LONG:  "Segnale rialzista forte\nTutti gli indicatori allineati al rialzo. Score ≥ 75.",
    LONG:         "Segnale rialzista\nPrevalenza di indicatori positivi. Score 60–74.",
    STRONG_SHORT: "Segnale ribassista forte\nTutti gli indicatori allineati al ribasso. Score ≥ 75.",
    SHORT:        "Segnale ribassista\nPrevalenza di indicatori negativi. Score 60–74.",
  };
  const desc = descriptions[direction] ?? direction;
  return reason ? `${desc}\n\nMotivo: ${reason}` : desc;
}

export function scoreSummary(signal: Signal): string {
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

export function buildSignalsPath(filters: Filters) {
  const params = new URLSearchParams();
  params.set("min_score", filters.minScore || "0");
  if (filters.status !== "all") params.set("status", filters.status);
  if (filters.direction !== "all") params.set("direction", filters.direction);
  if (filters.timeframe !== "all") params.set("timeframe", filters.timeframe);
  return `/api/signals?${params.toString()}`;
}

export function parseError(error: unknown) {
  if (error instanceof Error) return error.message;
  return "Operazione non riuscita";
}

export function asNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function isWorkerCycleCompleted(log: DecisionLog) {
  return log.message === "Worker cycle completed";
}

export function decisionLogAlertLevel(log: DecisionLog): "strong" | "good" | "info" | "warn" | "risk" {
  const eventType = log.event_type.toLowerCase();
  const score = asNumber(log.data?.score);
  if (eventType.includes("error") || eventType.includes("fail")) return "risk";
  if (eventType.includes("strong") || (score !== null && score >= 90)) return "strong";
  if (eventType.includes("signal")) return "good";
  if (eventType.includes("warn") || eventType.includes("kill")) return "warn";
  return "info";
}

export function exitTriggerLabel(log: DecisionLog): string | null {
  const text = `${log.event_type} ${log.message}`.toLowerCase();
  if (text.includes("stop_loss") || text.includes("stop loss")) return "SL raggiunto";
  if (text.includes("tp1")) return "TP1 raggiunto";
  if (text.includes("tp2")) return "TP2 raggiunto";
  if (text.includes("tp3")) return "TP3 raggiunto";
  if (text.includes("take_profit") || text.includes("take profit")) return "TP raggiunto";
  return null;
}

export function marketLabelById(markets: Market[], marketId: number | null | undefined) {
  if (marketId === null || marketId === undefined) return null;
  const market = markets.find((item) => item.id === marketId);
  return market ? marketLabel(market) : null;
}

export function buildLogToast(log: DecisionLog, markets: Market[]): Omit<DashboardToast, "id" | "createdAt"> | null {
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

export const SIGNAL_SORT_DEFAULTS: Record<SignalSortKey, SortDirection> = {
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

export function signalSearchText(signal: Signal) {
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

export function signalSortValue(signal: Signal, key: SignalSortKey): string | number | null {
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

export function compareSignals(a: Signal, b: Signal, sort: Exclude<SignalSort, null>) {
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

export function formatCompactNumber(value: string | number | null | undefined, maximumFractionDigits = 2): string {
  const parsed = asNumber(value);
  if (parsed === null) return "—";
  return parsed.toLocaleString("it-IT", { maximumFractionDigits });
}

export function formatEur(value: string | number | null | undefined): string {
  const parsed = asNumber(value);
  if (parsed === null) return "—";
  return parsed.toLocaleString("it-IT", { style: "currency", currency: "EUR", maximumFractionDigits: 2 });
}

export function formatPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}

export function getExitPrice(position: Position): string | null {
  const raw = position.metadata_json?.exit_price;
  return typeof raw === "string" ? raw : null;
}

export function calibrationChangeLabel(change: { target: string; key: string }) {
  if (change.target === "strategy_weight") return `Peso ${change.key}`;
  if (change.target === "strategy_params") return `Parametri ${change.key}`;
  if (change.key === "min_signal_score") return "Score minimo";
  if (change.key === "min_risk_reward") return "Risk/reward minimo";
  if (change.key === "max_risk_per_trade_pct") return "Rischio per trade";
  return change.key;
}

export function calibrationChangeValue(value: unknown) {
  if (value == null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export const SELECT_CLS =
  "min-h-[30px] pl-2.5 pr-6 rounded-lg bg-surface-2 text-text text-xs cursor-pointer focus:outline-none focus:ring-2 focus:ring-blue/30 disabled:opacity-40 border-0";

export const INPUT_CLS =
  "min-h-[30px] px-2.5 rounded-lg bg-surface-2 text-text text-xs focus:outline-none focus:ring-2 focus:ring-blue/30 border-0 placeholder:text-muted/50";

export const ICON_BTN =
  "inline-flex items-center justify-center w-8 h-8 rounded-full text-muted hover:bg-white/8 hover:text-text transition-colors disabled:opacity-30 disabled:cursor-not-allowed";

export const ICON_BTN_RED =
  "inline-flex items-center justify-center w-8 h-8 rounded-full text-red/60 hover:bg-red/10 hover:text-red transition-colors disabled:opacity-30 disabled:cursor-not-allowed";

export const TH = "px-3 py-2.5 border-b border-divider text-left text-xs font-semibold uppercase tracking-wider text-muted/70";
export const TD = "px-3 py-2.5 border-b border-divider/50 text-sm";

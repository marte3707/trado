import type { DecisionLog } from "@/lib/api";

export type AlertLevel = "strong" | "good" | "info" | "warn" | "risk";
export type AlertMode = "live" | "paper" | "system" | "unknown";

export interface AlertItem {
  id: number;
  ts: string;
  eventType: string;
  title: string;
  level: AlertLevel;
  mode: AlertMode;
  message: string;
  data: Record<string, unknown>;
  marketId?: number | null;
  signalId?: number | null;
  positionId?: number | null;
}

export const EVENT_LABELS: Record<string, string> = {
  live_order_rejected: "Ordine rifiutato dai guardrail",
  live_order_rejected_by_exchange: "Ordine rifiutato dall'exchange",
  live_order_submitted: "Ordine inviato all'exchange",
  live_protection_orders_submitted: "SL/TP inviati all'exchange",
  live_protection_orders_partial_failure: "SL/TP parzialmente falliti",
  live_position_time_stop_alert: "Time-stop da gestire",
  live_close_rejected_by_exchange: "Chiusura rifiutata dall'exchange",
  live_position_reconciled_closed: "Posizione chiusa su Hyperliquid",
  live_stop_update_submit_failed: "Aggiornamento stop fallito",
  live_stop_update_stuck: "Stop live bloccato",
  live_position_liquidation_guard_failed: "Guardia liquidazione attivata",
  live_liquidation_guard_sl_adjusted: "Stop aggiustato prima del liq price",
  live_liquidation_guard_recovered: "Leverage ri-applicato, guard superato",
  live_liquidation_guard_recovery_failed: "Re-applicazione leverage fallita",
  live_order_skipped_hl_position_exists: "Trade saltato: posizione HL già esistente",
  live_position_exit_data_patched: "Dati uscita live corretti",
  paper_order_executed: "Posizione paper aperta",
  paper_order_skipped_duplicate_signal: "Apertura saltata: segnale duplicato",
  paper_order_skipped_duplicate_market: "Apertura saltata: mercato gia' aperto",
  paper_position_auto_closed: "Posizione paper chiusa automaticamente",
  paper_position_partial_closed: "Posizione chiusa parzialmente",
  paper_position_time_stop_closed: "Posizione chiusa per time-stop",
  position_closed: "Posizione chiusa",
  position_partially_closed: "Posizione chiusa parzialmente",
  position_stop_updated: "Stop loss aggiornato",
  signal_generated: "Nuovo segnale generato",
  signal_executed: "Segnale eseguito",
  signal_ignored: "Segnale ignorato",
  signal_reopened: "Segnale riaperto",
  signal_deleted: "Segnale eliminato",
  position_deleted: "Posizione eliminata",
  take_profit_rescaled: "Take profit riscalato",
  auto_calibration_applied: "Auto-calibrazione applicata",
  auto_calibration_checked: "Auto-calibrazione verificata",
  auto_calibration_skipped: "Auto-calibrazione saltata",
  auto_calibration_failed: "Auto-calibrazione fallita",
  auto_tuner_step_started: "Auto-tuner avviato",
  auto_tuner_paused: "Auto-tuner in pausa",
  auto_tuner_resumed: "Auto-tuner ripreso",
  auto_tuner_failed: "Auto-tuner fallito",
  worker_cycle_failed: "Worker cycle fallito",
  kill_switch_enabled: "Kill switch armato",
  kill_switch_disabled: "Kill switch disarmato",
};

export function inferMode(eventType: string, data: Record<string, unknown>): AlertMode {
  const mode = String(data.mode ?? "").toLowerCase();
  if (mode === "live" || mode === "paper") return mode;
  if (eventType.startsWith("live_")) return "live";
  if (eventType.startsWith("paper_")) return "paper";
  if (
    eventType.startsWith("auto_") ||
    eventType.startsWith("worker_") ||
    eventType.startsWith("kill_") ||
    eventType.includes("signal_")
  )
    return "system";
  return "unknown";
}

export function inferLevel(eventType: string, data: Record<string, unknown>): AlertLevel {
  const lc = eventType.toLowerCase();
  if (lc.includes("rejected") || lc.includes("failed") || lc.includes("stuck") || lc.includes("liquidation"))
    return "risk";
  if (lc.includes("kill_switch_enabled") || lc.includes("time_stop_alert")) return "warn";
  if (lc.includes("submitted") || lc.includes("executed") || lc.includes("opened")) return "good";
  if (lc.includes("closed") || lc.includes("rescaled") || lc.includes("updated")) return "info";
  if (lc.includes("ignored") || lc.includes("skipped") || lc.includes("paused")) return "warn";
  if (typeof data.score === "number" && data.score >= 90) return "strong";
  return "info";
}

export function logToAlert(log: DecisionLog): AlertItem {
  const data = log.data ?? {};
  const title = EVENT_LABELS[log.event_type] ?? humanize(log.event_type);
  return {
    id: log.id,
    ts: log.timestamp,
    eventType: log.event_type,
    title,
    level: inferLevel(log.event_type, data),
    mode: inferMode(log.event_type, data),
    message: log.message ?? "",
    data,
    marketId: log.market_id ?? null,
    signalId: log.signal_id ?? null,
    positionId: log.position_id ?? null,
  };
}

export function collectNewLiveAlerts(logs: DecisionLog[], seenIds: Set<number>): AlertItem[] {
  const alerts = logs
    .filter((log) => !seenIds.has(log.id))
    .map(logToAlert)
    .filter((alert) => alert.mode === "live")
    .sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));

  logs.forEach((log) => seenIds.add(log.id));
  return alerts;
}

export function liveAlertToastContent(
  alert: AlertItem,
  context: { marketSymbol?: string | null } = {},
): { title: string; description: string } {
  const message = alert.message.trim();
  const details = liveAlertDetails(alert, context);
  const description = [...details, message || alert.eventType].join(" · ");
  return {
    title: `LIVE · ${alert.title}`,
    description,
  };
}

function liveAlertDetails(alert: AlertItem, context: { marketSymbol?: string | null }): string[] {
  const data = alert.data;
  const details: string[] = [];
  const ticker = stringField(data.coin ?? data.ticker ?? data.symbol ?? data.market ?? context.marketSymbol);
  const side = sideLabel(data.side ?? data.position_side ?? data.order_side ?? data.is_buy);
  const notional = notionalUsd(data);

  if (ticker) details.push(ticker);
  if (side) details.push(side);
  if (notional != null) details.push(formatUsd(notional));
  return details;
}

function stringField(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed || null;
}

function sideLabel(value: unknown): string | null {
  if (typeof value === "boolean") return value ? "LONG" : "SHORT";
  const side = stringField(value)?.toLowerCase();
  if (!side) return null;
  if (side === "long") return "LONG";
  if (side === "short") return "SHORT";
  if (side === "buy" || side === "b") return "BUY";
  if (side === "sell" || side === "s") return "SELL";
  return side.toUpperCase();
}

function notionalUsd(data: Record<string, unknown>): number | null {
  const direct =
    numberField(data.notional_usd) ??
    numberField(data.notional) ??
    numberField(data.position_value_usd) ??
    numberField(data.position_value);
  if (direct != null) return direct;

  const size = numberField(data.size) ?? numberField(data.filled_size);
  const price =
    numberField(data.entry_price) ??
    numberField(data.avg_price) ??
    numberField(data.price) ??
    numberField(data.filled_price);
  if (size == null || price == null) return null;
  return Math.abs(size * price);
}

function numberField(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const parsed = Number(value.replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function formatUsd(value: number): string {
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: value >= 100 ? 0 : 2,
    maximumFractionDigits: value >= 100 ? 0 : 2,
  });
}

function humanize(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export const LEVEL_LABEL: Record<AlertLevel, string> = {
  strong: "STRONG",
  good: "GOOD",
  info: "INFO",
  warn: "WARN",
  risk: "RISK",
};

export const MODE_LABEL: Record<AlertMode, string> = {
  live: "LIVE",
  paper: "PAPER",
  system: "SYSTEM",
  unknown: "—",
};

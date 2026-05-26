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

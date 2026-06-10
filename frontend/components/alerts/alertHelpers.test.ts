import { describe, expect, it } from "vitest";

import { collectNewLiveAlerts, liveAlertToastContent } from "@/components/alerts/alertHelpers";
import type { DecisionLog } from "@/lib/api";

function log(overrides: Partial<DecisionLog>): DecisionLog {
  return {
    id: 1,
    timestamp: "2026-06-10T10:00:00Z",
    event_type: "live_order_submitted",
    market_id: null,
    signal_id: null,
    position_id: null,
    message: "sent",
    data: {},
    ...overrides,
  };
}

describe("alertHelpers live alert toasts", () => {
  it("collects only unseen live alerts and marks fetched logs as seen", () => {
    const seenIds = new Set([1]);
    const alerts = collectNewLiveAlerts(
      [
        log({ id: 3, timestamp: "2026-06-10T10:02:00Z", event_type: "paper_order_executed" }),
        log({ id: 2, timestamp: "2026-06-10T10:01:00Z", event_type: "live_order_submitted" }),
        log({ id: 1, timestamp: "2026-06-10T10:00:00Z", event_type: "live_order_rejected" }),
        log({ id: 4, timestamp: "2026-06-10T10:03:00Z", event_type: "live_stop_update_submit_failed" }),
      ],
      seenIds,
    );

    expect(alerts.map((alert) => alert.id)).toEqual([2, 4]);
    expect(seenIds).toEqual(new Set([1, 2, 3, 4]));
  });

  it("builds toast copy from the alert label and falls back to event type", () => {
    const [alert] = collectNewLiveAlerts(
      [log({ id: 10, event_type: "live_order_rejected_by_exchange", message: "" })],
      new Set(),
    );

    expect(liveAlertToastContent(alert)).toEqual({
      title: "LIVE · Ordine rifiutato dall'exchange",
      description: "live_order_rejected_by_exchange",
    });
  });

  it("adds ticker side and USD notional when live payload contains position details", () => {
    const [alert] = collectNewLiveAlerts(
      [
        log({
          id: 11,
          message: "Live position opened from signal",
          data: { side: "long", size: "0.25", entry_price: "2000" },
        }),
      ],
      new Set(),
    );

    expect(liveAlertToastContent(alert, { marketSymbol: "ETH/USDC" })).toEqual({
      title: "LIVE · Ordine inviato all'exchange",
      description: "ETH/USDC · LONG · $500 · Live position opened from signal",
    });
  });

  it("prefers coin from payload and direct notional fields when available", () => {
    const [alert] = collectNewLiveAlerts(
      [
        log({
          id: 12,
          event_type: "live_reconciliation_tp1_filled",
          message: "Reconciled fill",
          data: { coin: "BTC", side: "sell", notional_usd: "125.5" },
        }),
      ],
      new Set(),
    );

    expect(liveAlertToastContent(alert, { marketSymbol: "BTC/USDC" }).description).toBe(
      "BTC · SELL · $126 · Reconciled fill",
    );
  });
});

"use client";

import React, { useEffect, useMemo, useState } from "react";
import { AlertTriangle, RefreshCw, RotateCcw, ChevronDown, ChevronUp } from "lucide-react";
import { Icon, fmt } from "@/components/atoms";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { StatusDot, type DotStatus } from "@/components/shell/StatusDot";
import { cn } from "@/lib/utils";
import {
  AccountEquityHistory,
  AccountFunding,
  AccountMonitor,
  AccountOpenOrder,
  AccountPosition,
  AccountSpotBalance,
  Health,
  apiPost,
} from "@/lib/api";
import { useAccountEquityHistory, useAccountMonitor } from "@/lib/hooks/queries";
import * as Recharts from "recharts";

const RArea = Recharts.Area as unknown as React.ComponentType<Record<string, unknown>>;
const RCartesianGrid = Recharts.CartesianGrid as unknown as React.ComponentType<Record<string, unknown>>;
const RComposedChart = Recharts.ComposedChart as unknown as React.ComponentType<Record<string, unknown> & { children?: React.ReactNode }>;
const RResponsiveContainer = Recharts.ResponsiveContainer as unknown as React.ComponentType<Record<string, unknown> & { children?: React.ReactNode }>;
const RTooltip = Recharts.Tooltip as unknown as React.ComponentType<Record<string, unknown>>;
const RXAxis = Recharts.XAxis as unknown as React.ComponentType<Record<string, unknown>>;
const RYAxis = Recharts.YAxis as unknown as React.ComponentType<Record<string, unknown>>;

// ── Types ────────────────────────────────────────────────────────────────────

type WsState = {
  status: "idle" | "connecting" | "live" | "error" | "closed";
  lastMessageAt: string | null;
  messages: number;
  error: string | null;
};

type ActionState = {
  key: string | null;
  message: string | null;
  error: string | null;
};

type TradingSummary = {
  openPnl: number | null;
  longPnl: number | null;
  shortPnl: number | null;
  expectedLimitPnl: number | null;
  openOrderNotional: number;
  longPositions: number;
  shortPositions: number;
};

type ConfirmDialog = {
  open: boolean;
  title: string;
  description: string;
  detail: string | null;
  onConfirm: () => void;
};

// ── Constants ────────────────────────────────────────────────────────────────

const WS_INITIAL: WsState = { status: "idle", lastMessageAt: null, messages: 0, error: null };
const ACTION_INITIAL: ActionState = { key: null, message: null, error: null };
const CONFIRM_CLOSED: ConfirmDialog = {
  open: false,
  title: "",
  description: "",
  detail: null,
  onConfirm: () => {},
};

// ── Utilities ────────────────────────────────────────────────────────────────

function wsUrl(network: Health["network"]) {
  return network === "mainnet"
    ? "wss://api.hyperliquid.xyz/ws"
    : "wss://api.hyperliquid-testnet.xyz/ws";
}

function asNumber(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function fmtUsd(value: string | number | null | undefined): string {
  const parsed = asNumber(value);
  if (parsed == null) return "-";
  return parsed.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

// Use 4 decimal places for small USD amounts (e.g. funding impacts < $0.01).
function fmtUsdPrecise(value: number | null | undefined): string {
  if (value == null) return "—";
  const abs = Math.abs(value);
  const digits = abs < 0.01 ? 4 : abs < 1 ? 3 : 2;
  return value.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtNum(value: string | number | null | undefined, digits = 4): string {
  const parsed = asNumber(value);
  if (parsed == null) return "-";
  return parsed.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function fmtFunding(value: string | null): string {
  const parsed = asNumber(value);
  if (parsed == null) return "-";
  return `${(parsed * 100).toFixed(4)}%`;
}

function fmtPct(value: string | number | null | undefined, digits = 2): string {
  const parsed = asNumber(value);
  if (parsed == null) return "-";
  return `${parsed.toFixed(digits)}%`;
}

function accountRatioTone(status: string | null | undefined, pct: number | null): "success" | "warn" | "danger" | "muted" {
  if (pct == null || status === "unavailable") return "muted";
  if (status === "liquidation_risk" || status === "critical" || pct >= 90) return "danger";
  if (status === "warning" || pct >= 80) return "warn";
  return "success";
}

function accountRatioHint(status: string | null | undefined, threshold: string | number | null | undefined): string {
  const thresholdText = fmtPct(threshold);
  if (status === "liquidation_risk") return `Liquidation risk · soglia ${thresholdText}`;
  if (status === "critical") return `Blocco nuove aperture · soglia ${thresholdText}`;
  if (status === "warning") return `Warning · soglia ${thresholdText}`;
  if (status === "ok") return `OK · liq risk ${thresholdText}`;
  return "Non disponibile";
}

function fmtTime(ms: number | null | undefined): string {
  if (!ms) return "-";
  return new Intl.DateTimeFormat("it-IT", {
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
  }).format(new Date(ms));
}

function protectionTone(protection: AccountPosition["protection"]): "ok" | "warn" | "muted" {
  if (!protection || protection.status === "untracked") return "muted";
  if (protection.needs_intervention) return "warn";
  return "ok";
}

function protectionLabel(protection: AccountPosition["protection"]): string {
  if (!protection || protection.status === "untracked") return "Untracked";
  if (protection.needs_intervention) return "Needs repair";
  return "OK";
}

function orderTriggerPx(row: AccountOpenOrder): number | null {
  const ot = row.orderType;
  if (ot && typeof ot === "object" && "trigger" in (ot as object)) {
    const trigger = (ot as Record<string, unknown>).trigger as Record<string, unknown>;
    const nested = asNumber(trigger?.triggerPx as string | number | null);
    if (nested != null && nested > 0) return nested;
  }
  const topLevel = asNumber(row.triggerPx);
  if (topLevel != null && topLevel > 0) return topLevel;
  return null;
}

function orderEffectivePx(row: AccountOpenOrder): number | null {
  const triggerPx = orderTriggerPx(row);
  if (triggerPx != null) return triggerPx;
  return asNumber(row.limitPx);
}

function orderEffectiveSz(row: AccountOpenOrder, positionSize?: number | null): number | null {
  const sz = asNumber(row.sz);
  if (sz != null && sz > 0) return sz;
  const orig = asNumber(row.origSz);
  if (orig != null && orig > 0) return orig;
  if (row.reduceOnly && positionSize != null && positionSize > 0) return positionSize;
  return null;
}

function leverageLabel(leverage: Record<string, unknown> | null | undefined): string {
  if (!leverage) return "-";
  const value = asNumber(leverage.value as string | number | null);
  if (value == null) return "-";
  return leverage.type === "isolated" ? `${value}x iso` : `${value}x`;
}

function orderId(row: AccountOpenOrder): string | null {
  if (row.oid == null || row.oid === "") return null;
  return String(row.oid);
}

function orderSide(row: AccountOpenOrder): "buy" | "sell" | null {
  const side = String(row.side ?? "").toLowerCase();
  if (side === "b" || side === "buy" || side === "bid") return "buy";
  if (side === "a" || side === "sell" || side === "ask") return "sell";
  return null;
}

function orderSideLabel(row: AccountOpenOrder): string {
  const side = orderSide(row);
  if (side === "buy") return "Compra";
  if (side === "sell") return "Vendi";
  return String(row.side ?? "-");
}

function orderTypeLabel(row: AccountOpenOrder): string {
  const ot = row.orderType;
  if (ot && typeof ot === "object") {
    const trigger = (ot as Record<string, unknown>).trigger as Record<string, unknown> | undefined;
    if (trigger) {
      const tpsl = trigger.tpsl;
      if (tpsl === "tp") return "Take Profit";
      if (tpsl === "sl") return "Stop Loss";
      return "Trigger";
    }
  }
  if (typeof ot === "string" && ot !== "") return ot;
  const tc = row.triggerCondition;
  if (tc === "tp") return "Take Profit";
  if (tc === "sl") return "Stop Loss";
  return "Limit";
}

function positionByCoin(rows: AccountPosition[]): Map<string, AccountPosition> {
  const map = new Map<string, AccountPosition>();
  for (const row of rows) {
    if (row.coin) map.set(row.coin, row);
  }
  return map;
}

function expectedLimitPnl(row: AccountOpenOrder, position: AccountPosition | undefined): number | null {
  if (!position) return null;
  const entry = asNumber(position.entry_price);
  const limit = orderEffectivePx(row);
  const positionSize = asNumber(position.size);
  const orderSize = orderEffectiveSz(row, positionSize);
  const side = orderSide(row);
  if (entry == null || limit == null || orderSize == null || positionSize == null || !side || !position.side) return null;
  const closesLong = position.side === "long" && side === "sell";
  const closesShort = position.side === "short" && side === "buy";
  if (!closesLong && !closesShort && row.reduceOnly !== true) return null;
  const size = Math.min(Math.abs(orderSize), Math.abs(positionSize));
  const multiplier = position.side === "long" ? 1 : -1;
  return (limit - entry) * size * multiplier;
}

function summarizeTrading(positions: AccountPosition[], orders: AccountOpenOrder[]): TradingSummary {
  const positionsMap = positionByCoin(positions);
  let longPnl: number | null = null;
  let shortPnl: number | null = null;
  for (const pos of positions) {
    const pnl = asNumber(pos.unrealized_pnl) ?? 0;
    if (pos.side === "long") longPnl = (longPnl ?? 0) + pnl;
    else if (pos.side === "short") shortPnl = (shortPnl ?? 0) + pnl;
  }
  const openPnl = positions.length > 0
    ? positions.reduce((sum, p) => sum + (asNumber(p.unrealized_pnl) ?? 0), 0)
    : null;
  const expectedValues = orders
    .map((o) => expectedLimitPnl(o, positionsMap.get(String(o.coin ?? ""))))
    .filter((v): v is number => v !== null);
  const openOrderNotional = orders.reduce((sum, o) => {
    const pos = positionsMap.get(String(o.coin ?? ""));
    const size = orderEffectiveSz(o, asNumber(pos?.size));
    const limit = orderEffectivePx(o);
    return size != null && limit != null ? sum + Math.abs(size * limit) : sum;
  }, 0);
  return {
    openPnl,
    longPnl,
    shortPnl,
    expectedLimitPnl: expectedValues.length > 0 ? expectedValues.reduce((s, v) => s + v, 0) : null,
    openOrderNotional,
    longPositions: positions.filter((p) => p.side === "long").length,
    shortPositions: positions.filter((p) => p.side === "short").length,
  };
}

function cumFundingSinceOpen(position: AccountPosition): number | null {
  if (!position.cum_funding) return null;
  const cf = position.cum_funding as Record<string, string | number | null>;
  const raw = asNumber(cf.sinceOpen);
  // Negate: HL sinceOpen positive = paid (cost), negative = received (income).
  // Display as P&L impact: positive = earned, negative = paid.
  return raw != null ? -raw : null;
}

function liquidationDistance(position: AccountPosition, midPrices: Record<string, number>): number | null {
  const liq = asNumber(position.liquidation_price);
  const mid = midPrices[position.coin ?? ""];
  if (liq == null || mid == null || liq <= 0 || mid <= 0) return null;
  return (Math.abs(mid - liq) / mid) * 100;
}

function dailyFundingPnl(position: AccountPosition, fundingRate: string | null): number | null {
  const rate = asNumber(fundingRate);
  const notional = asNumber(position.position_value);
  if (rate == null || notional == null) return null;
  const sideMultiplier = position.side === "long" ? 1 : -1;
  // Positive rate = longs pay; negative = shorts pay. 3 payments/day (8h interval).
  return -sideMultiplier * rate * Math.abs(notional) * 3;
}

function statusToDot(status: string): DotStatus {
  if (status === "ok" || status === "live") return "ok";
  if (status === "connecting" || status === "loading") return "info";
  if (status === "missing" || status === "closed" || status === "idle") return "muted";
  if (status === "error") return "danger";
  return "warn";
}

// ── Main component ───────────────────────────────────────────────────────────

export function AccountMonitorScreen({ health }: { health: Health }) {
  const { data: snapshot, isFetching, refetch } = useAccountMonitor();
  const [equityDays, setEquityDays] = useState(30);
  const { data: equityHistory } = useAccountEquityHistory(equityDays);
  const [ws, setWs] = useState<WsState>(WS_INITIAL);
  const [midPrices, setMidPrices] = useState<Record<string, number>>({});
  const [action, setAction] = useState<ActionState>(ACTION_INITIAL);
  const [selectedCoin, setSelectedCoin] = useState<string | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialog>(CONFIRM_CLOSED);
  const [debugOpen, setDebugOpen] = useState(false);

  function openConfirm(
    title: string,
    description: string,
    detail: string | null,
    actionKey: string,
    actionPath: string,
  ) {
    setConfirmDialog({
      open: true,
      title,
      description,
      detail,
      onConfirm: async () => {
        setConfirmDialog(CONFIRM_CLOSED);
        setAction({ key: actionKey, message: null, error: null });
        try {
          await apiPost<unknown>(actionPath);
          setAction({ key: null, message: "Azione inviata con successo.", error: null });
          await refetch();
        } catch (exc) {
          setAction({ key: null, message: null, error: exc instanceof Error ? exc.message : "Azione non riuscita" });
        }
      },
    });
  }

  async function reconcileExitData() {
    setAction({ key: "reconcile", message: null, error: null });
    try {
      const result = await apiPost<{
        patched: number;
        positions: { position_id: number; coin: string; exit_price: string; realized_pnl: string | null; was_wrong_data: boolean }[];
      }>("/api/account/positions/patch-missing-exit-data");
      const detail =
        result.positions.length > 0
          ? result.positions.map((p) => `${p.coin} exit=${p.exit_price}${p.was_wrong_data ? " (corretto)" : ""}`).join(", ")
          : "nessuna posizione da riconciliare";
      setAction({ key: null, message: `Riconciliazione completata: ${result.patched} posizioni — ${detail}`, error: null });
    } catch (exc) {
      setAction({ key: null, message: null, error: exc instanceof Error ? exc.message : "Riconciliazione non riuscita" });
    }
  }

  async function repairProtection(coin: string) {
    setAction({ key: `repair-protection-${coin}`, message: null, error: null });
    try {
      const result = await apiPost<{ coin: string; updated_positions: number; protection: AccountPosition["protection"] }>(
        `/api/account/positions/${encodeURIComponent(coin)}/repair-protection`,
      );
      setAction({
        key: null,
        message: `Repair ${coin}: ${result.updated_positions} posizioni rivalutate — ${result.protection?.summary ?? "completato"}`,
        error: null,
      });
      await refetch();
    } catch (exc) {
      setAction({ key: null, message: null, error: exc instanceof Error ? exc.message : "Repair non riuscito" });
    }
  }

  useEffect(() => {
    const socket = new WebSocket(wsUrl(health.network));
    let active = true;
    setWs({ status: "connecting", lastMessageAt: null, messages: 0, error: null });

    socket.addEventListener("open", () => {
      if (!active) return;
      socket.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids" } }));
      setWs((prev) => ({ ...prev, status: "live" }));
    });
    socket.addEventListener("message", (event) => {
      if (!active) return;
      try {
        const data = JSON.parse(event.data as string) as { data?: { mids?: Record<string, string> } };
        if (data?.data?.mids) {
          const mids: Record<string, number> = {};
          for (const [coin, price] of Object.entries(data.data.mids)) {
            const p = Number(price);
            if (Number.isFinite(p)) mids[coin] = p;
          }
          setMidPrices((prev) => ({ ...prev, ...mids }));
        }
      } catch { /* ignore parse errors */ }
      setWs((prev) => ({ ...prev, status: "live", messages: prev.messages + 1, lastMessageAt: new Date().toISOString() }));
    });
    socket.addEventListener("error", () => {
      if (!active) return;
      setWs((prev) => ({ ...prev, status: "error", error: "WebSocket Hyperliquid non raggiungibile" }));
    });
    socket.addEventListener("close", () => {
      if (!active) return;
      setWs((prev) => ({ ...prev, status: prev.status === "error" ? "error" : "closed" }));
    });

    return () => {
      active = false;
      socket.close();
    };
  }, [health.network]);

  const totalUnrealized = useMemo(
    () => snapshot?.positions.reduce((sum, p) => sum + (asNumber(p.unrealized_pnl) ?? 0), 0) ?? null,
    [snapshot],
  );
  const tradingSummary = useMemo(
    () => summarizeTrading(snapshot?.positions ?? [], snapshot?.open_orders ?? []),
    [snapshot],
  );
  const positionsNeedingIntervention = useMemo(
    () => (snapshot?.positions ?? []).filter((position) => position.protection?.needs_intervention),
    [snapshot?.positions],
  );
  const accountValueNum = asNumber(snapshot?.account.effective_account_value ?? snapshot?.account.account_value);
  const marginUsedNum = asNumber(snapshot?.account.total_margin_used);
  const marginRatio =
    accountValueNum != null && accountValueNum > 0 && marginUsedNum != null
      ? (marginUsedNum / accountValueNum) * 100
      : null;
  const unifiedAccountRatio = asNumber(snapshot?.account.unified_account_ratio_pct);
  const unifiedAccountRatioTone = accountRatioTone(
    snapshot?.account.unified_account_ratio_status,
    unifiedAccountRatio,
  );

  const fundingByCoin = useMemo(() => {
    const map = new Map<string, AccountFunding>();
    for (const f of snapshot?.funding.predicted ?? []) map.set(f.coin, f);
    return map;
  }, [snapshot?.funding.predicted]);

  // Total funding accumulated across all open positions since they were opened (from HL live data).
  const totalCumFunding = useMemo(() => {
    let total = 0;
    let hasData = false;
    for (const pos of snapshot?.positions ?? []) {
      const cf = cumFundingSinceOpen(pos);
      if (cf != null) { total += cf; hasData = true; }
    }
    return hasData ? total : null;
  }, [snapshot?.positions]);

  // Estimated daily funding impact based on current rates.
  const totalDailyFunding = useMemo(() => {
    let total = 0;
    let hasData = false;
    for (const pos of snapshot?.positions ?? []) {
      const f = fundingByCoin.get(pos.coin ?? "");
      const pnl = dailyFundingPnl(pos, f?.hyperliquid?.funding_rate ?? null);
      if (pnl != null) { total += pnl; hasData = true; }
    }
    return hasData ? total : null;
  }, [snapshot?.positions, fundingByCoin]);

  const isInitialLoading = snapshot === undefined;
  const hasDataError = snapshot === null;
  const isStale = snapshot != null && isFetching;

  return (
    <div className="space-y-6 px-7 py-7">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold tracking-tight text-fg">Account monitor</h1>
            {isStale && (
              <span className="inline-flex items-center gap-1 rounded-full bg-fg-4/20 px-2 py-0.5 text-[10px] font-medium text-fg-3">
                <RefreshCw className="h-2.5 w-2.5 animate-spin" />
                Aggiornamento
              </span>
            )}
          </div>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-fg-3">
            <span className="font-mono uppercase">{health.network}</span>
            <span className="text-fg-4">·</span>
            <span className="font-mono">{snapshot?.account.address ?? "account non configurato"}</span>
            <span className="text-fg-4">·</span>
            <span>{snapshot?.checked_at ? fmt.relTime(snapshot.checked_at) : "mai aggiornato"}</span>
            <span className="text-fg-4">·</span>
            <span className="inline-flex items-center gap-1">
              <StatusDot status={statusToDot(snapshot?.api.status ?? "loading")} size={6} />
              <span className="font-mono">API {snapshot?.api.latency_ms != null ? `${snapshot.api.latency_ms}ms` : "—"}</span>
            </span>
            <span className="text-fg-4">·</span>
            <span className="inline-flex items-center gap-1">
              <StatusDot status={statusToDot(ws.status)} size={6} />
              <span className="font-mono">WS {ws.messages > 0 ? `${ws.messages} msg` : ws.status}</span>
            </span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={reconcileExitData}
            disabled={action.key === "reconcile"}
            title="Riconcilia dati di uscita con lo storico HL"
          >
            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
            {action.key === "reconcile" ? "Riconcilio..." : "Riconcilia storico"}
          </Button>
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", isFetching && "animate-spin")} />
            {isFetching ? "Sync..." : "Refresh"}
          </Button>
        </div>
      </div>

      {/* Banners */}
      {hasDataError && (
        <Card className="border-danger/40 bg-danger/5 p-4 text-sm text-danger">
          Snapshot account non disponibile. Verifica la connessione al backend.
        </Card>
      )}
      {action.error && (
        <Card className="border-danger/40 bg-danger/5 p-4 text-sm text-danger">{action.error}</Card>
      )}
      {action.message && (
        <Card className="border-success/40 bg-success/5 p-4 text-sm text-success">{action.message}</Card>
      )}
      {(snapshot?.api.error || snapshot?.account.error) && (
        <Card className="border-warn/40 bg-warn/5 p-4 text-sm text-warn">
          {snapshot?.api.error || snapshot?.account.error}
        </Card>
      )}
      {positionsNeedingIntervention.length > 0 && (
        <Card className="border-warn/40 bg-warn/5 p-4 text-sm text-warn">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div className="space-y-1">
              <div className="font-medium">
                {positionsNeedingIntervention.length} posizioni richiedono intervento sulle protezioni
              </div>
              <div className="text-xs text-warn/90">
                {positionsNeedingIntervention
                  .map((position) => `${position.coin}: ${position.protection?.summary ?? "repair richiesto"}`)
                  .join(" • ")}
              </div>
            </div>
          </div>
        </Card>
      )}

      {isInitialLoading && (
        <Card className="p-8 text-center text-sm text-fg-3">Caricamento dati account…</Card>
      )}

      {!isInitialLoading && (
        <>
          {/* KPI cards */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <KpiTile
              label="Equity"
              value={fmtUsd(snapshot?.account.effective_account_value ?? snapshot?.account.account_value)}
              hint={`Withdrawable ${fmtUsd(snapshot?.account.withdrawable)}`}
            />
            <KpiTile
              label="Unrealized PnL"
              value={fmtUsd(totalUnrealized)}
              tone={(totalUnrealized ?? 0) >= 0 ? "success" : "danger"}
              hint={`${tradingSummary.longPositions} long · ${tradingSummary.shortPositions} short`}
            />
            <KpiTile
              label="Margin used"
              value={fmtUsd(snapshot?.account.total_margin_used)}
              hint={marginRatio != null ? `Utilization ${marginRatio.toFixed(1)}%` : "—"}
              progress={marginRatio}
              progressTone={
                marginRatio == null ? "muted" : marginRatio > 80 ? "danger" : marginRatio > 50 ? "warn" : "success"
              }
            />
            <KpiTile
              label="Unified Account Ratio"
              value={fmtPct(snapshot?.account.unified_account_ratio_pct)}
              tone={unifiedAccountRatioTone}
              hint={accountRatioHint(
                snapshot?.account.unified_account_ratio_status,
                snapshot?.account.unified_account_ratio_threshold_pct,
              )}
              tooltip="HL indica rischio liquidazione oltre 95%; a 90% Trado blocca nuove aperture, a 95% avvia de-risk."
              progress={unifiedAccountRatio}
              progressTone={unifiedAccountRatioTone}
            />
            <KpiTile
              label="Notional"
              value={fmtUsd(snapshot?.account.total_notional_position)}
              hint={`${snapshot?.account.open_orders ?? 0} ordini open`}
            />
          </div>

          {/* Equity Line + Drawdown */}
          <AccountEquityPanel
            history={equityHistory ?? null}
            days={equityDays}
            onDaysChange={setEquityDays}
          />

          {/* Margin Analysis + P&L/Funding */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <CrossMarginHealth account={snapshot?.account ?? null} />
            <PnlFundingPanel tradingSummary={tradingSummary} totalDailyFunding={totalDailyFunding} totalCumFunding={totalCumFunding} />
          </div>

          {/* Unified Positions + Funding table */}
          <UnifiedPositionsTable
            positions={snapshot?.positions ?? []}
            fundingByCoin={fundingByCoin}
            midPrices={midPrices}
            actionKey={action.key}
            selectedCoin={selectedCoin}
            onSelectCoin={(coin) => setSelectedCoin((prev) => (prev === coin ? null : coin))}
            onRepair={repairProtection}
            onClose={(coin, pnlInfo) =>
              openConfirm(
                `Chiudere posizione ${coin}?`,
                "Verrà inviato un ordine market reduce-only. L'operazione è irreversibile.",
                pnlInfo,
                `close-position-${coin}`,
                `/api/account/positions/${encodeURIComponent(coin)}/close`,
              )
            }
          />

          {/* Spot + Orders */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <SpotBalances rows={snapshot?.spot_balances ?? []} />
            <OpenOrders
              rows={snapshot?.open_orders ?? []}
              positions={snapshot?.positions ?? []}
              filterCoin={selectedCoin}
              onClearFilter={() => setSelectedCoin(null)}
              actionKey={action.key}
              onCancel={(order) => {
                const oid = orderId(order);
                const coin = String(order.coin ?? "");
                if (!oid || !coin) return;
                openConfirm(
                  `Cancellare ordine ${oid}?`,
                  `${coin} · ${orderSideLabel(order)} · ${orderTypeLabel(order)}`,
                  null,
                  `cancel-order-${oid}`,
                  `/api/account/orders/${encodeURIComponent(oid)}/cancel?coin=${encodeURIComponent(coin)}`,
                );
              }}
            />
          </div>

          {/* Debug collapsible */}
          <div>
            <button
              className="flex items-center gap-1 text-[11px] text-fg-3 transition-colors hover:text-fg"
              onClick={() => setDebugOpen((p) => !p)}
            >
              {debugOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              Debug connessioni
            </button>
            {debugOpen && (
              <Card className="mt-2 space-y-3 p-4">
                <ConnectionRow label="Info API" value={snapshot?.api.info_url ?? "-"} status={snapshot?.api.status ?? "loading"} />
                <ConnectionRow label="WS API" value={snapshot?.api.ws_url ?? wsUrl(health.network)} status={ws.status} />
                <ConnectionRow
                  label="Markets"
                  value={`${snapshot?.api.universe_count ?? "-"} universe · ${snapshot?.api.mids_count ?? "-"} mids`}
                  status={snapshot?.api.status ?? "loading"}
                />
                {ws.error && <div className="text-xs text-danger">{ws.error}</div>}
              </Card>
            )}
          </div>
        </>
      )}

      {/* Confirm Dialog */}
      <Dialog open={confirmDialog.open} onOpenChange={(open: boolean) => { if (!open) setConfirmDialog(CONFIRM_CLOSED); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{confirmDialog.title}</DialogTitle>
            <DialogDescription>{confirmDialog.description}</DialogDescription>
          </DialogHeader>
          {confirmDialog.detail && (
            <div className="rounded-md bg-bg-2 px-4 py-3 font-mono text-sm text-fg-2">
              {confirmDialog.detail}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDialog(CONFIRM_CLOSED)}>
              Annulla
            </Button>
            <Button variant="destructive" onClick={confirmDialog.onConfirm}>
              Conferma
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ── AccountEquityPanel ────────────────────────────────────────────────────────

const CHART_COLORS = {
  equity: "#3ecf8e",
  drawdown: "#f26d6d",
  grid: "#1d1d22",
  axis: "#4b4b53",
  tooltipBg: "#16161a",
  fg: "#e9e9ec",
  fgMuted: "#a4a4ad",
};

const DAYS_OPTIONS = [7, 14, 30, 90] as const;

function AccountEquityPanel({
  history,
  days,
  onDaysChange,
}: {
  history: AccountEquityHistory | null;
  days: number;
  onDaysChange: (d: number) => void;
}) {
  const points = useMemo(() => history?.points ?? [], [history]);
  const maxDd = history?.max_drawdown_pct ?? 0;
  const peak = history?.peak_equity;
  const current = history?.current_equity;
  const currentDd = points.at(-1)?.drawdown_pct ?? 0;

  const chartData = useMemo(
    () =>
      points.map((p) => ({
        label: new Intl.DateTimeFormat("it-IT", { day: "2-digit", month: "2-digit" }).format(
          new Date(p.ts),
        ),
        equity: p.equity,
        drawdown: -p.drawdown_pct,
      })),
    [points],
  );

  const equityMin = useMemo(() => {
    if (chartData.length === 0) return 0;
    const min = Math.min(...chartData.map((d) => d.equity));
    const max = Math.max(...chartData.map((d) => d.equity));
    const pad = (max - min) * 0.05 || 1;
    return Math.floor(min - pad);
  }, [chartData]);

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-6 py-4">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Equity line &amp; Drawdown
            </span>
            {history?.source === "local" && (
              <span className="rounded-full bg-warn/10 px-1.5 py-0.5 text-[9px] font-medium text-warn" title="Dati HL non disponibili — usando snapshot locali">
                local
              </span>
            )}
          </div>
          {current != null && (
            <span className="font-mono text-sm font-semibold text-fg">{fmtUsd(current)}</span>
          )}
          {peak != null && current != null && (
            <span className="font-mono text-xs text-fg-3">
              peak {fmtUsd(peak)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* Max drawdown badge */}
          <span
            className={cn(
              "rounded-full px-2 py-0.5 font-mono text-[10px] font-medium",
              maxDd === 0
                ? "bg-fg-4/20 text-fg-3"
                : maxDd > 15
                  ? "bg-danger/10 text-danger"
                  : maxDd > 8
                    ? "bg-warn/10 text-warn"
                    : "bg-success/10 text-success",
            )}
            title="Max drawdown nel periodo"
          >
            Max DD {maxDd.toFixed(2)}%
          </span>
          {currentDd > 0 && (
            <span
              className={cn(
                "rounded-full px-2 py-0.5 font-mono text-[10px] font-medium",
                currentDd > 15
                  ? "bg-danger/10 text-danger"
                  : currentDd > 8
                    ? "bg-warn/10 text-warn"
                    : "bg-success/10 text-success",
              )}
              title="Drawdown corrente dal picco"
            >
              Attuale {currentDd.toFixed(2)}%
            </span>
          )}
          {/* Days selector */}
          <div className="flex rounded-md border border-border overflow-hidden">
            {DAYS_OPTIONS.map((d) => (
              <button
                key={d}
                onClick={() => onDaysChange(d)}
                className={cn(
                  "px-2.5 py-1 text-[10px] font-mono font-medium transition-colors",
                  d === days
                    ? "bg-fg-4/30 text-fg"
                    : "text-fg-3 hover:text-fg",
                )}
              >
                {d}d
              </button>
            ))}
          </div>
        </div>
      </div>

      {chartData.length === 0 ? (
        <div className="grid h-40 place-items-center text-xs text-fg-3">
          Nessun snapshot equity disponibile per il periodo.
        </div>
      ) : (
        <div className="p-4" style={{ height: 260 }}>
          <RResponsiveContainer width="100%" height="100%">
            <RComposedChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={CHART_COLORS.equity} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={CHART_COLORS.equity} stopOpacity={0} />
                </linearGradient>
                <linearGradient id="ddFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={CHART_COLORS.drawdown} stopOpacity={0} />
                  <stop offset="100%" stopColor={CHART_COLORS.drawdown} stopOpacity={0.3} />
                </linearGradient>
              </defs>
              <RCartesianGrid
                stroke={CHART_COLORS.grid}
                strokeDasharray="2 4"
                vertical={false}
              />
              <RXAxis
                dataKey="label"
                stroke={CHART_COLORS.axis}
                fontSize={10}
                tickLine={false}
                axisLine={false}
                interval="preserveStartEnd"
              />
              <RYAxis
                yAxisId="eq"
                stroke={CHART_COLORS.axis}
                fontSize={10}
                tickLine={false}
                axisLine={false}
                width={56}
                domain={[equityMin, "auto"]}
                tickFormatter={(v: number) =>
                  v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)
                }
              />
              <RYAxis
                yAxisId="dd"
                orientation="right"
                stroke={CHART_COLORS.axis}
                fontSize={10}
                tickLine={false}
                axisLine={false}
                width={38}
                domain={[-(maxDd * 1.4 || 5), 0]}
                tickFormatter={(v: number) => `${Math.abs(v).toFixed(1)}%`}
              />
              <RTooltip
                cursor={{ stroke: "#2b2b32", strokeDasharray: "2 4" }}
                contentStyle={{
                  background: CHART_COLORS.tooltipBg,
                  border: `1px solid ${CHART_COLORS.grid}`,
                  borderRadius: 8,
                  fontSize: 12,
                  color: CHART_COLORS.fg,
                }}
                labelStyle={{ color: CHART_COLORS.fgMuted, fontSize: 10, textTransform: "uppercase" }}
                formatter={(value: number, name: string) => {
                  if (name === "equity") return [fmtUsd(value), "Equity"];
                  return [`${Math.abs(value).toFixed(2)}%`, "Drawdown"];
                }}
              />
              <RArea
                yAxisId="eq"
                type="monotone"
                dataKey="equity"
                stroke={CHART_COLORS.equity}
                strokeWidth={1.8}
                fill="url(#eqFill)"
                dot={false}
                activeDot={{ r: 3, fill: CHART_COLORS.equity }}
                isAnimationActive={false}
              />
              <RArea
                yAxisId="dd"
                type="monotone"
                dataKey="drawdown"
                stroke={CHART_COLORS.drawdown}
                strokeWidth={1.2}
                fill="url(#ddFill)"
                dot={false}
                activeDot={{ r: 3, fill: CHART_COLORS.drawdown }}
                isAnimationActive={false}
              />
            </RComposedChart>
          </RResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

// ── CrossMarginHealth ─────────────────────────────────────────────────────────

function CrossMarginHealth({ account }: { account: AccountMonitor["account"] | null }) {
  const crossAccountValue = asNumber(account?.cross_account_value);
  const crossMaintMargin = asNumber(account?.cross_maintenance_margin_used);
  const crossMarginUsed = asNumber(account?.cross_margin_used);
  const crossNtlPos = asNumber(account?.cross_ntl_pos);
  const unifiedAccountRatio = asNumber(account?.unified_account_ratio_pct);

  const crossBuffer =
    crossAccountValue != null && crossMaintMargin != null && crossAccountValue > 0
      ? Math.max(0, Math.min(100, ((crossAccountValue - crossMaintMargin) / crossAccountValue) * 100))
      : null;

  const crossLeverage =
    crossNtlPos != null && crossAccountValue != null && crossAccountValue > 0
      ? crossNtlPos / crossAccountValue
      : null;

  const bufferTone =
    crossBuffer == null
      ? ("muted" as const)
      : crossBuffer < 20
        ? ("danger" as const)
        : crossBuffer < 50
          ? ("warn" as const)
          : ("success" as const);

  const hasPositions = (crossNtlPos ?? 0) > 0;

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">Cross Margin Health</span>
        {crossBuffer != null && (
          <span
            className={cn(
              "font-mono text-sm font-semibold",
              bufferTone === "success" && "text-success",
              bufferTone === "warn" && "text-warn",
              bufferTone === "danger" && "text-danger",
              bufferTone === "muted" && "text-fg-3",
            )}
          >
            {crossBuffer.toFixed(1)}% buffer
          </span>
        )}
      </div>
      <div className="space-y-5 p-6">
        {hasPositions ? (
          <div>
            <div className="mb-1.5 flex justify-between text-[10px] text-fg-3">
              <span>← Liquidazione</span>
              <span>Sicuro →</span>
            </div>
            <Progress
              value={crossBuffer ?? 0}
              className="h-2.5"
              indicatorClassName={cn(
                bufferTone === "success" && "bg-success",
                bufferTone === "warn" && "bg-warn",
                bufferTone === "danger" && "bg-danger",
                bufferTone === "muted" && "bg-fg-3",
              )}
            />
            {crossBuffer != null && crossBuffer < 20 && (
              <p className="mt-2 text-xs font-medium text-danger">
                ⚠ Vicino alla liquidazione — considera di ridurre l&apos;esposizione
              </p>
            )}
          </div>
        ) : (
          <p className="text-sm text-fg-3">Nessuna posizione cross aperta</p>
        )}
        <div className="grid grid-cols-3 gap-4">
          <Metric label="Cross equity" value={fmtUsd(account?.cross_account_value)} />
          <Metric label="Maint. margin" value={fmtUsd(account?.cross_maintenance_margin_used)} />
          <Metric
            label="Leva cross"
            value={crossLeverage != null ? `${crossLeverage.toFixed(2)}x` : "-"}
            tone={crossLeverage != null && crossLeverage > 10 ? "short" : crossLeverage != null && crossLeverage > 5 ? "warn" : undefined}
          />
          <Metric label="Cross margin" value={fmtUsd(crossMarginUsed)} />
          <Metric
            label="Unified ratio"
            value={fmtPct(account?.unified_account_ratio_pct)}
            tone={unifiedAccountRatio != null && unifiedAccountRatio >= 90 ? "short" : unifiedAccountRatio != null && unifiedAccountRatio >= 80 ? "warn" : undefined}
          />
          <Metric label="Withdrawable" value={fmtUsd(account?.withdrawable)} />
        </div>
      </div>
    </Card>
  );
}

// ── PnlFundingPanel ───────────────────────────────────────────────────────────

function PnlFundingPanel({
  tradingSummary,
  totalDailyFunding,
  totalCumFunding,
}: {
  tradingSummary: TradingSummary;
  totalDailyFunding: number | null;
  totalCumFunding: number | null;
}) {
  return (
    <Card className="overflow-hidden">
      <div className="border-b border-border px-6 py-4">
        <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">P&amp;L &amp; Funding</span>
      </div>
      <div className="p-6">
        <div className="grid grid-cols-2 gap-x-6 gap-y-4">
          <Metric
            label={`Long PnL (${tradingSummary.longPositions})`}
            value={fmtUsd(tradingSummary.longPnl)}
            tone={tradingSummary.longPnl != null ? (tradingSummary.longPnl >= 0 ? "long" : "short") : undefined}
          />
          <Metric
            label={`Short PnL (${tradingSummary.shortPositions})`}
            value={fmtUsd(tradingSummary.shortPnl)}
            tone={tradingSummary.shortPnl != null ? (tradingSummary.shortPnl >= 0 ? "long" : "short") : undefined}
          />
          <Metric
            label="Unrealized totale"
            value={fmtUsd(tradingSummary.openPnl)}
            tone={tradingSummary.openPnl != null ? (tradingSummary.openPnl >= 0 ? "long" : "short") : undefined}
          />
          <Metric
            label="P&L atteso (TP/limit)"
            value={fmtUsd(tradingSummary.expectedLimitPnl)}
            tone={tradingSummary.expectedLimitPnl != null ? (tradingSummary.expectedLimitPnl >= 0 ? "long" : "short") : undefined}
          />
          <Metric
            label="Funding accumulato"
            value={totalCumFunding != null ? fmtUsdPrecise(totalCumFunding) : "-"}
            tone={totalCumFunding != null ? (totalCumFunding >= 0 ? "long" : "short") : undefined}
          />
          <Metric
            label="Funding stimato/giorno"
            value={totalDailyFunding != null ? fmtUsdPrecise(totalDailyFunding) : "-"}
            tone={totalDailyFunding != null ? (totalDailyFunding >= 0 ? "long" : "short") : undefined}
          />
        </div>
      </div>
    </Card>
  );
}

// ── Shared sub-components ─────────────────────────────────────────────────────

function KpiTile({
  label, value, hint, tooltip, tone, progress, progressTone,
}: {
  label: string;
  value: string;
  hint?: string;
  tooltip?: string;
  tone?: "success" | "danger" | "warn" | "muted";
  progress?: number | null;
  progressTone?: "success" | "warn" | "danger" | "muted";
}) {
  return (
    <Card className="p-5" title={tooltip}>
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-2 font-mono text-2xl font-semibold tabular-nums",
          tone === "success" && "text-success",
          tone === "danger" && "text-danger",
          tone === "warn" && "text-warn",
          (!tone || tone === "muted") && "text-fg",
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-1 text-[11px] text-fg-3">{hint}</div>}
      {progress != null && (
        <Progress
          value={Math.max(0, Math.min(100, progress))}
          className="mt-3"
          indicatorClassName={cn(
            progressTone === "success" && "bg-success",
            progressTone === "warn" && "bg-warn",
            progressTone === "danger" && "bg-danger",
            progressTone === "muted" && "bg-fg-3",
          )}
        />
      )}
    </Card>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-1 truncate font-mono text-sm tabular-nums",
          tone === "long" && "text-success",
          tone === "short" && "text-danger",
          tone === "warn" && "text-warn",
          !tone && "text-fg",
        )}
        title={value}
      >
        {value}
      </div>
    </div>
  );
}

function ConnectionRow({ label, value, status }: { label: string; value: string; status: string }) {
  const dot = statusToDot(status);
  return (
    <div className="grid items-center gap-3" style={{ gridTemplateColumns: "100px 1fr auto" }}>
      <span className="text-[11px] uppercase tracking-wider text-fg-3">{label}</span>
      <span className="truncate font-mono text-xs text-fg-2" title={value}>{value}</span>
      <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-fg-2">
        <StatusDot status={dot} size={6} />
        {status}
      </span>
    </div>
  );
}

function SideBadge({ side }: { side: string | null | undefined }) {
  const normalized = side?.toLowerCase();
  if (normalized === "long") return <span className="dir mono long">↑ LONG</span>;
  if (normalized === "short") return <span className="dir mono short">↓ SHORT</span>;
  return <span className="badge muted">-</span>;
}

// ── UnifiedPositionsTable ─────────────────────────────────────────────────────

type PositionSortKey = "coin" | "side" | "leverage" | "size" | "entry" | "value" | "pnl" | "roe" | "liq";

function positionSortValue(row: AccountPosition, key: PositionSortKey): string | number {
  switch (key) {
    case "coin": return row.coin ?? "";
    case "side": return row.side ?? "";
    case "leverage": return asNumber((row.leverage as Record<string, string | number | null> | null)?.value) ?? 0;
    case "size": return Math.abs(asNumber(row.size) ?? 0);
    case "entry": return asNumber(row.entry_price) ?? 0;
    case "value": return asNumber(row.position_value) ?? 0;
    case "pnl": return asNumber(row.unrealized_pnl) ?? 0;
    case "roe": return asNumber(row.return_on_equity) ?? 0;
    case "liq": return asNumber(row.liquidation_price) ?? 0;
  }
}

function SortTh({
  label, sortKey, current, dir, onSort,
}: {
  label: string;
  sortKey: PositionSortKey;
  current: PositionSortKey;
  dir: "asc" | "desc";
  onSort: (key: PositionSortKey) => void;
}) {
  const active = current === sortKey;
  return (
    <th style={{ cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" }} onClick={() => onSort(sortKey)}>
      {label}{" "}
      <span style={{ opacity: active ? 1 : 0.25, fontSize: 9 }}>
        {active ? (dir === "asc" ? "▲" : "▼") : "▲"}
      </span>
    </th>
  );
}

function UnifiedPositionsTable({
  positions,
  fundingByCoin,
  midPrices,
  actionKey,
  selectedCoin,
  onSelectCoin,
  onRepair,
  onClose,
}: {
  positions: AccountPosition[];
  fundingByCoin: Map<string, AccountFunding>;
  midPrices: Record<string, number>;
  actionKey: string | null;
  selectedCoin: string | null;
  onSelectCoin: (coin: string) => void;
  onRepair: (coin: string) => void;
  onClose: (coin: string, pnlInfo: string | null) => void;
}) {
  const [sortKey, setSortKey] = useState<PositionSortKey>("pnl");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  function handleSort(key: PositionSortKey) {
    if (key === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir("desc"); }
  }

  const sorted = useMemo(() => {
    return [...positions].sort((a, b) => {
      const av = positionSortValue(a, sortKey);
      const bv = positionSortValue(b, sortKey);
      const cmp = typeof av === "string" ? av.localeCompare(bv as string) : (av as number) - (bv as number);
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [positions, sortKey, sortDir]);

  const thProps = { current: sortKey, dir: sortDir, onSort: handleSort };

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Posizioni perpetual</span>
        <span className="badge muted">{positions.length}</span>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="tbl" style={{ minWidth: 1080 }}>
          <thead>
            <tr>
              <SortTh label="Coin" sortKey="coin" {...thProps} />
              <SortTh label="Dir" sortKey="side" {...thProps} />
              <SortTh label="Leva" sortKey="leverage" {...thProps} />
              <SortTh label="Size" sortKey="size" {...thProps} />
              <SortTh label="Entry" sortKey="entry" {...thProps} />
              <SortTh label="Value" sortKey="value" {...thProps} />
              <SortTh label="PnL" sortKey="pnl" {...thProps} />
              <SortTh label="ROE" sortKey="roe" {...thProps} />
              <SortTh label="Liq" sortKey="liq" {...thProps} />
              <th style={{ whiteSpace: "nowrap" }} title="Distanza % tra il prezzo mid live e il liquidationPx (isolated). Non è la distanza dall'entry — se il prezzo si è mosso verso la liquidazione, questa percentuale può essere piccola anche con alta leva.">Dist. liq ⓘ</th>
              <th style={{ whiteSpace: "nowrap" }}>FR 8h</th>
              <th style={{ whiteSpace: "nowrap" }}>FR/giorno</th>
              <th style={{ whiteSpace: "nowrap" }}>Fund. acc.</th>
              <th style={{ whiteSpace: "nowrap" }}>Protect</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, index) => {
              const coin = row.coin ?? "";
              const pnl = asNumber(row.unrealized_pnl);
              const roe = asNumber(row.return_on_equity);
              const running = actionKey === `close-position-${coin}`;
              const isSelected = selectedCoin === coin;
              const funding = fundingByCoin.get(coin);
              const frRate = funding?.hyperliquid?.funding_rate ?? null;
              const liqDist = liquidationDistance(row, midPrices);
              const frDaily = dailyFundingPnl(row, frRate);
              const cumFunding = cumFundingSinceOpen(row);
              const pnlInfo = pnl != null ? `PnL attuale: ${fmtUsd(pnl)}` : null;
              const protection = row.protection ?? null;
              const repairRunning = actionKey === `repair-protection-${coin}`;
              const repairTone = protectionTone(protection);

              // Bar fill: more fill = closer to liq (more dangerous). Caps at 30% distance = safe.
              const liqBarFill = liqDist != null ? Math.max(0, Math.min(100, (1 - liqDist / 30) * 100)) : null;
              const liqTone = liqDist == null ? null : liqDist < 10 ? "danger" : liqDist < 20 ? "warn" : "ok";

              return (
                <tr
                  key={`${coin}-${index}`}
                  onClick={() => { if (coin) onSelectCoin(coin); }}
                  style={{
                    cursor: coin ? "pointer" : undefined,
                    background: isSelected ? "var(--bg-2, rgba(255,255,255,0.05))" : undefined,
                    outline: isSelected ? "1px solid var(--border)" : undefined,
                  }}
                >
                  <td className="mono" style={{ fontWeight: isSelected ? 600 : undefined }}>{coin || "-"}</td>
                  <td><SideBadge side={row.side} /></td>
                  <td className="mono">{leverageLabel(row.leverage)}</td>
                  <td className="mono">{fmtNum(row.size)}</td>
                  <td className="mono">{fmt.price(row.entry_price)}</td>
                  <td className="mono">{fmtUsd(row.position_value)}</td>
                  <td className="mono" style={{ color: (pnl ?? 0) >= 0 ? "var(--long)" : "var(--short)" }}>
                    {fmtUsd(pnl)}
                  </td>
                  <td className="mono" style={{ color: (roe ?? 0) >= 0 ? "var(--long)" : "var(--short)" }}>
                    {fmtPct(roe != null ? roe * 100 : null)}
                  </td>
                  <td className="mono">{fmt.price(row.liquidation_price)}</td>
                  <td>
                    {liqDist != null ? (
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <div style={{ width: 36, height: 4, borderRadius: 2, overflow: "hidden", background: "var(--border)" }}>
                          <div
                            style={{
                              width: `${liqBarFill}%`,
                              height: "100%",
                              transition: "width 0.3s ease",
                              background:
                                liqTone === "danger" ? "var(--danger)"
                                : liqTone === "warn" ? "var(--warn)"
                                : "var(--success)",
                            }}
                          />
                        </div>
                        <span
                          className="mono"
                          style={{
                            fontSize: 11,
                            color:
                              liqTone === "danger" ? "var(--danger)"
                              : liqTone === "warn" ? "var(--warn)"
                              : "var(--fg-3)",
                          }}
                        >
                          {liqDist.toFixed(1)}%
                        </span>
                      </div>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="mono" style={{ fontSize: 11, color: "var(--fg-3)" }}>
                    {fmtFunding(frRate)}
                  </td>
                  <td
                    className="mono"
                    style={{ color: frDaily != null ? (frDaily >= 0 ? "var(--long)" : "var(--short)") : undefined }}
                  >
                    {frDaily != null ? fmtUsdPrecise(frDaily) : "—"}
                  </td>
                  <td
                    className="mono"
                    style={{ color: cumFunding != null ? (cumFunding >= 0 ? "var(--long)" : "var(--short)") : undefined }}
                  >
                    {cumFunding != null ? fmtUsdPrecise(cumFunding) : "—"}
                  </td>
                  <td style={{ minWidth: 170 }}>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      <span
                        className="mono"
                        style={{
                          fontSize: 11,
                          color:
                            repairTone === "warn" ? "var(--warn)"
                            : repairTone === "ok" ? "var(--success)"
                            : "var(--fg-3)",
                        }}
                        title={protection?.summary ?? "No local Trado live position linked"}
                      >
                        {protectionLabel(protection)}
                      </span>
                      <span className="muted" style={{ fontSize: 11, lineHeight: 1.3 }}>
                        {protection?.summary ?? "Nessuna posizione Trado collegata"}
                      </span>
                    </div>
                  </td>
                  <td style={{ textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                    <div style={{ display: "flex", justifyContent: "flex-end", gap: 6 }}>
                      {protection?.needs_intervention && protection.repairable && (
                        <button
                          className="btn ghost"
                          disabled={!coin || repairRunning}
                          title="Prova a ripristinare gli ordini protettivi mancanti"
                          onClick={() => onRepair(coin)}
                          style={{ minHeight: 26, padding: "0 8px" }}
                        >
                          <RotateCcw className={cn("h-3.5 w-3.5", repairRunning && "animate-spin")} />
                          <span>{repairRunning ? "..." : "Repair"}</span>
                        </button>
                      )}
                      <button
                        className="btn ghost"
                        disabled={!coin || running}
                        title="Chiudi posizione market"
                        onClick={() => onClose(coin, pnlInfo)}
                        style={{ minHeight: 26, padding: "0 8px" }}
                      >
                        <Icon name="x" size={12} /> {running ? "..." : "Chiudi"}
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
            {positions.length === 0 && (
              <tr><td colSpan={15} className="muted">Nessuna posizione perp aperta.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── SpotBalances ──────────────────────────────────────────────────────────────

function SpotBalances({ rows }: { rows: AccountSpotBalance[] }) {
  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Spot balances</span>
        <span className="badge muted">{rows.length}</span>
      </div>
      <table className="tbl">
        <thead><tr><th>Coin</th><th>Total</th><th>Hold</th><th>Entry NTL</th></tr></thead>
        <tbody>
          {rows.slice(0, 12).map((row) => (
            <tr key={row.coin}>
              <td className="mono">{row.coin ?? "-"}</td>
              <td className="mono">{fmtNum(row.total)}</td>
              <td className="mono">{fmtNum(row.hold)}</td>
              <td className="mono">{fmtUsd(row.entry_ntl)}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={4} className="muted">Nessun balance spot.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

// ── OpenOrders ────────────────────────────────────────────────────────────────

const ORDERS_PAGE_SIZE = 12;

function OpenOrders({
  rows, positions, filterCoin, onClearFilter, actionKey, onCancel,
}: {
  rows: AccountOpenOrder[];
  positions: AccountPosition[];
  filterCoin: string | null;
  onClearFilter: () => void;
  actionKey: string | null;
  onCancel: (order: AccountOpenOrder) => void;
}) {
  const [page, setPage] = useState(0);
  const positionsMap = useMemo(() => positionByCoin(positions), [positions]);

  const filtered = useMemo(
    () => filterCoin ? rows.filter((r) => String(r.coin ?? "") === filterCoin) : rows,
    [rows, filterCoin],
  );

  const totalPages = Math.max(1, Math.ceil(filtered.length / ORDERS_PAGE_SIZE));
  const pageRows = filtered.slice(page * ORDERS_PAGE_SIZE, (page + 1) * ORDERS_PAGE_SIZE);

  useEffect(() => { setPage(0); }, [filtered.length, filterCoin]);

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Open orders</span>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {filterCoin && (
            <span className="badge warn" style={{ display: "flex", alignItems: "center", gap: 4 }}>
              {filterCoin}
              <button
                onClick={onClearFilter}
                style={{ background: "none", border: "none", cursor: "pointer", padding: 0, lineHeight: 1, color: "inherit" }}
                title="Rimuovi filtro"
              >×</button>
            </span>
          )}
          <span className="badge muted">{filterCoin ? `${filtered.length}/${rows.length}` : rows.length}</span>
        </div>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="tbl" style={{ minWidth: 760 }}>
          <thead>
            <tr>
              <th>Coin</th>
              <th>Direzione</th>
              <th>Tipo</th>
              <th>Size</th>
              <th>Prezzo</th>
              <th>Notional</th>
              <th>P&amp;L previsto</th>
              <th>Ora</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row, index) => {
              const oid = orderId(row);
              const coin = String(row.coin ?? "");
              const side = orderSide(row);
              const position = positionsMap.get(coin);
              const pnl = expectedLimitPnl(row, position);
              const size = orderEffectiveSz(row, asNumber(position?.size));
              const limit = orderEffectivePx(row);
              const notional = size != null && limit != null ? Math.abs(size * limit) : null;
              const isTrigger = orderTriggerPx(row) != null;
              const running = oid != null && actionKey === `cancel-order-${oid}`;
              return (
                <tr key={`${String(row.oid ?? index)}`}>
                  <td className="mono">{coin || "-"}</td>
                  <td>
                    <span className={"dir mono " + (side === "buy" ? "long" : side === "sell" ? "short" : "neutral")}>
                      {side === "buy" ? "↑" : side === "sell" ? "↓" : "—"} {orderSideLabel(row)}
                    </span>
                  </td>
                  <td><span className={"badge " + (row.reduceOnly === true ? "warn" : "muted")}>{orderTypeLabel(row)}</span></td>
                  <td className="mono">{fmtNum(size)}</td>
                  <td className="mono">
                    {fmt.price(limit)}
                    {isTrigger && <span className="badge muted" style={{ marginLeft: 4, fontSize: 9 }}>trigger</span>}
                  </td>
                  <td className="mono">{fmtUsd(notional)}</td>
                  <td className="mono" style={{ color: pnl == null ? "var(--fg-3)" : pnl >= 0 ? "var(--long)" : "var(--short)" }}>
                    {pnl == null ? "n/a" : fmtUsd(pnl)}
                  </td>
                  <td className="mono muted">{fmtTime(row.timestamp)}</td>
                  <td style={{ textAlign: "right" }}>
                    <button
                      className="btn ghost"
                      disabled={!oid || !coin || running}
                      title="Cancella ordine"
                      onClick={() => onCancel(row)}
                      style={{ minHeight: 26, padding: "0 8px" }}
                    >
                      <Icon name="x" size={12} /> {running ? "..." : "Cancella"}
                    </button>
                  </td>
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={9} className="muted">
                  {filterCoin ? `Nessun ordine aperto per ${filterCoin}.` : "Nessun ordine aperto."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8, padding: "8px 14px", borderTop: "1px solid var(--border)" }}>
          <span className="muted" style={{ fontSize: "var(--fz-xs)" }}>
            {page * ORDERS_PAGE_SIZE + 1}–{Math.min((page + 1) * ORDERS_PAGE_SIZE, filtered.length)} di {filtered.length}
          </span>
          <button className="btn ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)} style={{ minHeight: 26, padding: "0 8px" }}>
            ‹ Prec
          </button>
          <button className="btn ghost" disabled={page >= totalPages - 1} onClick={() => setPage((p) => p + 1)} style={{ minHeight: 26, padding: "0 8px" }}>
            Succ ›
          </button>
        </div>
      )}
    </div>
  );
}

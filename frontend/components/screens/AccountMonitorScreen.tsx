"use client";

import React, { useEffect, useMemo, useState } from "react";
import { RefreshCw, RotateCcw } from "lucide-react";
import { Icon, fmt } from "@/components/atoms";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { StatusDot, type DotStatus } from "@/components/shell/StatusDot";
import { cn } from "@/lib/utils";
import { AccountFunding, AccountMonitor, AccountOpenOrder, AccountPosition, AccountSpotBalance, Health, apiGet, apiPost } from "@/lib/api";

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
  expectedLimitPnl: number | null;
  openOrderNotional: number;
  longPositions: number;
  shortPositions: number;
  marginRatio: number | null;
};

const WS_INITIAL: WsState = { status: "idle", lastMessageAt: null, messages: 0, error: null };
const ACTION_INITIAL: ActionState = { key: null, message: null, error: null };

function wsUrl(network: Health["network"]) {
  return network === "mainnet" ? "wss://api.hyperliquid.xyz/ws" : "wss://api.hyperliquid-testnet.xyz/ws";
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

function fmtTime(ms: number | null | undefined): string {
  if (!ms) return "-";
  return new Intl.DateTimeFormat("it-IT", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }).format(new Date(ms));
}


function orderNumber(row: AccountOpenOrder, key: "sz" | "origSz" | "limitPx"): number | null {
  return asNumber(row[key]);
}

function orderTriggerPx(row: AccountOpenOrder): number | null {
  // frontendOpenOrders: triggerPx è in orderType.trigger (oggetto annidato)
  const ot = row.orderType;
  if (ot && typeof ot === "object" && "trigger" in (ot as object)) {
    const trigger = (ot as Record<string, unknown>).trigger as Record<string, unknown>;
    const nested = asNumber(trigger?.triggerPx as string | number | null);
    if (nested != null && nested > 0) return nested;
  }
  // fallback: triggerPx come campo top-level (openOrders / altri endpoint)
  const topLevel = asNumber(row.triggerPx);
  if (topLevel != null && topLevel > 0) return topLevel;
  return null;
}

function orderEffectivePx(row: AccountOpenOrder): number | null {
  const triggerPx = orderTriggerPx(row);
  if (triggerPx != null) return triggerPx;
  return orderNumber(row, "limitPx");
}

function orderEffectiveSz(row: AccountOpenOrder, positionSize?: number | null): number | null {
  const sz = orderNumber(row, "sz");
  if (sz != null && sz > 0) return sz;
  const orig = orderNumber(row, "origSz");
  if (orig != null && orig > 0) return orig;
  // per ordini reduce-only senza size fissa (TP/SL su intera posizione)
  if (row.reduceOnly && positionSize != null && positionSize > 0) return positionSize;
  return null;
}

function leverageLabel(leverage: Record<string, unknown> | null | undefined): string {
  if (!leverage) return "-";
  const value = asNumber(leverage.value as string | number | null);
  if (value == null) return "-";
  const type = leverage.type;
  return type === "isolated" ? `${value}x iso` : `${value}x`;
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
  // frontendOpenOrders: orderType è un oggetto, tpsl è dentro trigger
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
  // openOrders: orderType è una stringa
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
  const openPnl = positions.length > 0
    ? positions.reduce((sum, position) => sum + (asNumber(position.unrealized_pnl) ?? 0), 0)
    : null;
  const expectedValues = orders
    .map((order) => expectedLimitPnl(order, positionsMap.get(String(order.coin ?? ""))))
    .filter((value): value is number => value !== null);
  const openOrderNotional = orders.reduce((sum, order) => {
    const pos = positionsMap.get(String(order.coin ?? ""));
    const size = orderEffectiveSz(order, asNumber(pos?.size));
    const limit = orderEffectivePx(order);
    return size != null && limit != null ? sum + Math.abs(size * limit) : sum;
  }, 0);
  const accountValue = positions.reduce((sum, position) => sum + (asNumber(position.position_value) ?? 0), 0);
  const marginUsed = positions.reduce((sum, position) => sum + (asNumber(position.margin_used) ?? 0), 0);
  return {
    openPnl,
    expectedLimitPnl: expectedValues.length > 0 ? expectedValues.reduce((sum, value) => sum + value, 0) : null,
    openOrderNotional,
    longPositions: positions.filter((position) => position.side === "long").length,
    shortPositions: positions.filter((position) => position.side === "short").length,
    marginRatio: accountValue > 0 ? (marginUsed / accountValue) * 100 : null,
  };
}

export function AccountMonitorScreen({ health }: { health: Health }) {
  const [snapshot, setSnapshot] = useState<AccountMonitor | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ws, setWs] = useState<WsState>(WS_INITIAL);
  const [action, setAction] = useState<ActionState>(ACTION_INITIAL);
  const [selectedCoin, setSelectedCoin] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      setSnapshot(await apiGet<AccountMonitor>("/api/account/snapshot"));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Snapshot account non disponibile");
    } finally {
      setLoading(false);
    }
  }

  async function runAccountAction(key: string, confirmMessage: string, actionPath: string) {
    if (!window.confirm(confirmMessage)) return;
    setAction({ key, message: null, error: null });
    try {
      await apiPost<unknown>(actionPath);
      setAction({ key: null, message: "Azione inviata. Snapshot aggiornato.", error: null });
      await refresh();
    } catch (exc) {
      setAction({ key: null, message: null, error: exc instanceof Error ? exc.message : "Azione non riuscita" });
    }
  }

  async function reconcileExitData() {
    setAction({ key: "reconcile", message: null, error: null });
    try {
      const result = await apiPost<{ patched: number; positions: { position_id: number; coin: string; exit_price: string; realized_pnl: string | null; was_wrong_data: boolean }[] }>(
        "/api/account/positions/patch-missing-exit-data"
      );
      const detail = result.positions.length > 0
        ? result.positions.map(p => `${p.coin} exit=${p.exit_price}${p.was_wrong_data ? " (corretto)" : ""}`).join(", ")
        : "nessuna posizione da riconciliare";
      setAction({ key: null, message: `Riconciliazione completata: ${result.patched} posizioni — ${detail}`, error: null });
    } catch (exc) {
      setAction({ key: null, message: null, error: exc instanceof Error ? exc.message : "Riconciliazione non riuscita" });
    }
  }

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const socket = new WebSocket(wsUrl(health.network));
    let active = true;
    setWs({ status: "connecting", lastMessageAt: null, messages: 0, error: null });

    socket.addEventListener("open", () => {
      if (!active) return;
      socket.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids" } }));
      setWs((prev) => ({ ...prev, status: "live" }));
    });
    socket.addEventListener("message", () => {
      if (!active) return;
      setWs((prev) => ({
        ...prev,
        status: "live",
        messages: prev.messages + 1,
        lastMessageAt: new Date().toISOString(),
      }));
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
    () => snapshot?.positions.reduce((sum, position) => sum + (asNumber(position.unrealized_pnl) ?? 0), 0) ?? null,
    [snapshot]
  );
  const tradingSummary = useMemo(
    () => summarizeTrading(snapshot?.positions ?? [], snapshot?.open_orders ?? []),
    [snapshot]
  );
  const accountValueNum = asNumber(
    snapshot?.account.effective_account_value ?? snapshot?.account.account_value,
  );
  const marginUsedNum = asNumber(snapshot?.account.total_margin_used);
  const marginRatio =
    accountValueNum != null && accountValueNum > 0 && marginUsedNum != null
      ? (marginUsedNum / accountValueNum) * 100
      : null;

  return (
    <div className="space-y-6 px-7 py-7">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Account monitor</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-fg-3">
            <span className="font-mono uppercase">{health.network}</span>
            <span className="text-fg-4">·</span>
            <span className="font-mono">{snapshot?.account.address ?? "account non configurato"}</span>
            <span className="text-fg-4">·</span>
            <span>{snapshot?.checked_at ? fmt.relTime(snapshot.checked_at) : "mai aggiornato"}</span>
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
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
            {loading ? "Sync..." : "Refresh"}
          </Button>
        </div>
      </div>

      {/* Banner errori / messaggi */}
      {error && (
        <Card className="border-danger/40 bg-danger/5 p-4 text-sm text-danger">{error}</Card>
      )}
      {action.error && (
        <Card className="border-danger/40 bg-danger/5 p-4 text-sm text-danger">{action.error}</Card>
      )}
      {action.message && (
        <Card className="border-success/40 bg-success/5 p-4 text-sm text-success">
          {action.message}
        </Card>
      )}

      {/* 4 KPI cards principali */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
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
          hint={marginRatio != null ? `Ratio ${marginRatio.toFixed(1)}%` : "—"}
          progress={marginRatio}
          progressTone={
            marginRatio == null
              ? "muted"
              : marginRatio > 80
                ? "danger"
                : marginRatio > 50
                  ? "warn"
                  : "success"
          }
        />
        <KpiTile
          label="Notional"
          value={fmtUsd(snapshot?.account.total_notional_position)}
          hint={`${snapshot?.account.open_orders ?? 0} ordini open`}
        />
      </div>

      {/* 4 status pill (API, WS, Account, Funding) */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatusPill
          title="API"
          status={snapshot?.api.status ?? "loading"}
          detail={`${snapshot?.api.latency_ms ?? "-"} ms`}
        />
        <StatusPill
          title="WebSocket"
          status={ws.status}
          detail={ws.lastMessageAt ? fmt.relTime(ws.lastMessageAt) : `${ws.messages} msg`}
        />
        <StatusPill
          title="Account"
          status={snapshot?.account.configured ? (snapshot.account.error ? "error" : "ok") : "missing"}
          detail={snapshot?.account.address ?? "no address"}
        />
        <StatusPill
          title="Funding"
          status={snapshot?.funding.status ?? "loading"}
          detail={`${snapshot?.funding.predicted.length ?? 0} mercati`}
        />
      </div>

      {/* Riga: Saldo dettaglio + Connessioni */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1.1fr_0.9fr]">
        <Card className="overflow-hidden">
          <div className="flex items-center justify-between border-b border-border px-6 py-4">
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
                Saldo account dettagliato
              </div>
            </div>
            <Badge
              variant="outline"
              className={cn(
                "font-mono uppercase",
                snapshot?.status === "ok" || snapshot?.status === "live"
                  ? "border-success/40 bg-success/10 text-success"
                  : "border-warn/40 bg-warn/10 text-warn",
              )}
            >
              {snapshot?.status ?? "loading"}
            </Badge>
          </div>
          <div className="grid grid-cols-3 gap-5 p-6">
            <Metric label="Equity" value={fmtUsd(snapshot?.account.effective_account_value ?? snapshot?.account.account_value)} />
            <Metric label="Withdrawable" value={fmtUsd(snapshot?.account.withdrawable)} />
            <Metric label="Margin used" value={fmtUsd(snapshot?.account.total_margin_used)} />
            <Metric label="Notional" value={fmtUsd(snapshot?.account.total_notional_position)} />
            <Metric
              label="Unrealized PnL"
              value={fmtUsd(totalUnrealized)}
              tone={(totalUnrealized ?? 0) >= 0 ? "long" : "short"}
            />
            <Metric label="Open orders" value={String(snapshot?.account.open_orders ?? "-")} />
          </div>
          {(snapshot?.api.error || snapshot?.account.error) && (
            <div className="border-t border-border bg-danger/5 px-6 py-3 text-xs text-danger">
              {snapshot.api.error || snapshot.account.error}
            </div>
          )}
        </Card>

        <Card className="overflow-hidden">
          <div className="border-b border-border px-6 py-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
              Connessioni
            </div>
          </div>
          <div className="space-y-3 p-6">
            <ConnectionRow
              label="Info API"
              value={snapshot?.api.info_url ?? "-"}
              status={snapshot?.api.status ?? "loading"}
            />
            <ConnectionRow
              label="WS API"
              value={snapshot?.api.ws_url ?? wsUrl(health.network)}
              status={ws.status}
            />
            <ConnectionRow
              label="Markets"
              value={`${snapshot?.api.universe_count ?? "-"} universe · ${snapshot?.api.mids_count ?? "-"} mids`}
              status={snapshot?.api.status ?? "loading"}
            />
            {ws.error && <div className="text-xs text-danger">{ws.error}</div>}
          </div>
        </Card>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <SummaryWidget
          title="P&L posizioni"
          value={fmtUsd(tradingSummary.openPnl)}
          detail={`${tradingSummary.longPositions} long · ${tradingSummary.shortPositions} short`}
          tone={(tradingSummary.openPnl ?? 0) >= 0 ? "long" : "short"}
        />
        <SummaryWidget
          title="P&L ordini limit"
          value={fmtUsd(tradingSummary.expectedLimitPnl)}
          detail="solo ordini che chiudono posizioni"
          tone={(tradingSummary.expectedLimitPnl ?? 0) >= 0 ? "long" : "short"}
        />
        <SummaryWidget
          title="Notional ordini"
          value={fmtUsd(tradingSummary.openOrderNotional)}
          detail={`${snapshot?.open_orders.length ?? 0} ordini aperti`}
        />
        <SummaryWidget
          title="Leva account"
          value={(() => {
            const ntl = asNumber(snapshot?.account.cross_ntl_pos ?? snapshot?.account.total_notional_position);
            const val = asNumber(snapshot?.account.effective_account_value ?? snapshot?.account.account_value);
            if (ntl == null || val == null || val <= 0) return "-";
            return `${(ntl / val).toFixed(2)}x`;
          })()}
          detail={`${fmtUsd(snapshot?.account.cross_ntl_pos ?? snapshot?.account.total_notional_position)} cross notional`}
          tone={(() => {
            const ntl = asNumber(snapshot?.account.cross_ntl_pos ?? snapshot?.account.total_notional_position);
            const val = asNumber(snapshot?.account.effective_account_value ?? snapshot?.account.account_value);
            if (ntl == null || val == null || val <= 0) return undefined;
            const lev = ntl / val;
            return lev > 10 ? "short" : lev > 5 ? "warn" : undefined;
          })()}
        />
      </div>

      <UnifiedAccountCard account={snapshot?.account ?? null} />

      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 0.8fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <PositionsTable
          rows={snapshot?.positions ?? []}
          actionKey={action.key}
          selectedCoin={selectedCoin}
          onSelectCoin={(coin) => setSelectedCoin((prev) => (prev === coin ? null : coin))}
          onClose={(coin) =>
            runAccountAction(
              `close-position-${coin}`,
              `Chiudere la posizione live ${coin} con ordine market reduce-only?`,
              `/api/account/positions/${encodeURIComponent(coin)}/close`
            )
          }
        />
        <FundingTable rows={snapshot?.funding.predicted ?? []} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)" }}>
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
            runAccountAction(
              `cancel-order-${oid}`,
              `Cancellare l'ordine ${oid} su ${coin}?`,
              `/api/account/orders/${encodeURIComponent(oid)}/cancel?coin=${encodeURIComponent(coin)}`
            );
          }}
        />
      </div>
    </div>
  );
}

function UnifiedAccountCard({ account }: { account: AccountMonitor["account"] | null }) {
  const accountValue = asNumber(account?.effective_account_value ?? account?.account_value);
  const crossAccountValue = asNumber(account?.cross_account_value);
  const crossNtlPos = asNumber(account?.cross_ntl_pos);
  const totalNtl = asNumber(account?.total_notional_position);
  const freeSpotUsdc = asNumber(account?.free_spot_usdc);
  const maintMargin = asNumber(account?.cross_maintenance_margin_used);

  // leverage: cross notional (solo posizioni cross) / portfolio value totale, come HL
  const leverage = accountValue != null && accountValue > 0
    ? (crossNtlPos ?? totalNtl) != null ? (crossNtlPos ?? totalNtl)! / accountValue : null
    : null;
  // unified ratio: maintenance margin / (cross account value + free spot USDC), come HL
  const uarDenominator = crossAccountValue != null
    ? crossAccountValue + (freeSpotUsdc ?? 0)
    : accountValue;
  const unifiedRatio = maintMargin != null && uarDenominator != null && uarDenominator > 0
    ? (maintMargin / uarDenominator) * 100
    : null;

  return (
    <div className="card" style={{ marginBottom: "var(--gap)" }}>
      <div className="card-head">
        <span className="title">Unified Account</span>
      </div>
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
        <Metric label="Portfolio Value" value={fmtUsd(account?.effective_account_value ?? account?.account_value)} />
        <Metric
          label="Unified Account Leverage"
          value={leverage != null ? `${leverage.toFixed(2)}x` : "-"}
          tone={leverage != null && leverage > 10 ? "short" : leverage != null && leverage > 5 ? "warn" : undefined}
        />
        <Metric
          label="Perps Maintenance Margin"
          value={fmtUsd(account?.cross_maintenance_margin_used)}
        />
        <Metric
          label="Unified Account Ratio"
          value={unifiedRatio != null ? fmtPct(unifiedRatio) : "-"}
          tone={
            (unifiedRatio ?? 0) > 80 ? "short"
            : (unifiedRatio ?? 0) > 50 ? "warn"
            : undefined
          }
        />
      </div>
    </div>
  );
}

function statusToDot(status: string): DotStatus {
  if (status === "ok" || status === "live") return "ok";
  if (status === "connecting" || status === "loading") return "info";
  if (status === "missing" || status === "closed" || status === "idle") return "muted";
  if (status === "error") return "danger";
  return "warn";
}

function KpiTile({
  label,
  value,
  hint,
  tone,
  progress,
  progressTone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "success" | "danger" | "warn" | "muted";
  progress?: number | null;
  progressTone?: "success" | "warn" | "danger" | "muted";
}) {
  return (
    <Card className="p-5">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{label}</div>
      <div
        className={cn(
          "mt-2 font-mono text-2xl font-semibold tabular-nums",
          tone === "success" && "text-success",
          tone === "danger" && "text-danger",
          tone === "warn" && "text-warn",
          (tone === "muted" || !tone) && "text-fg",
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-1 text-[11px] text-fg-3">{hint}</div>}
      {progress != null && (
        <Progress
          value={progress}
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

function StatusPill({ title, status, detail }: { title: string; status: string; detail: string }) {
  const dot = statusToDot(status);
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">{title}</span>
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-fg-2">
          <StatusDot status={dot} size={6} />
          {status}
        </span>
      </div>
      <div className="mt-2 truncate font-mono text-sm text-fg" title={detail}>
        {detail}
      </div>
    </Card>
  );
}

function SummaryWidget({ title, value, detail, tone }: { title: string; value: string; detail: string; tone?: string }) {
  const mappedTone =
    tone === "long" ? "success" : tone === "short" ? "danger" : tone === "warn" ? "warn" : undefined;
  return <KpiTile label={title} value={value} hint={detail} tone={mappedTone} />;
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
      <span className="truncate font-mono text-xs text-fg-2" title={value}>
        {value}
      </span>
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

function SortTh({ label, sortKey, current, dir, onSort }: {
  label: string;
  sortKey: PositionSortKey;
  current: PositionSortKey;
  dir: "asc" | "desc";
  onSort: (key: PositionSortKey) => void;
}) {
  const active = current === sortKey;
  return (
    <th
      style={{ cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" }}
      onClick={() => onSort(sortKey)}
    >
      {label}{" "}
      <span style={{ opacity: active ? 1 : 0.25, fontSize: 9 }}>
        {active ? (dir === "asc" ? "▲" : "▼") : "▲"}
      </span>
    </th>
  );
}

function PositionsTable({ rows, actionKey, selectedCoin, onSelectCoin, onClose }: {
  rows: AccountPosition[];
  actionKey: string | null;
  selectedCoin: string | null;
  onSelectCoin: (coin: string) => void;
  onClose: (coin: string) => void;
}) {
  const [sortKey, setSortKey] = useState<PositionSortKey>("pnl");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  function handleSort(key: PositionSortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const sorted = useMemo(() => {
    return [...rows].sort((a, b) => {
      const av = positionSortValue(a, sortKey);
      const bv = positionSortValue(b, sortKey);
      const cmp = typeof av === "string" ? av.localeCompare(bv as string) : (av as number) - (bv as number);
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [rows, sortKey, sortDir]);

  const thProps = { current: sortKey, dir: sortDir, onSort: handleSort };

  return (
    <div className="card">
      <div className="card-head">
        <span className="title">Posizioni perp</span>
        <span className="badge muted">{rows.length}</span>
      </div>
      <table className="tbl">
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
            <th />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => {
            const pnl = asNumber(row.unrealized_pnl);
            const coin = row.coin ?? "";
            const running = actionKey === `close-position-${coin}`;
            const isSelected = selectedCoin === coin;
            return (
              <tr
                key={`${row.coin}-${index}`}
                onClick={() => { if (coin) onSelectCoin(coin); }}
                style={{
                  cursor: coin ? "pointer" : undefined,
                  background: isSelected ? "var(--bg-2, rgba(255,255,255,0.05))" : undefined,
                  outline: isSelected ? "1px solid var(--border)" : undefined,
                }}
              >
                <td className="mono" style={{ fontWeight: isSelected ? 600 : undefined }}>{row.coin ?? "-"}</td>
                <td><SideBadge side={row.side} /></td>
                <td className="mono">{leverageLabel(row.leverage)}</td>
                <td className="mono">{fmtNum(row.size)}</td>
                <td className="mono">{fmt.price(row.entry_price)}</td>
                <td className="mono">{fmtUsd(row.position_value)}</td>
                <td className="mono" style={{ color: (pnl ?? 0) >= 0 ? "var(--long)" : "var(--short)" }}>{fmtUsd(row.unrealized_pnl)}</td>
                <td className="mono" style={{ color: (asNumber(row.return_on_equity) ?? 0) >= 0 ? "var(--long)" : "var(--short)" }}>
                  {fmtPct(asNumber(row.return_on_equity) != null ? (asNumber(row.return_on_equity)! * 100) : null)}
                </td>
                <td className="mono">{fmt.price(row.liquidation_price)}</td>
                <td style={{ textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                  <button
                    className="btn ghost"
                    disabled={!coin || running}
                    title="Chiudi posizione market"
                    onClick={() => onClose(coin)}
                    style={{ minHeight: 26, padding: "0 8px" }}
                  >
                    <Icon name="x" size={12} /> {running ? "..." : "Chiudi"}
                  </button>
                </td>
              </tr>
            );
          })}
          {rows.length === 0 && <tr><td colSpan={10} className="muted">Nessuna posizione perp aperta.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function FundingTable({ rows }: { rows: AccountFunding[] }) {
  return (
    <div className="card">
      <div className="card-head"><span className="title">Funding previsto</span><span className="badge muted">{rows.length}</span></div>
      <table className="tbl">
        <thead><tr><th>Coin</th><th>HL rate</th><th>Next</th></tr></thead>
        <tbody>
          {rows.slice(0, 12).map((row) => (
            <tr key={row.coin}>
              <td className="mono">{row.coin}</td>
              <td className="mono">{fmtFunding(row.hyperliquid?.funding_rate ?? null)}</td>
              <td className="mono">{fmtTime(row.hyperliquid?.next_funding_time)}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={3} className="muted">Funding non disponibile.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function SpotBalances({ rows }: { rows: AccountSpotBalance[] }) {
  return (
    <div className="card">
      <div className="card-head"><span className="title">Spot balances</span><span className="badge muted">{rows.length}</span></div>
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

const ORDERS_PAGE_SIZE = 12;

function OpenOrders({
  rows,
  positions,
  filterCoin,
  onClearFilter,
  actionKey,
  onCancel,
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
    [rows, filterCoin]
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
              <th>P&L previsto</th>
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

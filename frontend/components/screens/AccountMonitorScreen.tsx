"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Icon, fmt } from "@/components/atoms";
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

function statusClass(status: string) {
  if (status === "ok" || status === "live") return "long";
  if (status === "connecting") return "info";
  if (status === "idle" || status === "closed") return "muted";
  return "short";
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
  const marginRatio = useMemo(() => {
    const marginUsed = asNumber(snapshot?.account.total_margin_used);
    const accountValue = asNumber(snapshot?.account.account_value);
    if (marginUsed == null || accountValue == null || accountValue <= 0) return tradingSummary.marginRatio;
    return (marginUsed / accountValue) * 100;
  }, [snapshot, tradingSummary.marginRatio]);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Account Monitor</h1>
          <div className="page-sub">
            {health.network} · {snapshot?.account.address ?? "account non configurato"} · {snapshot?.checked_at ? fmt.relTime(snapshot.checked_at) : "mai aggiornato"}
          </div>
        </div>
        <div className="page-actions">
          <button className="btn ghost" onClick={refresh} disabled={loading} title="Aggiorna">
            <Icon name="refresh" size={12} /> {loading ? "Sync..." : "Refresh"}
          </button>
        </div>
      </div>

      {error && <div className="card" style={{ color: "var(--short)", padding: 14, marginBottom: "var(--gap)" }}>{error}</div>}
      {action.error && <div className="card" style={{ color: "var(--short)", padding: 14, marginBottom: "var(--gap)" }}>{action.error}</div>}
      {action.message && <div className="card" style={{ color: "var(--long)", padding: 14, marginBottom: "var(--gap)" }}>{action.message}</div>}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <StatusCard title="API" status={snapshot?.api.status ?? "loading"} detail={`${snapshot?.api.latency_ms ?? "-"} ms`} />
        <StatusCard title="WebSocket" status={ws.status} detail={ws.lastMessageAt ? fmt.relTime(ws.lastMessageAt) : `${ws.messages} msg`} />
        <StatusCard title="Account" status={snapshot?.account.configured ? (snapshot.account.error ? "error" : "ok") : "missing"} detail={snapshot?.account.address ?? "no address"} />
        <StatusCard title="Funding" status={snapshot?.funding.status ?? "loading"} detail={`${snapshot?.funding.predicted.length ?? 0} mercati`} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1.1fr 0.9fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Saldo account</span>
            <span className={"badge " + statusClass(snapshot?.status ?? "loading")}>{snapshot?.status ?? "loading"}</span>
          </div>
          <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
            <Metric label="Equity" value={fmtUsd(snapshot?.account.account_value)} />
            <Metric label="Withdrawable" value={fmtUsd(snapshot?.account.withdrawable)} />
            <Metric label="Margin used" value={fmtUsd(snapshot?.account.total_margin_used)} />
            <Metric label="Notional" value={fmtUsd(snapshot?.account.total_notional_position)} />
            <Metric label="Unrealized PnL" value={fmtUsd(totalUnrealized)} tone={(totalUnrealized ?? 0) >= 0 ? "long" : "short"} />
            <Metric label="Open orders" value={String(snapshot?.account.open_orders ?? "-")} />
          </div>
          {(snapshot?.api.error || snapshot?.account.error) && (
            <div style={{ borderTop: "1px solid var(--border)", padding: 14, color: "var(--short)", fontSize: "var(--fz-sm)" }}>
              {snapshot.api.error || snapshot.account.error}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="title">Connessioni</span>
          </div>
          <div className="card-body" style={{ display: "grid", gap: 12 }}>
            <ConnectionRow label="Info API" value={snapshot?.api.info_url ?? "-"} status={snapshot?.api.status ?? "loading"} />
            <ConnectionRow label="WS API" value={snapshot?.api.ws_url ?? wsUrl(health.network)} status={ws.status} />
            <ConnectionRow label="Markets" value={`${snapshot?.api.universe_count ?? "-"} universe · ${snapshot?.api.mids_count ?? "-"} mids`} status={snapshot?.api.status ?? "loading"} />
            {ws.error && <div style={{ color: "var(--short)", fontSize: "var(--fz-sm)" }}>{ws.error}</div>}
          </div>
        </div>
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
            const ntl = asNumber(snapshot?.account.total_notional_position);
            const val = asNumber(snapshot?.account.account_value);
            if (ntl == null || val == null || val <= 0) return "-";
            return `${(ntl / val).toFixed(2)}x`;
          })()}
          detail={`${fmtUsd(snapshot?.account.total_notional_position)} notional`}
          tone={(() => {
            const ntl = asNumber(snapshot?.account.total_notional_position);
            const val = asNumber(snapshot?.account.account_value);
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
  const accountValue = asNumber(account?.account_value);
  const crossAccountValue = asNumber(account?.cross_account_value);
  const totalNtl = asNumber(account?.total_notional_position);
  const maintMargin = asNumber(account?.cross_maintenance_margin_used);

  // leverage: total notional / portfolio value (include spot + perps)
  const leverage = accountValue != null && accountValue > 0 && totalNtl != null
    ? totalNtl / accountValue
    : null;
  // unified ratio: maintenance margin / cross account value (solo cross, come fa HL)
  const denominator = crossAccountValue ?? accountValue;
  const unifiedRatio = maintMargin != null && denominator != null && denominator > 0
    ? (maintMargin / denominator) * 100
    : null;

  return (
    <div className="card" style={{ marginBottom: "var(--gap)" }}>
      <div className="card-head">
        <span className="title">Unified Account</span>
      </div>
      <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
        <Metric label="Portfolio Value" value={fmtUsd(account?.account_value)} />
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

function StatusCard({ title, status, detail }: { title: string; status: string; detail: string }) {
  return (
    <div className="card">
      <div className="card-body" style={{ display: "grid", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <span className="muted" style={{ fontSize: "var(--fz-xs)", textTransform: "uppercase" }}>{title}</span>
          <span className={"badge " + statusClass(status)}>{status}</span>
        </div>
        <div className="mono" style={{ fontSize: "var(--fz-lg)", overflow: "hidden", textOverflow: "ellipsis" }}>{detail}</div>
      </div>
    </div>
  );
}

function SummaryWidget({ title, value, detail, tone }: { title: string; value: string; detail: string; tone?: string }) {
  return (
    <div className="card">
      <div className="card-body" style={{ display: "grid", gap: 8 }}>
        <span className="muted" style={{ fontSize: "var(--fz-xs)", textTransform: "uppercase" }}>{title}</span>
        <span className="mono" style={{ color: tone ? `var(--${tone})` : "var(--fg)", fontSize: "var(--fz-xl)", overflow: "hidden", textOverflow: "ellipsis" }}>
          {value}
        </span>
        <span className="muted" style={{ fontSize: "var(--fz-xs)" }}>{detail}</span>
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div style={{ display: "grid", gap: 4, minWidth: 0 }}>
      <span className="muted" style={{ fontSize: 10, textTransform: "uppercase" }}>{label}</span>
      <span className="mono" style={{ color: tone ? `var(--${tone})` : "var(--fg)", fontSize: "var(--fz-md)", overflow: "hidden", textOverflow: "ellipsis" }}>{value}</span>
    </div>
  );
}

function ConnectionRow({ label, value, status }: { label: string; value: string; status: string }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "100px 1fr auto", gap: 10, alignItems: "center" }}>
      <span className="muted" style={{ fontSize: "var(--fz-xs)" }}>{label}</span>
      <span className="mono" style={{ fontSize: "var(--fz-xs)", overflow: "hidden", textOverflow: "ellipsis" }}>{value}</span>
      <span className={"badge " + statusClass(status)}>{status}</span>
    </div>
  );
}

function SideBadge({ side }: { side: string | null | undefined }) {
  const normalized = side?.toLowerCase();
  if (normalized === "long") return <span className="dir mono long">↑ LONG</span>;
  if (normalized === "short") return <span className="dir mono short">↓ SHORT</span>;
  return <span className="badge muted">-</span>;
}

function PositionsTable({ rows, actionKey, onClose }: { rows: AccountPosition[]; actionKey: string | null; onClose: (coin: string) => void }) {
  return (
    <div className="card">
      <div className="card-head"><span className="title">Posizioni perp</span><span className="badge muted">{rows.length}</span></div>
      <table className="tbl">
        <thead><tr><th>Coin</th><th>Direzione</th><th>Leva</th><th>Size</th><th>Entry</th><th>Value</th><th>PnL</th><th>ROE</th><th>Liq</th><th /></tr></thead>
        <tbody>
          {rows.map((row, index) => {
            const pnl = asNumber(row.unrealized_pnl);
            const coin = row.coin ?? "";
            const running = actionKey === `close-position-${coin}`;
            return (
              <tr key={`${row.coin}-${index}`}>
                <td className="mono">{row.coin ?? "-"}</td>
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
                <td style={{ textAlign: "right" }}>
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
  actionKey,
  onCancel,
}: {
  rows: AccountOpenOrder[];
  positions: AccountPosition[];
  actionKey: string | null;
  onCancel: (order: AccountOpenOrder) => void;
}) {
  const [page, setPage] = useState(0);
  const positionsMap = useMemo(() => positionByCoin(positions), [positions]);
  const totalPages = Math.max(1, Math.ceil(rows.length / ORDERS_PAGE_SIZE));
  const pageRows = rows.slice(page * ORDERS_PAGE_SIZE, (page + 1) * ORDERS_PAGE_SIZE);

  // Reset to first page when rows change (e.g. after refresh)
  useEffect(() => { setPage(0); }, [rows.length]);

  return (
    <div className="card">
      <div className="card-head"><span className="title">Open orders</span><span className="badge muted">{rows.length}</span></div>
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
            {rows.length === 0 && <tr><td colSpan={9} className="muted">Nessun ordine aperto.</td></tr>}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8, padding: "8px 14px", borderTop: "1px solid var(--border)" }}>
          <span className="muted" style={{ fontSize: "var(--fz-xs)" }}>
            {page * ORDERS_PAGE_SIZE + 1}–{Math.min((page + 1) * ORDERS_PAGE_SIZE, rows.length)} di {rows.length}
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

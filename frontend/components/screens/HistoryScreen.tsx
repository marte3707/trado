"use client";

import React, { useMemo, useState } from "react";
import { Icon, Seg, fmt } from "@/components/atoms";
import { Position, Signal, Order } from "@/lib/api";

interface Props {
  positions: Position[];
  signals: Signal[];
  orders: Order[];
}

type ModeFilter = "all" | "live" | "paper";
type ModeStats = {
  count: number;
  wins: number;
  losses: number;
  wr: number;
  total: number;
  totalPct: number | null;
  avgR: number | null;
  rCount: number;
  maxDrawdown: number;
  maxDrawdownPct: number | null;
};
type TpPlanItem = {
  level: string;
  price: string;
  allocation: number | null;
  filled: boolean;
};
type EquityPoint = {
  date: Date;
  time: number;
  value: number;
  pct: number | null;
  trades: number;
  label: string;
};
type EquityHover = {
  point: EquityPoint;
  series: string;
  color: string;
  x: number;
  y: number;
};

const EXIT_ORDER_TYPES = new Set(["tp1", "tp2", "tp3", "take_profit", "stop_loss", "manual"]);
const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];

function asNumber(v: unknown): number | null {
  if (v == null) return null;
  const n = typeof v === "number" ? v : Number(v);
  return isFinite(n) ? n : null;
}

function sideMultiplier(side: string): 1 | -1 {
  return side.toLowerCase() === "short" ? -1 : 1;
}

function initialRisk(position: Position): number | null {
  const stored = asNumber(position.initial_risk);
  if (stored !== null) return stored;
  const entry = asNumber(position.entry_price);
  const stop = asNumber(position.stop_loss);
  const size = asNumber(displaySize(position));
  if (entry == null || stop == null || size == null) return null;
  const risk = Math.abs(entry - stop) * size;
  return risk > 0 ? risk : null;
}

function fmtSize(value: string | null | undefined): string {
  const n = asNumber(value);
  if (n == null) return "—";
  const abs = Math.abs(n);
  const maxDigits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return n.toLocaleString("en-US", { maximumFractionDigits: maxDigits });
}

function displaySize(position: Position): string {
  if (position.initial_size != null) return position.initial_size;
  const initialSize = position.metadata_json?.initial_size;
  return typeof initialSize === "string" || typeof initialSize === "number"
    ? String(initialSize)
    : position.size;
}

function takeProfitPlan(position: Position): TpPlanItem[] {
  const raw = position.metadata_json?.take_profit_plan;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item): item is Record<string, unknown> => item != null && typeof item === "object")
    .map((item, index) => ({
      level: String(item.level ?? `tp${index + 1}`),
      price: String(item.price ?? ""),
      allocation: asNumber(item.allocation),
      filled: Boolean(item.filled),
    }));
}

function exitOrdersFor(position: Position, orders: Order[]): Order[] {
  return orders
    .filter(order => order.position_id === position.id && EXIT_ORDER_TYPES.has(order.order_type) && order.status === "filled")
    .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());
}

function hasExitActivity(position: Position, orders: Order[]): boolean {
  return position.status !== "open" || exitOrdersFor(position, orders).length > 0 || asNumber(position.realized_pnl) !== null;
}

function latestExitPrice(position: Position, exitOrders: Order[]): string | null {
  const metadataPrice = position.metadata_json?.exit_price ?? position.metadata_json?.last_exit_price;
  if (typeof metadataPrice === "string" || typeof metadataPrice === "number") return String(metadataPrice);
  return exitOrders.at(-1)?.price ?? null;
}

function exitPnl(position: Position, order: Order): number | null {
  const entry = asNumber(position.entry_price);
  const price = asNumber(order.price);
  const size = asNumber(order.size);
  if (entry == null || price == null || size == null) return null;
  return (price - entry) * size * sideMultiplier(position.side);
}

function exitRContribution(position: Position, order: Order): number | null {
  const pnl = exitPnl(position, order);
  const risk = initialRisk(position);
  if (pnl === null || risk === null) return null;
  return pnl / risk;
}

function realizedPnl(position: Position, exitOrders: Order[] = []): number | null {
  const stored = asNumber(position.realized_pnl);
  if (stored !== null) return stored;
  const exitPnls = exitOrders.map((order) => exitPnl(position, order)).filter((value): value is number => value !== null);
  if (exitPnls.length === 0) return null;
  return exitPnls.reduce((sum, value) => sum + value, 0);
}

function positionReturnPct(position: Position, exitOrders: Order[] = []): number | null {
  const stored = asNumber(position.realized_return_pct);
  if (stored !== null) return stored;
  const pnl = realizedPnl(position, exitOrders);
  const notional = positionNotional(position);
  if (pnl === null || notional === null || notional <= 0) return null;
  return (pnl / notional) * 100;
}

function positionRMultiple(position: Position, exitOrders: Order[] = []): number | null {
  const stored = asNumber(position.realized_r_multiple);
  if (stored !== null) return stored;
  const pnl = realizedPnl(position, exitOrders);
  const risk = initialRisk(position);
  if (pnl === null || risk === null) return null;
  return pnl / risk;
}

function exitLabel(orderType: string): string {
  if (orderType === "tp1" || orderType === "tp2" || orderType === "tp3") return orderType.toUpperCase();
  if (orderType === "take_profit") return "TP";
  if (orderType === "stop_loss") return "SL";
  return orderType.toUpperCase();
}

function positionStatusLabel(position: Position, exitOrders: Order[]): string {
  if (position.status !== "open") return position.status;
  return exitOrders.length > 0 || asNumber(position.realized_pnl) !== null ? "partial" : "open";
}

function positionNotional(position: Position): number | null {
  const stored = asNumber(position.notional);
  if (stored !== null) return stored;
  const size = asNumber(displaySize(position));
  const entry = asNumber(position.entry_price);
  if (size == null || entry == null) return null;
  return size * entry;
}

function pnlTone(value: number | null | undefined): "" | "up" | "down" {
  if (value == null || value === 0) return "";
  return value > 0 ? "up" : "down";
}

function fmtR(value: number | null | undefined, digits = 2): string {
  if (value == null || !isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}R`;
}

function positionSourceSignalId(position: Position) {
  const parsed = asNumber(position.metadata_json?.source_signal_id);
  return parsed === null ? null : Math.trunc(parsed);
}

function positionEventTime(position: Position): number {
  const parsed = new Date(position.closed_at ?? position.opened_at).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function positionSearchText(position: Position, exitOrders: Order[]): string {
  const pnl = realizedPnl(position, exitOrders);
  const pct = positionReturnPct(position, exitOrders);
  const r = positionRMultiple(position, exitOrders);
  const notional = positionNotional(position);
  const sourceSignalId = position.metadata_json?.source_signal_id;
  const sourceTimeframe = position.metadata_json?.source_timeframe;
  const status = positionStatusLabel(position, exitOrders);
  const exitPrice = latestExitPrice(position, exitOrders);
  const tpText = takeProfitPlan(position)
    .flatMap((item) => [item.level, item.price, item.filled ? "filled" : "pending"])
    .join(" ");
  const exitText = exitOrders
    .flatMap((order) => [order.order_type, exitLabel(order.order_type), order.price, order.size, order.status])
    .filter((value): value is string => typeof value === "string")
    .join(" ");

  return [
    position.id,
    position.market?.symbol,
    position.market?.base,
    position.market?.quote,
    position.market?.market_type,
    position.mode,
    position.side,
    status,
    position.entry_price,
    exitPrice,
    displaySize(position),
    position.size,
    position.stop_loss,
    position.take_profit,
    notional,
    pnl,
    pct,
    r,
    position.realized_pnl,
    position.realized_return_pct,
    position.realized_r_multiple,
    sourceSignalId,
    sourceTimeframe,
    position.opened_at,
    position.closed_at,
    tpText,
    exitText,
  ]
    .filter((value): value is string | number => typeof value === "string" || typeof value === "number")
    .join(" ")
    .toLowerCase();
}

/* ─────────────────────────────────────────────────────────────────────────── */
export function HistoryScreen({ positions, signals, orders }: Props) {
  const [modeFilter, setModeFilter] = useState<ModeFilter>("all");
  const [marketTypeFilter, setMarketTypeFilter] = useState<"all" | "perp" | "spot">("all");
  const [periodFilter, setPeriodFilter] = useState("7d");
  const [historyQuery, setHistoryQuery] = useState("");
  const [expandedPositionId, setExpandedPositionId] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | "closed" | "partial">("all");
  const [historyPage, setHistoryPage] = useState(1);
  const [historyPageSize, setHistoryPageSize] = useState(10);
  const [perfExpanded, setPerfExpanded] = useState(false);
  const [stratExpanded, setStratExpanded] = useState(false);
  const [timeframeExpanded, setTimeframeExpanded] = useState(false);

  const trackedPositions = useMemo(() => positions.filter(p => hasExitActivity(p, orders)), [positions, orders]);

  const cutoff = useMemo(() => {
    const now = Date.now();
    if (periodFilter === "1d")  return now - 86400000;
    if (periodFilter === "7d")  return now - 7 * 86400000;
    if (periodFilter === "30d") return now - 30 * 86400000;
    return 0;
  }, [periodFilter]);

  const periodFiltered = useMemo(
    () => trackedPositions.filter(p => new Date(p.closed_at ?? p.opened_at).getTime() >= cutoff),
    [trackedPositions, cutoff]
  );

  const perpCount = useMemo(() => periodFiltered.filter(p => (p.market?.market_type ?? "perp") === "perp").length, [periodFiltered]);
  const spotCount = useMemo(() => periodFiltered.filter(p => p.market?.market_type === "spot").length, [periodFiltered]);

  const marketTypeFiltered = useMemo(
    () => marketTypeFilter === "all"
      ? periodFiltered
      : periodFiltered.filter(p => (p.market?.market_type ?? "perp") === marketTypeFilter),
    [periodFiltered, marketTypeFilter]
  );

  const modeRows = useMemo(
    () => marketTypeFiltered.filter(p => modeFilter === "all" || p.mode === modeFilter),
    [marketTypeFiltered, modeFilter]
  );

  const closedCount = useMemo(() => modeRows.filter(p => positionStatusLabel(p, exitOrdersFor(p, orders)) !== "partial").length, [modeRows, orders]);
  const partialCount = useMemo(() => modeRows.filter(p => positionStatusLabel(p, exitOrdersFor(p, orders)) === "partial").length, [modeRows, orders]);

  const statusRows = useMemo(() => {
    if (statusFilter === "all") return modeRows;
    return modeRows.filter(p => {
      const label = positionStatusLabel(p, exitOrdersFor(p, orders));
      return statusFilter === "partial" ? label === "partial" : label !== "partial";
    });
  }, [modeRows, statusFilter, orders]);

  const historySearchTokens = historyQuery.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rows = useMemo(
    () => historySearchTokens.length === 0
      ? statusRows
      : statusRows.filter((position) => {
        const text = positionSearchText(position, exitOrdersFor(position, orders));
        return historySearchTokens.every((token) => text.includes(token));
      }),
    [historySearchTokens, statusRows, orders]
  );
  const historyTotalPages = Math.max(1, Math.ceil(rows.length / historyPageSize));
  const historySafePage = Math.min(historyPage, historyTotalPages);
  const paginatedRows = useMemo(
    () => rows.slice((historySafePage - 1) * historyPageSize, historySafePage * historyPageSize),
    [rows, historySafePage, historyPageSize]
  );

  const live   = useMemo(() => marketTypeFiltered.filter(p => p.mode === "live"),  [marketTypeFiltered]);
  const paper  = useMemo(() => marketTypeFiltered.filter(p => p.mode === "paper"), [marketTypeFiltered]);
  const visibleLive = useMemo(() => rows.filter(p => p.mode === "live"), [rows]);
  const visiblePaper = useMemo(() => rows.filter(p => p.mode === "paper"), [rows]);

  const statsFor = (items: Position[]) => {
    const pnls = items.map(p => realizedPnl(p, exitOrdersFor(p, orders))).filter((v): v is number => v !== null);
    const wins = pnls.filter(v => v > 0).length;
    const total = pnls.reduce((a, b) => a + b, 0);
    const rMultiples = items
      .map((position) => positionRMultiple(position, exitOrdersFor(position, orders)))
      .filter((value): value is number => value !== null);
    const totalNotional = items.reduce((sum, position) => {
      if (realizedPnl(position, exitOrdersFor(position, orders)) === null) return sum;
      const notional = positionNotional(position);
      return notional !== null && notional > 0 ? sum + notional : sum;
    }, 0);
    let cumulativePnl = 0;
    let cumulativeNotional = 0;
    let peakPnl = 0;
    let peakPct = 0;
    let maxDrawdown = 0;
    let maxDrawdownPct: number | null = null;
    const sorted = [...items]
      .filter(p => realizedPnl(p, exitOrdersFor(p, orders)) !== null)
      .sort((a, b) => positionEventTime(a) - positionEventTime(b));

    for (const position of sorted) {
      cumulativePnl += realizedPnl(position, exitOrdersFor(position, orders)) ?? 0;
      const notional = positionNotional(position);
      if (notional !== null && notional > 0) cumulativeNotional += notional;

      peakPnl = Math.max(peakPnl, cumulativePnl);
      maxDrawdown = Math.max(maxDrawdown, peakPnl - cumulativePnl);

      if (cumulativeNotional > 0) {
        const cumulativePct = (cumulativePnl / cumulativeNotional) * 100;
        peakPct = Math.max(peakPct, cumulativePct);
        const drawdownPct = peakPct - cumulativePct;
        maxDrawdownPct = Math.max(maxDrawdownPct ?? 0, drawdownPct);
      }
    }

    return {
      count: items.length,
      wins,
      losses: pnls.length - wins,
      wr: pnls.length > 0 ? (wins / pnls.length) * 100 : 0,
      total,
      totalPct: totalNotional > 0 ? (total / totalNotional) * 100 : null,
      avgR: rMultiples.length > 0 ? rMultiples.reduce((a, b) => a + b, 0) / rMultiples.length : null,
      rCount: rMultiples.length,
      maxDrawdown,
      maxDrawdownPct,
    };
  };

  const all = statsFor(rows);
  const selectedLiveSt = statsFor(visibleLive);
  const modeLiveSt = statsFor(live);
  const modePaperSt = statsFor(paper);

  const strategyStats = useMemo(() => {
    const signalMap = new Map<number, Signal>();
    for (const s of signals) signalMap.set(s.id, s);

    type Acc = {
      count: number;
      wins: number;
      losses: number;
      totalPnl: number;
      totalR: number;
      rCount: number;
      totalScore: number;
      scoreCount: number;
      totalContribution: number;
      contributionCount: number;
    };
    const map = new Map<string, Acc>();

    for (const p of marketTypeFiltered) {
      const sigId = positionSourceSignalId(p);
      if (sigId === null) continue;
      const sig = signalMap.get(sigId);
      if (!sig) continue;

      const breakdown = sig.strategy_breakdown as Record<string, Record<string, unknown>>;
      const exitOrds = exitOrdersFor(p, orders);
      const pnl = realizedPnl(p, exitOrds);
      const r = positionRMultiple(p, exitOrds);

      for (const [name, info] of Object.entries(breakdown)) {
        if (name.startsWith("_") || typeof info !== "object" || info === null) continue;
        const dir = info.direction as string;
        if (!dir || dir === "NEUTRAL") continue;
        const contribution = asNumber(info.contribution as unknown) ?? 0;

        const acc = map.get(name) ?? {
          count: 0,
          wins: 0,
          losses: 0,
          totalPnl: 0,
          totalR: 0,
          rCount: 0,
          totalScore: 0,
          scoreCount: 0,
          totalContribution: 0,
          contributionCount: 0,
        };
        acc.count++;
        if (pnl !== null) {
          if (pnl > 0) acc.wins++;
          else acc.losses++;
          acc.totalPnl += pnl;
        }
        if (r !== null) { acc.totalR += r; acc.rCount++; }
        acc.totalScore += sig.score;
        acc.scoreCount++;
        acc.totalContribution += contribution;
        acc.contributionCount++;
        map.set(name, acc);
      }
    }

    return Array.from(map.entries())
      .map(([name, st]) => ({
        name,
        trades: st.count,
        wins: st.wins,
        losses: st.losses,
        wr: st.wins + st.losses > 0 ? (st.wins / (st.wins + st.losses)) * 100 : 0,
        avgR: st.rCount > 0 ? st.totalR / st.rCount : null,
        avgScore: st.scoreCount > 0 ? st.totalScore / st.scoreCount : null,
        avgContribution: st.contributionCount > 0 ? st.totalContribution / st.contributionCount : null,
        totalPnl: st.totalPnl,
      }))
      .sort((a, b) => b.trades - a.trades);
  }, [marketTypeFiltered, signals, orders]);

  const timeframeStats = useMemo(() => {
    type Acc = { count: number; wins: number; losses: number; totalPnl: number; totalR: number; rCount: number };
    const map = new Map<string, Acc>();
    for (const p of marketTypeFiltered) {
      const tf = p.metadata_json?.source_timeframe;
      if (!tf || typeof tf !== "string") continue;
      const exitOrds = exitOrdersFor(p, orders);
      const pnl = realizedPnl(p, exitOrds);
      const r = positionRMultiple(p, exitOrds);
      const acc = map.get(tf) ?? { count: 0, wins: 0, losses: 0, totalPnl: 0, totalR: 0, rCount: 0 };
      acc.count++;
      if (pnl !== null) { if (pnl > 0) acc.wins++; else acc.losses++; acc.totalPnl += pnl; }
      if (r !== null) { acc.totalR += r; acc.rCount++; }
      map.set(tf, acc);
    }
    return Array.from(map.entries())
      .map(([timeframe, st]) => ({
        timeframe,
        trades: st.count,
        wins: st.wins,
        losses: st.losses,
        wr: st.wins + st.losses > 0 ? (st.wins / (st.wins + st.losses)) * 100 : 0,
        avgR: st.rCount > 0 ? st.totalR / st.rCount : null,
        totalPnl: st.totalPnl,
      }))
      .sort((a, b) => b.trades - a.trades);
  }, [marketTypeFiltered, orders]);

  function changeModeFilter(next: ModeFilter) {
    setModeFilter(next);
    setHistoryPage(1);
    setExpandedPositionId(null);
  }

  function changeMarketTypeFilter(next: "all" | "perp" | "spot") {
    setMarketTypeFilter(next);
    setHistoryPage(1);
    setExpandedPositionId(null);
  }

  function changePeriodFilter(next: string) {
    setPeriodFilter(next);
    setHistoryPage(1);
    setExpandedPositionId(null);
  }

  function changeStatusFilter(next: "all" | "closed" | "partial") {
    setStatusFilter(next);
    setHistoryPage(1);
    setExpandedPositionId(null);
  }

  function changeHistoryQuery(next: string) {
    setHistoryQuery(next);
    setHistoryPage(1);
    setExpandedPositionId(null);
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Storico segnali</h1>
          <div className="page-sub">
            {historySearchTokens.length > 0 ? `${rows.length}/${modeRows.length}` : rows.length} posizioni con exit · {visibleLive.length} live · {visiblePaper.length} paper
          </div>
        </div>
        <div className="page-actions">
          <Seg value={periodFilter} onChange={changePeriodFilter} options={[["1d","1d"],["7d","7d"],["30d","30d"],["all","All"]]} />
          <button className="btn ghost"><Icon name="dl" size={12} /> Export</button>
        </div>
      </div>

      {/* Mode + market type filter */}
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: "var(--gap)", padding: "10px 14px", background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Mode</span>
        <ModeSeg
          value={modeFilter}
          onChange={changeModeFilter}
          counts={{ all: marketTypeFiltered.length, live: live.length, paper: paper.length }}
        />
        <span style={{ width: 1, height: 20, background: "var(--border)", display: "inline-block" }} />
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Mercato</span>
        <MarketTypeSeg
          value={marketTypeFilter}
          onChange={changeMarketTypeFilter}
          counts={{ all: periodFiltered.length, perp: perpCount, spot: spotCount }}
        />
        <span style={{ width: 1, height: 20, background: "var(--border)", display: "inline-block" }} />
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Status</span>
        <StatusSeg
          value={statusFilter}
          onChange={changeStatusFilter}
          counts={{ all: modeRows.length, closed: closedCount, partial: partialCount }}
        />
        <div style={{ flex: 1 }} />
        <LiveEdgePill liveSt={modeLiveSt} papSt={modePaperSt} />
      </div>

      {/* Performance — collapsible */}
      <div style={{ marginBottom: "var(--gap)" }}>
        <button
          onClick={() => setPerfExpanded(v => !v)}
          style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", background: "none", border: "none", cursor: "pointer", padding: "4px 0 10px", color: "var(--fg-2)" }}
        >
          <span style={{ display: "inline-flex", transition: "transform 0.15s", transform: perfExpanded ? "rotate(90deg)" : "none" }}>
            <Icon name="chevron" size={12} />
          </span>
          <span style={{ fontSize: "var(--fz-sm)", fontWeight: 500 }}>Performance</span>
          {!perfExpanded && (
            <span className="muted mono" style={{ fontSize: "var(--fz-xs)", marginLeft: 4 }}>
              WR {all.wr.toFixed(0)}% · {fmtR(all.avgR)} · {fmt.pnl(all.total)} · {all.count} trade
            </span>
          )}
        </button>

        {perfExpanded && (
          <>
            {/* Stats */}
            <div className="stat-grid" style={{ marginBottom: "var(--gap)" }}>
              <div className="stat">
                <div className="stat-label">Win rate</div>
                <div className="stat-value">{all.wr.toFixed(0)}<span style={{ color: "var(--fg-3)" }}>%</span></div>
                <div className="stat-delta"><span className="mono">{all.wins}W</span> · <span className="mono">{all.losses}L</span></div>
              </div>
              <div className="stat">
                <div className="stat-label">P&amp;L totale</div>
                <div className={"stat-value " + pnlTone(all.total)}>{fmt.pnl(all.total)}</div>
                <div className={"stat-delta " + pnlTone(all.totalPct)}>
                  {fmt.pct(all.totalPct)} · {all.count} posizioni filtrate
                </div>
              </div>
              <div className="stat">
                <div className="stat-label">Drawdown max</div>
                <div className={"stat-value " + (all.maxDrawdown > 0 ? "down" : "")}>
                  {all.maxDrawdown > 0 ? fmt.pnl(-all.maxDrawdown) : fmt.money(0)}
                </div>
                <div className={"stat-delta " + ((all.maxDrawdownPct ?? 0) > 0 ? "down" : "")}>
                  {all.maxDrawdownPct !== null ? fmt.pct(-all.maxDrawdownPct, false) : "—"} · picco-valle
                </div>
              </div>
              <div className="stat">
                <div className="stat-label">R-multiple medio</div>
                <div className={"stat-value " + pnlTone(all.avgR)}>{fmtR(all.avgR)}</div>
                <div className="stat-delta">{all.rCount} trade con SL/exit</div>
              </div>
              <div className="stat">
                <div className="stat-label">{modeFilter === "live" ? "Net P&L live €" : "Trade totali"}</div>
                {modeFilter === "live" ? (
                  <>
                    <div className={"stat-value " + pnlTone(selectedLiveSt.total)}>{fmt.pnl(selectedLiveSt.total)}</div>
                    <div className="stat-delta">su {selectedLiveSt.count} posizioni live</div>
                  </>
                ) : (
                  <>
                    <div className="stat-value">{all.count}</div>
                    <div className="stat-delta">{visibleLive.length} live · {visiblePaper.length} paper</div>
                  </>
                )}
              </div>
              <div className="stat">
                <div className="stat-label">Distribuzione</div>
                <ModeBars live={visibleLive.length} paper={visiblePaper.length} />
              </div>
            </div>

            {/* Per-mode + equity curve */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
              <div className="card">
                <div className="card-head">
                  <span className="title">Performance per modalità</span>
                </div>
                <div style={{ padding: "4px 18px 14px" }}>
                  {[
                    { label: "Live",  stats: modeLiveSt,  color: "var(--short)" },
                    { label: "Paper", stats: modePaperSt, color: "var(--warn)" },
                  ].map((row, i) => (
                    <div key={i} style={{ display: "grid", gridTemplateColumns: "80px 60px 1fr 80px", alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
                      <span style={{ fontSize: "var(--fz-sm)", display: "flex", alignItems: "center", gap: 6 }}>
                        <span style={{ width: 5, height: 5, borderRadius: "50%", background: row.color, display: "inline-block" }} />
                        {row.label}
                      </span>
                      <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{row.stats.count}t</span>
                      <WrBar wr={row.stats.wr} />
                      <span className={"mono " + pnlTone(row.stats.total)} style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
                        {fmt.pnl(row.stats.total)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="card">
                <div className="card-head">
                  <span className="title">Equity curve</span>
                  <div className="right" style={{ display: "flex", gap: 10, fontSize: "var(--fz-xs)" }}>
                    <span className="mono"><span className="dot red" /> Live</span>
                    <span className="mono"><span className="dot amber" /> Paper</span>
                  </div>
                </div>
                <div className="card-body"><DualEquityCurve live={visibleLive} paper={visiblePaper} /></div>
              </div>
            </div>

            {/* Performance per strategia */}
            <div className="card" style={{ marginBottom: "var(--gap)" }}>
              <div className="card-head" style={{ cursor: "pointer" }} onClick={() => setStratExpanded(v => !v)}>
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ display: "inline-flex", transition: "transform 0.15s", transform: stratExpanded ? "rotate(90deg)" : "none", color: "var(--fg-2)" }}>
                    <Icon name="chevron" size={12} />
                  </span>
                  <span className="title">Performance per strategia</span>
                  {!stratExpanded && strategyStats.length > 0 && (
                    <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{strategyStats.length} indicatori</span>
                  )}
                </span>
                <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>tutti i mode · periodo selezionato</div>
              </div>
              {stratExpanded && <StrategyPerformanceRows rows={strategyStats} />}
            </div>

            {/* Performance per timeframe */}
            <div className="card" style={{ marginBottom: "var(--gap)" }}>
              <div className="card-head" style={{ cursor: "pointer" }} onClick={() => setTimeframeExpanded(v => !v)}>
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ display: "inline-flex", transition: "transform 0.15s", transform: timeframeExpanded ? "rotate(90deg)" : "none", color: "var(--fg-2)" }}>
                    <Icon name="chevron" size={12} />
                  </span>
                  <span className="title">Performance per timeframe</span>
                  {!timeframeExpanded && timeframeStats.length > 0 && (
                    <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{timeframeStats.length} timeframe</span>
                  )}
                </span>
                <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>tutti i mode · periodo selezionato</div>
              </div>
              {timeframeExpanded && <TimeframePerformanceRows rows={timeframeStats} />}
            </div>
          </>
        )}
      </div>

      {/* History table */}
      <div className="card">
        <div className="card-head">
          <span className="title">
            {modeFilter === "all" ? "Trade chiusi e parziali" :
             modeFilter === "live" ? "Trade live" :
             "Trade paper"}
          </span>
          <div className="right">
            <div style={{ position: "relative", display: "inline-flex", alignItems: "center" }}>
              <span style={{ position: "absolute", left: 9, color: "var(--fg-3)", pointerEvents: "none" }}>
                <Icon name="search" size={12} />
              </span>
              <input
                type="text"
                value={historyQuery}
                onChange={(event) => changeHistoryQuery(event.target.value)}
                placeholder="Cerca trade..."
                className="filter"
                style={{ width: 220, paddingLeft: 28 }}
                aria-label="Cerca trade chiusi e parziali"
              />
            </div>
            <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>
              {historySearchTokens.length > 0 ? `${rows.length}/${modeRows.length}` : "click riga per TP, exit e size residua"}
            </span>
          </div>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table className="tbl" style={{ minWidth: 1120 }}>
            <thead>
              <tr>
                <th style={{ width: 34 }} />
                <th>Data</th>
                <th>Mode</th>
                <th>Symbol</th>
                <th>Side</th>
                <th style={{ textAlign: "right" }}>Size</th>
                <th style={{ textAlign: "right" }}>Residua</th>
                <th style={{ textAlign: "right" }}>Entry</th>
                <th style={{ textAlign: "right" }}>Exit</th>
                <th style={{ textAlign: "right" }}>Return</th>
                <th style={{ textAlign: "right" }}>P&amp;L €</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr>
                  <td colSpan={12} style={{ textAlign: "center", padding: "32px 0", color: "var(--fg-3)" }}>
                    {modeRows.length > 0 && historySearchTokens.length > 0
                      ? "Nessun trade corrisponde alla ricerca"
                      : "Nessuna uscita nel periodo selezionato"}
                  </td>
                </tr>
              )}
              {paginatedRows.map(p => {
                const isLive = p.mode === "live";
                const exitOrders = exitOrdersFor(p, orders);
                const exitPrice = latestExitPrice(p, exitOrders);
                const pnl = realizedPnl(p, exitOrders);
                const pct = positionReturnPct(p, exitOrders);
                const r = positionRMultiple(p, exitOrders);
                const notional = positionNotional(p);
                const tone = pnlTone(pnl);
                const status = positionStatusLabel(p, exitOrders);
                const expanded = expandedPositionId === p.id;
                return (
                  <React.Fragment key={p.id}>
                    <tr
                      style={{ background: isLive ? "var(--bg-1)" : "transparent", cursor: "pointer" }}
                      onClick={() => setExpandedPositionId(expanded ? null : p.id)}
                    >
                      <td>
                        <span className="muted" style={{ display: "inline-flex", transform: expanded ? "rotate(90deg)" : "none" }}>
                          <Icon name="chevron" size={12} />
                        </span>
                      </td>
                      <td className="muted mono" style={{ whiteSpace: "nowrap" }}>
                        <span style={{ display: "inline-block", width: 2, height: 22, background: isLive ? "var(--short)" : "transparent", borderRadius: 1, marginRight: 8, verticalAlign: "middle" }} />
                        {new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(p.opened_at))}
                      </td>
                      <td><ModeChip mode={p.mode} /></td>
                      <td className="fg">
                        <strong>{p.market?.symbol ?? "—"}</strong>
                        {p.market?.market_type && p.market.market_type !== "perp" && (
                          <span className="badge muted" style={{ marginLeft: 6, fontSize: 9 }}>{p.market.market_type}</span>
                        )}
                      </td>
                      <td>
                        <span className={"dir mono " + (p.side.toUpperCase() === "LONG" ? "long" : "short")}>
                          {p.side.toUpperCase() === "LONG" ? "↑" : "↓"} {p.side.toUpperCase()}
                        </span>
                      </td>
                      <td className="num fg" style={{ opacity: isLive ? 1 : 0.78 }}>
                        <div>
                          {fmtSize(displaySize(p))}
                          {p.market?.base && <span className="muted" style={{ marginLeft: 4 }}>{p.market.base}</span>}
                        </div>
                        <div className="muted mono" style={{ fontSize: 10, marginTop: 1 }}>
                          {fmt.money(notional)}
                        </div>
                      </td>
                      <td className="num fg">{p.status === "closed" ? "—" : fmtSize(p.size)}</td>
                      <td className="num fg" style={{ opacity: isLive ? 1 : 0.78 }}>{fmt.price(p.entry_price)}</td>
                      <td className="num fg" style={{ opacity: isLive ? 1 : 0.78 }}>{fmt.price(exitPrice)}</td>
                      <td className="num" style={{ whiteSpace: "nowrap" }}>
                        <ReturnCell pnl={pnl} pct={pct} r={r} isLive={isLive} />
                      </td>
                      <td className={"num " + tone}>{pnl !== null ? fmt.pnl(pnl) : "—"}</td>
                      <td>
                        <span className={"badge " + (status === "partial" ? "warn" : "muted")}>{status}</span>
                      </td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td colSpan={12} style={{ padding: 0, background: "var(--bg)" }}>
                          <PositionDetails position={p} orders={exitOrders} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        <PaginationControls
          total={rows.length}
          page={historySafePage}
          pageSize={historyPageSize}
          onPageChange={(page) => {
            setHistoryPage(page);
            setExpandedPositionId(null);
          }}
          onPageSizeChange={(pageSize) => {
            setHistoryPageSize(pageSize);
            setHistoryPage(1);
            setExpandedPositionId(null);
          }}
        />
      </div>
    </div>
  );
}

/* ── components ──────────────────────────────────────────────────────────── */
function ModeSeg({ value, onChange, counts }: {
  value: ModeFilter;
  onChange: (v: ModeFilter) => void;
  counts: Record<string, number>;
}) {
  const opts: { v: ModeFilter; label: string; dotColor: string | null }[] = [
    { v: "all",    label: "Tutti",       dotColor: null },
    { v: "live",   label: "Live",        dotColor: "var(--short)" },
    { v: "paper",  label: "Paper",       dotColor: "var(--warn)" },
  ];
  return (
    <div className="seg">
      {opts.map(o => (
        <button key={o.v} className={"seg-opt " + (value === o.v ? "active" : "")} onClick={() => onChange(o.v)}
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {o.dotColor && <span style={{ width: 5, height: 5, borderRadius: "50%", background: o.dotColor }} />}
          <span>{o.label}</span>
          <span className="mono" style={{ fontSize: 9, color: "var(--fg-3)" }}>{counts[o.v]}</span>
        </button>
      ))}
    </div>
  );
}

function MarketTypeSeg({ value, onChange, counts }: {
  value: "all" | "perp" | "spot";
  onChange: (v: "all" | "perp" | "spot") => void;
  counts: Record<string, number>;
}) {
  const opts: { v: "all" | "perp" | "spot"; label: string }[] = [
    { v: "all",  label: "Tutti" },
    { v: "perp", label: "Perp" },
    { v: "spot", label: "Spot" },
  ];
  return (
    <div className="seg">
      {opts.map(o => (
        <button key={o.v} className={"seg-opt " + (value === o.v ? "active" : "")} onClick={() => onChange(o.v)}
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span>{o.label}</span>
          <span className="mono" style={{ fontSize: 9, color: "var(--fg-3)" }}>{counts[o.v]}</span>
        </button>
      ))}
    </div>
  );
}

function StatusSeg({ value, onChange, counts }: {
  value: "all" | "closed" | "partial";
  onChange: (v: "all" | "closed" | "partial") => void;
  counts: Record<string, number>;
}) {
  const opts: { v: "all" | "closed" | "partial"; label: string }[] = [
    { v: "all",     label: "Tutti" },
    { v: "closed",  label: "Closed" },
    { v: "partial", label: "Partial" },
  ];
  return (
    <div className="seg">
      {opts.map(o => (
        <button key={o.v} className={"seg-opt " + (value === o.v ? "active" : "")} onClick={() => onChange(o.v)}
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span>{o.label}</span>
          <span className="mono" style={{ fontSize: 9, color: "var(--fg-3)" }}>{counts[o.v]}</span>
        </button>
      ))}
    </div>
  );
}

function LiveEdgePill({ liveSt, papSt }: { liveSt: ModeStats; papSt: ModeStats }) {
  const liveWr = liveSt.wr, papWr = papSt.wr;
  const edge = liveWr - papWr;
  const tone = edge >= 0 ? "up" : "down";
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 10, padding: "6px 12px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", background: "var(--bg-2)" }}>
      <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Live edge</span>
      <span className={"mono " + tone} style={{ fontSize: "var(--fz-md)", fontWeight: 500 }}>
        {edge >= 0 ? "+" : ""}{edge.toFixed(1)}% WR
      </span>
      <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>live {liveWr.toFixed(0)}% · paper {papWr.toFixed(0)}%</span>
    </div>
  );
}

function ModeBars({ live, paper }: { live: number; paper: number }) {
  const total = live + paper || 1;
  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ display: "flex", height: 8, borderRadius: 2, overflow: "hidden", background: "var(--bg-2)" }}>
        <div style={{ width: `${(live / total) * 100}%`, background: "var(--short)" }} />
        <div style={{ width: `${(paper / total) * 100}%`, background: "var(--warn)" }} />
      </div>
      <div style={{ display: "flex", gap: 10, marginTop: 8, fontSize: "var(--fz-xs)" }} className="mono">
        <span><span className="dot red" style={{ marginRight: 4 }} />{live} live</span>
        <span><span className="dot amber" style={{ marginRight: 4 }} />{paper} paper</span>
      </div>
    </div>
  );
}

function PaginationControls({
  total,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange,
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
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "12px 16px", borderTop: "1px solid var(--border)" }}>
      <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>{start}-{end} di {total}</span>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <select
          value={pageSize}
          onChange={(event) => onPageSizeChange(Number(event.target.value))}
          aria-label="Elementi per pagina"
          style={{
            height: 30,
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            background: "var(--bg-1)",
            color: "var(--fg)",
            fontSize: "var(--fz-xs)",
            padding: "0 8px",
          }}
        >
          {PAGE_SIZE_OPTIONS.map((size) => (
            <option key={size} value={size}>{size} / pagina</option>
          ))}
        </select>
        <button
          className="btn ghost"
          onClick={() => onPageChange(safePage - 1)}
          disabled={safePage <= 1}
          title="Pagina precedente"
          style={{ height: 30, width: 32, padding: 0 }}
        >
          <span style={{ display: "inline-flex", transform: "rotate(180deg)" }}><Icon name="chevron" size={13} /></span>
        </button>
        <span className="muted mono" style={{ minWidth: 46, textAlign: "center", fontSize: "var(--fz-xs)" }}>
          {safePage}/{totalPages}
        </span>
        <button
          className="btn ghost"
          onClick={() => onPageChange(safePage + 1)}
          disabled={safePage >= totalPages}
          title="Pagina successiva"
          style={{ height: 30, width: 32, padding: 0 }}
        >
          <Icon name="chevron" size={13} />
        </button>
      </div>
    </div>
  );
}

function ReturnCell({ pnl, pct, r, isLive }: { pnl: number | null; pct: number | null; r: number | null; isLive: boolean }) {
  const sign = (pct ?? 0) >= 0 ? "+" : "";
  const toneValue = pct ?? pnl ?? 0;
  const tone = toneValue >= 0 ? "up" : "down";
  if (isLive) {
    return (
      <>
        <strong className={tone}>{pnl !== null ? fmt.pnl(pnl) : "—"}</strong>
        <div className={"mono " + tone} style={{ fontSize: 10, marginTop: 1 }}>
          {pct !== null ? `${sign}${pct.toFixed(2)}%` : "—"} · {r !== null ? `${r >= 0 ? "+" : ""}${r.toFixed(1)}R` : "—"}
        </div>
      </>
    );
  }
  return (
    <>
      <strong className={tone} style={{ opacity: 0.88 }}>{pct !== null ? `${sign}${pct.toFixed(2)}%` : "—"}</strong>
      <div className={"mono " + tone} style={{ fontSize: 10, marginTop: 1, opacity: 0.7 }}>
        {r !== null ? `${r >= 0 ? "+" : ""}${r.toFixed(1)}R` : "—"}
      </div>
    </>
  );
}

function ModeChip({ mode }: { mode: string }) {
  if (mode === "live")   return <span className="badge" style={{ color: "var(--short)", borderColor: "var(--short-border)", background: "var(--short-soft)" }}>● LIVE</span>;
  if (mode === "paper")  return <span className="badge" style={{ color: "var(--warn)", borderColor: "#f0b34133", background: "var(--warn-soft)" }}>paper</span>;
  return <span className="badge muted">{mode}</span>;
}

function PositionDetails({ position, orders }: { position: Position; orders: Order[] }) {
  const plan = takeProfitPlan(position);
  const realized = asNumber(position.realized_pnl);
  const initialSize = displaySize(position);
  return (
    <div style={{ padding: "14px 18px 16px", borderBottom: "1px solid var(--border)", display: "grid", gridTemplateColumns: "minmax(260px, 0.8fr) minmax(360px, 1.2fr)", gap: 18 }}>
      <div>
        <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>Piano TP</div>
        {plan.length === 0 ? (
          <div className="muted mono">Nessun piano parziale salvato</div>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {plan.map((item) => {
              const filled = item.filled;
              const allocation = item.allocation != null ? `${Math.round(item.allocation * 100)}%` : "—";
              return (
                <div key={item.level} style={{ display: "grid", gridTemplateColumns: "56px 1fr 54px", alignItems: "center", gap: 10 }}>
                  <span className={"badge " + (filled ? "long" : "muted")}>{item.level.toUpperCase()}</span>
                  <div>
                    <div className="mono fg">{fmt.price(item.price)}</div>
                    <div className="muted mono" style={{ fontSize: 10 }}>{filled ? "raggiunto" : "in attesa"}</div>
                  </div>
                  <span className="mono muted" style={{ textAlign: "right", fontSize: "var(--fz-sm)" }}>{allocation}</span>
                </div>
              );
            })}
          </div>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 14 }}>
          <DetailMetric label="Size iniziale" value={fmtSize(initialSize)} />
          <DetailMetric label="Size residua" value={fmtSize(position.size)} />
          <DetailMetric label="SL" value={fmt.price(position.stop_loss)} />
          <DetailMetric label="P&L real." value={fmt.pnl(realized)} tone={pnlTone(realized) || undefined} />
        </div>
      </div>

      <div>
        <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>Uscite eseguite</div>
        {orders.length === 0 ? (
          <div className="muted mono">Nessuna uscita reduce-only registrata</div>
        ) : (
          <table className="tbl" style={{ background: "var(--bg-1)" }}>
            <thead>
              <tr>
                <th>Evento</th>
                <th>Ora</th>
                <th style={{ textAlign: "right" }}>Prezzo</th>
                <th style={{ textAlign: "right" }}>Size</th>
                <th style={{ textAlign: "right" }}>P&L</th>
                <th style={{ textAlign: "right" }}>R</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => {
                const pnl = exitPnl(position, order);
                const r = exitRContribution(position, order);
                const tone = pnlTone(pnl);
                return (
                  <tr key={order.id}>
                    <td><span className={"badge " + (order.order_type === "stop_loss" ? "short" : "long")}>{exitLabel(order.order_type)}</span></td>
                    <td className="muted mono">{new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(order.created_at))}</td>
                    <td className="num fg">{fmt.price(order.price)}</td>
                    <td className="num fg">{fmtSize(order.size)}</td>
                    <td className={"num " + tone}>{fmt.pnl(pnl)}</td>
                    <td className={"num " + tone}>{r != null ? `${r >= 0 ? "+" : ""}${r.toFixed(2)}R` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

type StrategyRow = {
  name: string;
  trades: number;
  wins: number;
  losses: number;
  wr: number;
  avgR: number | null;
  avgScore: number | null;
  avgContribution: number | null;
  totalPnl: number;
};

function StrategyPerformanceRows({ rows }: { rows: StrategyRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="muted mono" style={{ padding: "18px 18px", fontSize: "var(--fz-sm)" }}>
        Nessun segnale con breakdown di strategia nel periodo
      </div>
    );
  }
  return (
    <div style={{ padding: "4px 18px 14px", overflowX: "auto" }}>
      <div className="muted mono" style={{ display: "grid", gridTemplateColumns: "1.45fr 58px 64px 1fr 82px 76px 76px 96px", minWidth: 840, alignItems: "center", gap: 12, padding: "7px 0", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", borderBottom: "1px solid var(--border)" }}>
        <span>Indicatore</span>
        <span>Trade</span>
        <span>W/L</span>
        <span>WR</span>
        <span style={{ textAlign: "right" }}>Avg R</span>
        <span style={{ textAlign: "right" }}>Score</span>
        <span style={{ textAlign: "right" }}>Contr.</span>
        <span style={{ textAlign: "right" }}>P&L</span>
      </div>
      {rows.map((r) => {
        const tone = pnlTone(r.totalPnl);
        return (
          <div key={r.name} style={{ display: "grid", gridTemplateColumns: "1.45fr 58px 64px 1fr 82px 76px 76px 96px", minWidth: 840, alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
            <span style={{ fontSize: "var(--fz-sm)" }}>{r.name}</span>
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{r.trades}t</span>
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{r.wins}W/{r.losses}L</span>
            <WrBar wr={r.wr} />
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
              {r.avgR !== null ? `${r.avgR >= 0 ? "+" : ""}${r.avgR.toFixed(1)}R` : "—"}
            </span>
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
              {r.avgScore !== null ? r.avgScore.toFixed(0) : "—"}
            </span>
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
              {r.avgContribution !== null ? r.avgContribution.toFixed(1) : "—"}
            </span>
            <span className={"mono " + tone} style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
              {fmt.pnl(r.totalPnl)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function WrBar({ wr }: { wr: number }) {
  const barColor = wr >= 60 ? "var(--long)" : wr >= 50 ? "var(--warn)" : "var(--short)";
  return (
    <div style={{ position: "relative", height: 6, background: "var(--bg-2)", borderRadius: 3, overflow: "hidden" }}>
      <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${wr}%`, background: barColor, borderRadius: 3 }} />
    </div>
  );
}

function DetailMetric({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", padding: "8px 10px", background: "var(--bg-1)" }}>
      <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 3 }}>{label}</div>
      <div className={"mono " + (tone ?? "fg")}>{value}</div>
    </div>
  );
}

type TimeframeRow = {
  timeframe: string;
  trades: number;
  wins: number;
  losses: number;
  wr: number;
  avgR: number | null;
  totalPnl: number;
};

function TimeframePerformanceRows({ rows }: { rows: TimeframeRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="muted mono" style={{ padding: "18px 18px", fontSize: "var(--fz-sm)" }}>
        Nessun segnale con timeframe nel periodo
      </div>
    );
  }
  return (
    <div style={{ padding: "4px 18px 14px", overflowX: "auto" }}>
      <div className="muted mono" style={{ display: "grid", gridTemplateColumns: "1fr 58px 64px 1fr 82px 96px", minWidth: 560, alignItems: "center", gap: 12, padding: "7px 0", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", borderBottom: "1px solid var(--border)" }}>
        <span>Timeframe</span>
        <span>Trade</span>
        <span>W/L</span>
        <span>WR</span>
        <span style={{ textAlign: "right" }}>Avg R</span>
        <span style={{ textAlign: "right" }}>P&L</span>
      </div>
      {rows.map((r) => {
        const tone = pnlTone(r.totalPnl);
        return (
          <div key={r.timeframe} style={{ display: "grid", gridTemplateColumns: "1fr 58px 64px 1fr 82px 96px", minWidth: 560, alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
            <span className="mono fg" style={{ fontSize: "var(--fz-sm)" }}>{r.timeframe}</span>
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{r.trades}t</span>
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{r.wins}W/{r.losses}L</span>
            <WrBar wr={r.wr} />
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
              {r.avgR !== null ? `${r.avgR >= 0 ? "+" : ""}${r.avgR.toFixed(1)}R` : "—"}
            </span>
            <span className={"mono " + tone} style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
              {fmt.pnl(r.totalPnl)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function toCumulative(posns: Position[]): EquityPoint[] {
  const sorted = [...posns]
    .filter(p => p.realized_pnl != null)
    .sort((a, b) => positionEventTime(a) - positionEventTime(b));
  if (sorted.length === 0) return [];

  let cumPnl = 0;
  let cumNotional = 0;
  const firstTime = positionEventTime(sorted[0]);
  const points: EquityPoint[] = [{
    date: new Date(firstTime),
    time: firstTime,
    value: 0,
    pct: 0,
    trades: 0,
    label: "Inizio",
  }];

  sorted.forEach((position, index) => {
    cumPnl += asNumber(position.realized_pnl) ?? 0;
    const notional = positionNotional(position);
    if (notional !== null && notional > 0) cumNotional += notional;
    const time = positionEventTime(position);
    points.push({
      date: new Date(time),
      time,
      value: cumPnl,
      pct: cumNotional > 0 ? (cumPnl / cumNotional) * 100 : null,
      trades: index + 1,
      label: position.market?.symbol ?? `Trade ${index + 1}`,
    });
  });

  return points;
}

function DualEquityCurve({ live, paper }: { live: Position[]; paper: Position[] }) {
  const liveData  = toCumulative(live);
  const paperData = toCumulative(paper);
  const [hover, setHover] = useState<EquityHover | null>(null);

  const W = 520, H = 172, padL = 10, padR = 58, padT = 12, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const allPoints = [...liveData, ...paperData];
  const hasData = liveData.length > 1 || paperData.length > 1;
  const pctValues = allPoints.map(point => point.pct).filter((value): value is number => value !== null);
  const minPctRaw = Math.min(0, ...pctValues);
  const maxPctRaw = Math.max(0, ...pctValues);
  const pctPad = Math.max((maxPctRaw - minPctRaw) * 0.14, 0.25);
  const minPct = minPctRaw - pctPad;
  const maxPct = maxPctRaw + pctPad;
  const pctRange = maxPct - minPct || 1;
  const times = allPoints.map(point => point.time).filter(Number.isFinite);
  const minTime = times.length > 0 ? Math.min(...times) : Date.now();
  const maxTime = times.length > 0 ? Math.max(...times) : minTime;
  const timeRange = maxTime - minTime || 1;
  const yTicks = [maxPct, (maxPct + minPct) / 2, minPct];
  const xTicks = timeRange === 1
    ? [minTime]
    : [minTime, minTime + timeRange / 2, maxTime];

  const xFor = (point: EquityPoint) => padL + ((point.time - minTime) / timeRange) * plotW;
  const yFor = (pct: number) => padT + (1 - (pct - minPct) / pctRange) * plotH;
  const plotPoints = (data: EquityPoint[]) => data.filter((point) => point.pct !== null);
  const ptsFor = (data: EquityPoint[]) => plotPoints(data)
    .map((point) => `${xFor(point).toFixed(1)},${yFor(point.pct as number).toFixed(1)}`)
    .join(" ");
  const dateLabel = (date: Date) => new Intl.DateTimeFormat("it-IT", { day: "2-digit", month: "2-digit" }).format(date);
  const fullDateLabel = (date: Date) => new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(date);

  const zero = yFor(0);
  const renderSeries = (data: EquityPoint[], color: string, gradientId: string, series: string, dashed = false) => {
    const points = plotPoints(data);
    if (points.length <= 1) return null;
    const first = points[0];
    const last = points[points.length - 1];
    return (
      <>
        <polygon
          points={`${xFor(first).toFixed(1)},${zero.toFixed(1)} ${ptsFor(data)} ${xFor(last).toFixed(1)},${zero.toFixed(1)}`}
          fill={`url(#${gradientId})`}
        />
        <polyline
          points={ptsFor(data)}
          fill="none"
          stroke={color}
          strokeWidth={series === "Live" ? 1.8 : 1.4}
          strokeDasharray={dashed ? "3 3" : undefined}
          strokeLinejoin="round"
        />
        {points.map((point) => {
          const cx = xFor(point);
          const cy = yFor(point.pct as number);
          const title = `${series} · ${point.label} · ${fmt.pct(point.pct)} · ${fmt.pnl(point.value)} · ${fullDateLabel(point.date)}`;
          return (
            <React.Fragment key={`${series}-${point.trades}-${point.time}`}>
              <circle
                cx={cx}
                cy={cy}
                r={point.trades === last.trades ? 3.4 : 2.4}
                fill={color}
                opacity={point.trades === 0 ? 0.45 : 1}
              />
              <circle
                cx={cx}
                cy={cy}
                r={8}
                fill="transparent"
                style={{ cursor: "crosshair" }}
                onMouseEnter={() => setHover({ point, series, color, x: cx, y: cy })}
                onMouseMove={() => setHover({ point, series, color, x: cx, y: cy })}
                onMouseLeave={() => setHover(null)}
              >
                <title>{title}</title>
              </circle>
            </React.Fragment>
          );
        })}
      </>
    );
  };

  const tooltipX = hover ? Math.max(8, Math.min(W - 148, hover.x + (hover.x > W - 180 ? -138 : 10))) : 0;
  const tooltipY = hover ? Math.max(8, Math.min(H - 62, hover.y + (hover.y < 58 ? 12 : -54))) : 0;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: 172 }}>
      <defs>
        <linearGradient id="paperg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--warn)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--warn)" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="liveg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--short)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--short)" stopOpacity="0" />
        </linearGradient>
      </defs>
      {hasData && yTicks.map((tick) => {
        const y = yFor(tick);
        return (
          <React.Fragment key={tick.toFixed(4)}>
            <line x1={padL} y1={y} x2={W - padR} y2={y} stroke="var(--fg-4)" strokeDasharray="2 4" strokeWidth="0.5" opacity={0.7} />
            <text x={W - padR + 7} y={y + 3} fill="var(--fg-3)" fontSize="10" fontFamily="var(--font-mono)">{fmt.pct(tick)}</text>
          </React.Fragment>
        );
      })}
      {hasData && <line x1={padL} y1={zero} x2={W - padR} y2={zero} stroke="var(--fg-3)" strokeDasharray="2 3" strokeWidth="0.7" />}
      {hasData && xTicks.map((time) => {
        const x = padL + ((time - minTime) / timeRange) * plotW;
        return (
          <React.Fragment key={time}>
            <line x1={x} y1={padT} x2={x} y2={H - padB} stroke="var(--fg-4)" strokeWidth="0.5" opacity={0.35} />
            <text x={x} y={H - 7} textAnchor="middle" fill="var(--fg-3)" fontSize="10" fontFamily="var(--font-mono)">{dateLabel(new Date(time))}</text>
          </React.Fragment>
        );
      })}
      {renderSeries(paperData, "var(--warn)", "paperg", "Paper", true)}
      {renderSeries(liveData, "var(--short)", "liveg", "Live")}
      {liveData.length <= 1 && paperData.length <= 1 && (
        <text x={W / 2} y={H / 2} textAnchor="middle" fill="var(--fg-3)" fontSize="11" fontFamily="var(--font-mono)">nessun dato storico</text>
      )}
      {hover && (
        <g pointerEvents="none">
          <rect x={tooltipX} y={tooltipY} width={138} height={46} rx={4} fill="var(--bg-2)" stroke="var(--border)" />
          <circle cx={tooltipX + 10} cy={tooltipY + 13} r={3} fill={hover.color} />
          <text x={tooltipX + 18} y={tooltipY + 16} fill="var(--fg)" fontSize="10" fontFamily="var(--font-mono)">{hover.series} · {hover.point.label}</text>
          <text x={tooltipX + 8} y={tooltipY + 30} fill="var(--fg-2)" fontSize="10" fontFamily="var(--font-mono)">{fmt.pct(hover.point.pct)} · {fmt.pnl(hover.point.value)}</text>
          <text x={tooltipX + 8} y={tooltipY + 42} fill="var(--fg-3)" fontSize="9" fontFamily="var(--font-mono)">{fullDateLabel(hover.point.date)}</text>
        </g>
      )}
    </svg>
  );
}

"use client";

import React, { useMemo, useState } from "react";
import { Icon, Seg, fmt } from "@/components/atoms";
import { Position, Signal, Order } from "@/lib/api";

interface Props {
  positions: Position[];
  signals: Signal[];
  orders: Order[];
}

type ModeFilter = "all" | "live" | "paper" | "signal";
type ModeStats = {
  count: number;
  wins: number;
  losses: number;
  wr: number;
  total: number;
};
type TpPlanItem = {
  level: string;
  price: string;
  allocation: number | null;
  filled: boolean;
};

const EXIT_ORDER_TYPES = new Set(["tp1", "tp2", "tp3", "take_profit", "stop_loss", "manual"]);

function asNumber(v: unknown): number | null {
  if (v == null) return null;
  const n = typeof v === "number" ? v : Number(v);
  return isFinite(n) ? n : null;
}

function sideMultiplier(side: string): 1 | -1 {
  return side.toLowerCase() === "short" ? -1 : 1;
}

function pctChange(entry: string, exit: string | null, side: string): number | null {
  const e = asNumber(entry), x = asNumber(exit);
  if (e == null || x == null || e === 0) return null;
  return (((x - e) * sideMultiplier(side)) / e) * 100;
}

function calcR(entry: string, exit: string | null, sl: string | null, side: string): number | null {
  const e = asNumber(entry), x = asNumber(exit), s = asNumber(sl);
  if (e == null || x == null || s == null || Math.abs(e - s) === 0) return null;
  return ((x - e) * sideMultiplier(side)) / Math.abs(e - s);
}

function fmtSize(value: string | null | undefined): string {
  const n = asNumber(value);
  if (n == null) return "—";
  const abs = Math.abs(n);
  const maxDigits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return n.toLocaleString("en-US", { maximumFractionDigits: maxDigits });
}

function displaySize(position: Position): string {
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
    .filter(order => order.position_id === position.id && EXIT_ORDER_TYPES.has(order.order_type))
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
  const size = asNumber(displaySize(position));
  const entry = asNumber(position.entry_price);
  if (size == null || entry == null) return null;
  return size * entry;
}

function positionSourceSignalId(position: Position) {
  const parsed = asNumber(position.metadata_json?.source_signal_id);
  return parsed === null ? null : Math.trunc(parsed);
}

function signalHasOpenPosition(signal: Signal, positions: Position[]) {
  return positions.some((position) => (
    position.status.toLowerCase() === "open" && positionSourceSignalId(position) === signal.id
  ));
}

/* ─────────────────────────────────────────────────────────────────────────── */
export function HistoryScreen({ positions, signals, orders }: Props) {
  const [modeFilter, setModeFilter] = useState<ModeFilter>("all");
  const [periodFilter, setPeriodFilter] = useState("7d");
  const [expandedPositionId, setExpandedPositionId] = useState<number | null>(null);

  const trackedPositions = useMemo(() => positions.filter(p => hasExitActivity(p, orders)), [positions, orders]);

  const cutoff = useMemo(() => {
    const now = Date.now();
    if (periodFilter === "1d")  return now - 86400000;
    if (periodFilter === "7d")  return now - 7 * 86400000;
    if (periodFilter === "30d") return now - 30 * 86400000;
    return 0;
  }, [periodFilter]);

  const periodFiltered = useMemo(
    () => trackedPositions.filter(p => new Date(p.opened_at).getTime() >= cutoff),
    [trackedPositions, cutoff]
  );

  const closedSignals = useMemo(
    () => signals.filter(s => (
      (s.status === "closed" || s.status === "ignored" || (s.status === "executed" && !signalHasOpenPosition(s, positions)))
      && new Date(s.created_at).getTime() >= cutoff
    )),
    [positions, signals, cutoff]
  );

  const rows = useMemo(
    () => periodFiltered.filter(p => modeFilter === "all" || p.mode === modeFilter),
    [periodFiltered, modeFilter]
  );

  const live   = useMemo(() => periodFiltered.filter(p => p.mode === "live"),  [periodFiltered]);
  const paper  = useMemo(() => periodFiltered.filter(p => p.mode === "paper"), [periodFiltered]);
  const signal = closedSignals;

  const statsFor = (items: Position[]) => {
    const pnls = items.map(p => asNumber(p.realized_pnl)).filter((v): v is number => v !== null);
    const wins = pnls.filter(v => v > 0).length;
    return {
      count: items.length,
      wins,
      losses: pnls.length - wins,
      wr: pnls.length > 0 ? (wins / pnls.length) * 100 : 0,
      total: pnls.reduce((a, b) => a + b, 0),
    };
  };

  const all   = statsFor(rows);
  const liveSt = statsFor(live);
  const papSt  = statsFor(paper);

  const strategyStats = useMemo(() => {
    const signalMap = new Map<number, Signal>();
    for (const s of signals) signalMap.set(s.id, s);

    type Acc = { count: number; wins: number; totalPnl: number; totalR: number; rCount: number };
    const map = new Map<string, Acc>();

    for (const p of periodFiltered) {
      const sigId = positionSourceSignalId(p);
      if (sigId === null) continue;
      const sig = signalMap.get(sigId);
      if (!sig) continue;

      const breakdown = sig.strategy_breakdown as Record<string, Record<string, unknown>>;
      const pnl = asNumber(p.realized_pnl);
      const exitOrds = exitOrdersFor(p, orders);
      const exitPrice = latestExitPrice(p, exitOrds);
      const r = calcR(p.entry_price, exitPrice, p.stop_loss, p.side);

      for (const [name, info] of Object.entries(breakdown)) {
        if (name.startsWith("_") || typeof info !== "object" || info === null) continue;
        const dir = info.direction as string;
        if (!dir || dir === "NEUTRAL") continue;
        const contribution = asNumber(info.contribution as unknown);
        if (contribution === null || contribution <= 0) continue;

        const acc = map.get(name) ?? { count: 0, wins: 0, totalPnl: 0, totalR: 0, rCount: 0 };
        acc.count++;
        if (pnl !== null && pnl > 0) acc.wins++;
        if (pnl !== null) acc.totalPnl += pnl;
        if (r !== null) { acc.totalR += r; acc.rCount++; }
        map.set(name, acc);
      }
    }

    return Array.from(map.entries())
      .map(([name, st]) => ({
        name,
        trades: st.count,
        wins: st.wins,
        wr: st.count > 0 ? (st.wins / st.count) * 100 : 0,
        avgR: st.rCount > 0 ? st.totalR / st.rCount : null,
        totalPnl: st.totalPnl,
      }))
      .sort((a, b) => b.trades - a.trades);
  }, [periodFiltered, signals, orders]);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Storico segnali</h1>
          <div className="page-sub">
            {rows.length} posizioni con exit · {live.length} live · {paper.length} paper · {signal.length} signal-only
          </div>
        </div>
        <div className="page-actions">
          <Seg value={periodFilter} onChange={setPeriodFilter} options={[["1d","1d"],["7d","7d"],["30d","30d"],["all","All"]]} />
          <button className="btn ghost"><Icon name="dl" size={12} /> Export</button>
        </div>
      </div>

      {/* Mode filter */}
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: "var(--gap)", padding: "10px 14px", background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Mode</span>
        <ModeSeg
          value={modeFilter}
          onChange={setModeFilter}
          counts={{ all: periodFiltered.length, live: live.length, paper: paper.length, signal: signal.length }}
        />
        <div style={{ flex: 1 }} />
        <LiveEdgePill liveSt={liveSt} papSt={papSt} />
      </div>

      {/* Stats */}
      <div className="stat-grid" style={{ marginBottom: "var(--gap)" }}>
        <div className="stat">
          <div className="stat-label">Win rate</div>
          <div className="stat-value">{all.wr.toFixed(0)}<span style={{ color: "var(--fg-3)" }}>%</span></div>
          <div className="stat-delta"><span className="mono">{all.wins}W</span> · <span className="mono">{all.losses}L</span></div>
        </div>
        <div className="stat">
          <div className="stat-label">P&amp;L totale</div>
          <div className={"stat-value " + (all.total >= 0 ? "up" : "down")}>{fmt.pnl(all.total)}</div>
          <div className="stat-delta">{all.count} posizioni filtrate</div>
        </div>
        <div className="stat">
          <div className="stat-label">{modeFilter === "live" ? "Net P&L live €" : "Trade totali"}</div>
          {modeFilter === "live" ? (
            <>
              <div className={"stat-value " + (liveSt.total >= 0 ? "up" : "down")}>{fmt.pnl(liveSt.total)}</div>
              <div className="stat-delta">su {liveSt.count} posizioni live</div>
            </>
          ) : (
            <>
              <div className="stat-value">{all.count}</div>
              <div className="stat-delta">{live.length} live · {paper.length} paper · {signal.length} signal</div>
            </>
          )}
        </div>
        <div className="stat">
          <div className="stat-label">Distribuzione</div>
          <ModeBars live={live.length} paper={paper.length} sig={signal.length} />
        </div>
      </div>

      {/* Per-strategy + equity curve */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap)", marginBottom: "var(--gap)" }}>
        <div className="card">
          <div className="card-head">
            <span className="title">Performance per modalità</span>
          </div>
          <div style={{ padding: "4px 18px 14px" }}>
            {[
              { label: "Live",        stats: liveSt, color: "var(--short)" },
              { label: "Paper",       stats: papSt,  color: "var(--warn)" },
            ].map((row, i) => (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "80px 60px 1fr 80px", alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
                <span style={{ fontSize: "var(--fz-sm)", display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ width: 5, height: 5, borderRadius: "50%", background: row.color, display: "inline-block" }} />
                  {row.label}
                </span>
                <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{row.stats.count}t</span>
                <WrBar wr={row.stats.wr} />
                <span className={"mono " + (row.stats.total >= 0 ? "up" : "down")} style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
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
              <span className="mono"><span className="dot green" /> Live</span>
              <span className="mono"><span className="dot amber" /> Paper</span>
            </div>
          </div>
          <div className="card-body"><DualEquityCurve live={live} paper={paper} /></div>
        </div>
      </div>

      {/* Performance per strategia */}
      <div className="card" style={{ marginBottom: "var(--gap)" }}>
        <div className="card-head">
          <span className="title">Performance per strategia</span>
          <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>tutti i mode · periodo selezionato</div>
        </div>
        <StrategyPerformanceRows rows={strategyStats} />
      </div>

      {/* History table */}
      <div className="card">
        <div className="card-head">
          <span className="title">
            {modeFilter === "all" ? "Trade chiusi e parziali" :
             modeFilter === "live" ? "Trade live" :
             modeFilter === "paper" ? "Trade paper" : "Segnali signal-only"}
          </span>
          <div className="right">
            <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>click riga per TP, exit e size residua</span>
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
                <tr><td colSpan={12} style={{ textAlign: "center", padding: "32px 0", color: "var(--fg-3)" }}>Nessuna uscita nel periodo selezionato</td></tr>
              )}
              {rows.map(p => {
                const isLive = p.mode === "live";
                const exitOrders = exitOrdersFor(p, orders);
                const exitPrice = latestExitPrice(p, exitOrders);
                const pnl = asNumber(p.realized_pnl);
                const pct = pctChange(p.entry_price, exitPrice, p.side);
                const r = calcR(p.entry_price, exitPrice, p.stop_loss, p.side);
                const notional = positionNotional(p);
                const tone = pnl === null ? "" : pnl >= 0 ? "up" : "down";
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
                      <td className="fg"><strong>{p.market?.symbol ?? "—"}</strong></td>
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
    { v: "signal", label: "Signal-only", dotColor: "var(--info)" },
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

function ModeBars({ live, paper, sig }: { live: number; paper: number; sig: number }) {
  const total = live + paper + sig || 1;
  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ display: "flex", height: 8, borderRadius: 2, overflow: "hidden", background: "var(--bg-2)" }}>
        <div style={{ width: `${(live / total) * 100}%`, background: "var(--short)" }} />
        <div style={{ width: `${(paper / total) * 100}%`, background: "var(--warn)" }} />
        <div style={{ width: `${(sig / total) * 100}%`, background: "var(--info)" }} />
      </div>
      <div style={{ display: "flex", gap: 10, marginTop: 8, fontSize: "var(--fz-xs)" }} className="mono">
        <span><span className="dot red" style={{ marginRight: 4 }} />{live} live</span>
        <span><span className="dot amber" style={{ marginRight: 4 }} />{paper} paper</span>
        <span><span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--info)", display: "inline-block", marginRight: 4 }} />{sig} signal</span>
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
  if (mode === "signal") return <span className="badge" style={{ color: "var(--info)", borderColor: "#5b8def33", background: "var(--info-soft)" }}>signal</span>;
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
          <DetailMetric label="P&L real." value={fmt.pnl(realized)} tone={realized == null ? undefined : realized >= 0 ? "up" : "down"} />
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
                const r = calcR(position.entry_price, order.price, position.stop_loss, position.side);
                const tone = pnl == null ? "" : pnl >= 0 ? "up" : "down";
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

type StrategyRow = { name: string; trades: number; wins: number; wr: number; avgR: number | null; totalPnl: number };

function StrategyPerformanceRows({ rows }: { rows: StrategyRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="muted mono" style={{ padding: "18px 18px", fontSize: "var(--fz-sm)" }}>
        Nessun segnale con breakdown di strategia nel periodo
      </div>
    );
  }
  return (
    <div style={{ padding: "4px 18px 14px" }}>
      {rows.map((r) => {
        const tone = r.totalPnl >= 0 ? "up" : "down";
        return (
          <div key={r.name} style={{ display: "grid", gridTemplateColumns: "1.6fr 60px 1fr 100px 88px", alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
            <span style={{ fontSize: "var(--fz-sm)" }}>{r.name}</span>
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{r.trades}t</span>
            <WrBar wr={r.wr} />
            <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
              {r.wr.toFixed(0)}% · {r.avgR !== null ? `${r.avgR >= 0 ? "+" : ""}${r.avgR.toFixed(1)}R` : "—"}
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

function toCumulative(posns: Position[]): number[] {
  const sorted = [...posns]
    .filter(p => p.realized_pnl != null)
    .sort((a, b) => new Date(a.closed_at ?? a.opened_at).getTime() - new Date(b.closed_at ?? b.opened_at).getTime());
  let cum = 0;
  return [0, ...sorted.map(p => {
    cum += asNumber(p.realized_pnl) ?? 0;
    return cum;
  })];
}

function DualEquityCurve({ live, paper }: { live: Position[]; paper: Position[] }) {
  const liveData  = toCumulative(live);
  const paperData = toCumulative(paper);

  const W = 460, H = 140, pad = 8;
  const allv = [...liveData, ...paperData];
  const min = Math.min(...allv), max = Math.max(...allv);
  const range = max - min || 1;

  const xs = (i: number, len: number) => pad + (len <= 1 ? 0 : (i / (len - 1)) * (W - pad * 2));
  const ys = (v: number) => pad + (1 - (v - min) / range) * (H - pad * 2);
  const ptsFor = (data: number[]) => data.map((v, i) => `${xs(i, data.length).toFixed(1)},${ys(v).toFixed(1)}`).join(" ");

  const zero = ys(0);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: 140 }}>
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
      <line x1={0} y1={zero} x2={W} y2={zero} stroke="var(--fg-4)" strokeDasharray="2 3" strokeWidth="0.5" />
      {paperData.length > 1 && <>
        <polygon points={`${xs(0, paperData.length)},${H - pad} ${ptsFor(paperData)} ${xs(paperData.length - 1, paperData.length)},${H - pad}`} fill="url(#paperg)" />
        <polyline points={ptsFor(paperData)} fill="none" stroke="var(--warn)" strokeWidth="1.4" strokeDasharray="3 3" strokeLinejoin="round" />
        <circle cx={xs(paperData.length - 1, paperData.length)} cy={ys(paperData[paperData.length - 1])} r="3" fill="var(--warn)" />
      </>}
      {liveData.length > 1 && <>
        <polygon points={`${xs(0, liveData.length)},${H - pad} ${ptsFor(liveData)} ${xs(liveData.length - 1, liveData.length)},${H - pad}`} fill="url(#liveg)" />
        <polyline points={ptsFor(liveData)} fill="none" stroke="var(--short)" strokeWidth="1.8" strokeLinejoin="round" />
        <circle cx={xs(liveData.length - 1, liveData.length)} cy={ys(liveData[liveData.length - 1])} r="3" fill="var(--short)" />
      </>}
      {liveData.length <= 1 && paperData.length <= 1 && (
        <text x={W / 2} y={H / 2} textAnchor="middle" fill="var(--fg-3)" fontSize="11" fontFamily="var(--font-mono)">nessun dato storico</text>
      )}
    </svg>
  );
}

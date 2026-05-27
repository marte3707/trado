"use client";

import React, { useMemo, useState } from "react";
import { Icon, fmt } from "@/components/atoms";
import { Signal, Position, Order, Strategy } from "@/lib/api";

interface Props {
  signals: Signal[];
  positions: Position[];
  orders: Order[];
  strategies: Strategy[];
}

type Tab = "ranking" | "segnale" | "confronto" | "combinazioni" | "heatmap";
type SortKey = "wr" | "trades" | "avgR" | "pnl" | "pf" | "agreement";
type SortDir = "desc" | "asc";

const TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"];

/* ── helpers ──────────────────────────────────────────────────────────────── */
function asNum(v: unknown): number | null {
  if (v == null) return null;
  const n = typeof v === "number" ? v : Number(v);
  return isFinite(n) ? n : null;
}

function exitOrdersFor(pos: Position, orders: Order[]): Order[] {
  const EXIT = new Set(["tp1", "tp2", "tp3", "take_profit", "stop_loss", "manual"]);
  return orders.filter(o => o.position_id === pos.id && EXIT.has(o.order_type) && o.status === "filled");
}

function realizedPnl(pos: Position, exitOrders: Order[]): number | null {
  const stored = asNum(pos.realized_pnl);
  if (stored !== null) return stored;
  const side = pos.side.toLowerCase() === "short" ? -1 : 1;
  const entry = asNum(pos.entry_price);
  if (entry === null) return null;
  const parts = exitOrders
    .map(o => { const p = asNum(o.price); const s = asNum(o.size); return p !== null && s !== null ? (p - entry) * s * side : null; })
    .filter((x): x is number => x !== null);
  return parts.length > 0 ? parts.reduce((a, b) => a + b, 0) : null;
}

function rMultiple(pos: Position, exitOrders: Order[]): number | null {
  const stored = asNum(pos.realized_r_multiple);
  if (stored !== null) return stored;
  const pnl = realizedPnl(pos, exitOrders);
  const risk = asNum(pos.initial_risk) ?? (() => {
    const e = asNum(pos.entry_price), sl = asNum(pos.stop_loss), sz = asNum(pos.initial_size ?? pos.size);
    return e !== null && sl !== null && sz !== null ? Math.abs(e - sl) * sz : null;
  })();
  if (pnl === null || risk === null || risk === 0) return null;
  return pnl / risk;
}

function srcSignalId(pos: Position): number | null {
  const v = asNum(pos.metadata_json?.source_signal_id);
  return v !== null ? Math.trunc(v) : null;
}

function sortedTfs(tfs: string[]): string[] {
  return [
    ...TF_ORDER.filter(t => tfs.includes(t)),
    ...tfs.filter(t => !TF_ORDER.includes(t)).sort(),
  ];
}

/* ── types ────────────────────────────────────────────────────────────────── */
type TfState = { enabled: boolean; weight: number };

type StratStat = {
  name: string;
  trades: number;
  wins: number;
  losses: number;
  wr: number;
  grossProfit: number;
  grossLoss: number;
  pf: number | null;
  totalPnl: number;
  totalR: number;
  rCount: number;
  avgR: number | null;
  avgContribution: number;
  contributionCount: number;
  avgConfidence: number;
  confidenceCount: number;
  agreements: number;
  disagreements: number;
  agreementRate: number | null;
  tfStates: Record<string, TfState>;
};

type CoalitionEntry = { name: string; weight: number };
type CoalitionStat = {
  fingerprint: string;
  strategies: CoalitionEntry[];
  trades: number; wins: number; losses: number; wr: number;
  totalR: number; rCount: number; avgR: number | null;
  avgScore: number; scoreCount: number;
};
type ConsensusLevel = {
  count: number;
  trades: number; wins: number; wr: number;
  totalR: number; rCount: number; avgR: number | null;
  avgScore: number;
};
type ScoreBracket = {
  label: string; min: number; max: number;
  trades: number; wins: number; wr: number;
  totalR: number; rCount: number; avgR: number | null;
};

/* ── compute strategy stats ───────────────────────────────────────────────── */
function computeStats(
  signals: Signal[],
  positions: Position[],
  orders: Order[],
  strategies: Strategy[],
  tfFilter: string | null,
): StratStat[] {
  const sigMap = new Map<number, Signal>();
  for (const s of signals) sigMap.set(s.id, s);

  // name → tf → Strategy (correct: one entry per name+tf pair)
  const stratCfg = new Map<string, Map<string, Strategy>>();
  for (const s of strategies) {
    if (!stratCfg.has(s.strategy_name)) stratCfg.set(s.strategy_name, new Map());
    stratCfg.get(s.strategy_name)!.set(s.timeframe, s);
  }

  type Acc = {
    wins: number; losses: number; gp: number; gl: number; totalPnl: number;
    totalR: number; rCount: number; totalContr: number; contrCount: number;
    totalConf: number; confCount: number; agreements: number; disagreements: number;
  };
  const map = new Map<string, Acc>();

  for (const pos of positions) {
    const sigId = srcSignalId(pos);
    if (sigId === null) continue;
    const sig = sigMap.get(sigId);
    if (!sig) continue;
    // filter by timeframe when selected
    if (tfFilter !== null && sig.timeframe !== tfFilter) continue;

    const exitOrds = exitOrdersFor(pos, orders);
    const pnl = realizedPnl(pos, exitOrds);
    const r = rMultiple(pos, exitOrds);
    const bd = sig.strategy_breakdown as Record<string, Record<string, unknown>>;
    const finalDir = sig.direction;

    for (const [name, info] of Object.entries(bd)) {
      if (name.startsWith("_") || typeof info !== "object" || info === null) continue;
      const dir = info.direction as string;
      if (!dir || dir === "NEUTRAL") continue;
      const contr = asNum(info.contribution as unknown) ?? 0;
      const conf = asNum(info.confidence as unknown) ?? 0;

      const acc = map.get(name) ?? { wins: 0, losses: 0, gp: 0, gl: 0, totalPnl: 0, totalR: 0, rCount: 0, totalContr: 0, contrCount: 0, totalConf: 0, confCount: 0, agreements: 0, disagreements: 0 };
      if (pnl !== null) {
        if (pnl > 0) { acc.wins++; acc.gp += pnl; }
        else { acc.losses++; acc.gl += Math.abs(pnl); }
        acc.totalPnl += pnl;
      }
      if (r !== null) { acc.totalR += r; acc.rCount++; }
      acc.totalContr += contr; acc.contrCount++;
      acc.totalConf += conf; acc.confCount++;
      const agrees = dir === finalDir || (dir.includes("LONG") && finalDir.includes("LONG")) || (dir.includes("SHORT") && finalDir.includes("SHORT"));
      if (agrees) acc.agreements++; else acc.disagreements++;
      map.set(name, acc);
    }
  }

  return Array.from(map.entries()).map(([name, a]) => {
    const total = a.wins + a.losses;
    const tfMap = stratCfg.get(name) ?? new Map<string, Strategy>();
    const tfStates: Record<string, TfState> = {};
    for (const [tf, s] of tfMap.entries()) tfStates[tf] = { enabled: s.enabled, weight: s.weight };
    return {
      name,
      trades: total,
      wins: a.wins,
      losses: a.losses,
      wr: total > 0 ? (a.wins / total) * 100 : 0,
      grossProfit: a.gp,
      grossLoss: a.gl,
      pf: a.gl > 0 ? a.gp / a.gl : a.gp > 0 ? null : null,
      totalPnl: a.totalPnl,
      totalR: a.totalR,
      rCount: a.rCount,
      avgR: a.rCount > 0 ? a.totalR / a.rCount : null,
      avgContribution: a.contrCount > 0 ? a.totalContr / a.contrCount : 0,
      contributionCount: a.contrCount,
      avgConfidence: a.confCount > 0 ? a.totalConf / a.confCount : 0,
      confidenceCount: a.confCount,
      agreements: a.agreements,
      disagreements: a.disagreements,
      agreementRate: a.agreements + a.disagreements > 0 ? (a.agreements / (a.agreements + a.disagreements)) * 100 : null,
      tfStates,
    };
  }).sort((a, b) => b.trades - a.trades);
}

/* ── compute coalition stats ──────────────────────────────────────────────── */
const SCORE_BRACKETS: Pick<ScoreBracket, "label" | "min" | "max">[] = [
  { label: "≤60", min: 0, max: 60 },
  { label: "61–70", min: 61, max: 70 },
  { label: "71–80", min: 71, max: 80 },
  { label: "81–90", min: 81, max: 90 },
  { label: "91+", min: 91, max: 100 },
];

function computeCoalitions(
  signals: Signal[],
  positions: Position[],
  orders: Order[],
  tfFilter: string | null,
): { coalitions: CoalitionStat[]; consensus: ConsensusLevel[]; scoreBrackets: ScoreBracket[] } {
  const sigMap = new Map<number, Signal>();
  for (const s of signals) sigMap.set(s.id, s);

  type CoalAcc = { wins: number; losses: number; totalR: number; rCount: number; totalScore: number; scoreCount: number; strategies: CoalitionEntry[] };
  const coalMap = new Map<string, CoalAcc>();
  const consensusMap = new Map<number, { wins: number; losses: number; totalR: number; rCount: number; totalScore: number }>();
  const bracketAccs: { wins: number; losses: number; totalR: number; rCount: number }[] = SCORE_BRACKETS.map(() => ({ wins: 0, losses: 0, totalR: 0, rCount: 0 }));

  for (const pos of positions) {
    const sigId = srcSignalId(pos);
    if (sigId === null) continue;
    const sig = sigMap.get(sigId);
    if (!sig) continue;
    if (tfFilter !== null && sig.timeframe !== tfFilter) continue;

    const exitOrds = exitOrdersFor(pos, orders);
    const pnl = realizedPnl(pos, exitOrds);
    const r = rMultiple(pos, exitOrds);
    const won = pnl !== null && pnl > 0;
    const lost = pnl !== null && pnl <= 0;

    const bd = sig.strategy_breakdown as Record<string, Record<string, unknown>>;
    const finalDir = sig.direction;

    // Extract strategies that voted in the same direction as the final signal
    const coalition: CoalitionEntry[] = [];
    for (const [name, info] of Object.entries(bd)) {
      if (name.startsWith("_") || typeof info !== "object" || info === null) continue;
      const dir = info.direction as string;
      if (!dir || dir === "NEUTRAL") continue;
      const agrees = dir === finalDir ||
        (dir.includes("LONG") && finalDir.includes("LONG")) ||
        (dir.includes("SHORT") && finalDir.includes("SHORT"));
      if (!agrees) continue;
      coalition.push({ name, weight: asNum(info.weight as unknown) ?? 0 });
    }
    if (coalition.length === 0) continue;

    coalition.sort((a, b) => a.name.localeCompare(b.name));
    const fingerprint = coalition.map(e => e.name).join(" + ");

    // Accumulate coalition
    const ca = coalMap.get(fingerprint) ?? { wins: 0, losses: 0, totalR: 0, rCount: 0, totalScore: 0, scoreCount: 0, strategies: coalition };
    if (won) ca.wins++;
    if (lost) ca.losses++;
    if (r !== null) { ca.totalR += r; ca.rCount++; }
    ca.totalScore += sig.score; ca.scoreCount++;
    coalMap.set(fingerprint, ca);

    // Accumulate consensus level (number of aligned strategies)
    const level = coalition.length;
    const lv = consensusMap.get(level) ?? { wins: 0, losses: 0, totalR: 0, rCount: 0, totalScore: 0 };
    if (won) lv.wins++;
    if (lost) lv.losses++;
    if (r !== null) { lv.totalR += r; lv.rCount++; }
    lv.totalScore += sig.score;
    consensusMap.set(level, lv);

    // Accumulate score bracket
    const bi = SCORE_BRACKETS.findIndex(b => sig.score >= b.min && sig.score <= b.max);
    if (bi >= 0) {
      if (won) bracketAccs[bi].wins++;
      if (lost) bracketAccs[bi].losses++;
      if (r !== null) { bracketAccs[bi].totalR += r; bracketAccs[bi].rCount++; }
    }
  }

  const coalitions: CoalitionStat[] = Array.from(coalMap.entries())
    .map(([fingerprint, a]) => {
      const total = a.wins + a.losses;
      return {
        fingerprint,
        strategies: a.strategies,
        trades: total,
        wins: a.wins,
        losses: a.losses,
        wr: total > 0 ? (a.wins / total) * 100 : 0,
        totalR: a.totalR,
        rCount: a.rCount,
        avgR: a.rCount > 0 ? a.totalR / a.rCount : null,
        avgScore: a.scoreCount > 0 ? a.totalScore / a.scoreCount : 0,
        scoreCount: a.scoreCount,
      };
    })
    .sort((a, b) => b.trades - a.trades);

  const consensus: ConsensusLevel[] = Array.from(consensusMap.entries())
    .map(([count, lv]) => {
      const total = lv.wins + lv.losses;
      return {
        count,
        trades: total,
        wins: lv.wins,
        wr: total > 0 ? (lv.wins / total) * 100 : 0,
        totalR: lv.totalR,
        rCount: lv.rCount,
        avgR: lv.rCount > 0 ? lv.totalR / lv.rCount : null,
        avgScore: total > 0 ? lv.totalScore / total : 0,
      };
    })
    .sort((a, b) => a.count - b.count);

  const scoreBrackets: ScoreBracket[] = SCORE_BRACKETS.map((b, i) => {
    const a = bracketAccs[i];
    const total = a.wins + a.losses;
    return {
      ...b,
      trades: total,
      wins: a.wins,
      wr: total > 0 ? (a.wins / total) * 100 : 0,
      totalR: a.totalR,
      rCount: a.rCount,
      avgR: a.rCount > 0 ? a.totalR / a.rCount : null,
    };
  });

  return { coalitions, consensus, scoreBrackets };
}

/* ── main component ───────────────────────────────────────────────────────── */
export function StrategyAnalysisScreen({ signals, positions, orders, strategies }: Props) {
  const [tab, setTab] = useState<Tab>("ranking");
  const [period, setPeriod] = useState("all");
  const [tfFilter, setTfFilter] = useState<string | null>(null);

  const availableTfs = useMemo(() => {
    const tfs = new Set<string>();
    for (const s of strategies) tfs.add(s.timeframe);
    return sortedTfs(Array.from(tfs));
  }, [strategies]);

  const cutoff = useMemo(() => {
    const now = Date.now();
    if (period === "7d") return now - 7 * 86400000;
    if (period === "30d") return now - 30 * 86400000;
    if (period === "90d") return now - 90 * 86400000;
    return 0;
  }, [period]);

  const filteredPositions = useMemo(() =>
    positions.filter(p => {
      if (p.realized_pnl == null && exitOrdersFor(p, orders).length === 0) return false;
      const t = new Date(p.closed_at ?? p.opened_at).getTime();
      return t >= cutoff;
    }),
    [positions, orders, cutoff]
  );

  const stats = useMemo(
    () => computeStats(signals, filteredPositions, orders, strategies, tfFilter),
    [signals, filteredPositions, orders, strategies, tfFilter]
  );

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Analisi strategie</h1>
          <div className="page-sub">
            {stats.length} indicatori
            {tfFilter ? ` · timeframe ${tfFilter}` : " · tutti i timeframe"}
            {" · "}{filteredPositions.length} posizioni chiuse nel periodo
          </div>
        </div>
        <div className="page-actions">
          <PeriodSeg value={period} onChange={setPeriod} />
        </div>
      </div>

      {/* Timeframe filter bar */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "var(--gap)", padding: "10px 14px", background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", marginRight: 4 }}>Timeframe</span>
        <button
          className={"btn sm " + (tfFilter === null ? "primary" : "")}
          onClick={() => setTfFilter(null)}
        >
          Tutti
        </button>
        {availableTfs.map(tf => (
          <button
            key={tf}
            className={"btn sm " + (tfFilter === tf ? "primary" : "")}
            onClick={() => setTfFilter(tf)}
          >
            {tf}
          </button>
        ))}
        {tfFilter && (
          <span className="muted" style={{ marginLeft: 6, fontSize: "var(--fz-xs)" }}>
            Le statistiche mostrano solo posizioni generate da segnali {tfFilter}
          </span>
        )}
      </div>

      {/* Summary pills */}
      <SummaryPills stats={stats} />

      {/* Tabs */}
      <div style={{ display: "flex", gap: 2, marginBottom: "var(--gap)", borderBottom: "1px solid var(--border)" }}>
        {(["ranking", "segnale", "confronto", "combinazioni", "heatmap"] as Tab[]).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              padding: "8px 18px",
              fontSize: "var(--fz-sm)",
              fontWeight: tab === t ? 600 : 400,
              color: tab === t ? "var(--fg)" : "var(--fg-3)",
              background: "none",
              border: "none",
              borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
              cursor: "pointer",
              marginBottom: -1,
            }}
          >
            {t === "ranking" ? "Ranking" : t === "segnale" ? "Segnale" : t === "confronto" ? "Confronto" : t === "combinazioni" ? "Combinazioni" : "Heatmap"}
          </button>
        ))}
      </div>

      {tab === "ranking" && <RankingTab stats={stats} strategies={strategies} tfFilter={tfFilter} availableTfs={availableTfs} />}
      {tab === "segnale" && <SignaleTab signals={signals} strategies={strategies} tfFilter={tfFilter} />}
      {tab === "confronto" && <ConfrontoTab stats={stats} tfFilter={tfFilter} />}
      {tab === "combinazioni" && <CombinazioniTab signals={signals} positions={filteredPositions} orders={orders} tfFilter={tfFilter} totalSignals={signals.length} />}
      {tab === "heatmap" && <HeatmapTab signals={signals} positions={filteredPositions} orders={orders} strategies={strategies} />}
    </div>
  );
}

/* ── Config grid (specchietto) ────────────────────────────────────────────── */
function WeightChip({ weight }: { weight: number }) {
  const MAX_WEIGHT = 15;
  const pct = Math.min(100, (weight / MAX_WEIGHT) * 100);
  const color = weight >= 13 ? "var(--accent)" : weight >= 9 ? "var(--long)" : weight >= 6 ? "var(--warn)" : "var(--fg-3)";
  return (
    <div style={{ display: "inline-flex", flexDirection: "column", alignItems: "center", gap: 3, minWidth: 40 }}>
      <span className="mono" style={{ fontSize: 13, fontWeight: 700, lineHeight: 1, color }}>{weight}</span>
      <div style={{ width: 36, height: 4, background: "var(--bg-2)", borderRadius: 2, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: color, borderRadius: 2 }} />
      </div>
    </div>
  );
}

function ConfigGrid({ strategies, highlightTf }: { strategies: Strategy[]; highlightTf: string | null }) {
  const tfs = useMemo(() => {
    const s = new Set<string>();
    for (const st of strategies) s.add(st.timeframe);
    return sortedTfs(Array.from(s));
  }, [strategies]);

  const names = useMemo(() => {
    const s = new Set<string>();
    for (const st of strategies) s.add(st.strategy_name);
    return Array.from(s).sort();
  }, [strategies]);

  // name+tf → Strategy
  const idx = useMemo(() => {
    const m = new Map<string, Strategy>();
    for (const s of strategies) m.set(`${s.strategy_name}::${s.timeframe}`, s);
    return m;
  }, [strategies]);

  if (names.length === 0) return null;

  return (
    <div style={{ overflowX: "auto" }}>
      <table className="tbl" style={{ minWidth: 400, fontSize: "var(--fz-xs)" }}>
        <thead>
          <tr>
            <th style={{ minWidth: 160 }}>Strategia</th>
            <th style={{ textAlign: "center", minWidth: 64 }}>Peso</th>
            {tfs.map(tf => (
              <th key={tf} style={{ textAlign: "center", minWidth: 44, fontWeight: highlightTf === tf ? 700 : 400, color: highlightTf === tf ? "var(--accent)" : undefined }}>
                {tf}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map(name => {
            const anyEnabled = tfs.some(tf => idx.get(`${name}::${tf}`)?.enabled);
            // representative weight: first available TF
            const repWeight = tfs.map(tf => idx.get(`${name}::${tf}`)?.weight).find(w => w != null) ?? 0;
            return (
              <tr key={name} style={{ opacity: anyEnabled ? 1 : 0.45 }}>
                <td style={{ fontSize: "var(--fz-sm)" }}>{name}</td>
                <td style={{ textAlign: "center", paddingTop: 6, paddingBottom: 6 }}>
                  <WeightChip weight={repWeight} />
                </td>
                {tfs.map(tf => {
                  const s = idx.get(`${name}::${tf}`);
                  const isHighlight = highlightTf === tf;
                  const hasCustomWeight = s != null && s.weight !== repWeight;
                  if (!s) return (
                    <td key={tf} style={{ textAlign: "center", color: "var(--fg-4)" }}>—</td>
                  );
                  return (
                    <td key={tf} style={{ textAlign: "center", background: isHighlight ? "var(--bg-2)" : undefined }}>
                      {s.enabled ? (
                        <span title={hasCustomWeight ? `peso ${s.weight} (diverso dal default)` : undefined} style={{ color: "var(--long)", cursor: "default" }}>
                          ●{hasCustomWeight && <sup className="mono" style={{ fontSize: 9, color: "var(--warn)", marginLeft: 1 }}>{s.weight}</sup>}
                        </span>
                      ) : (
                        <span title="disabilitata" style={{ color: "var(--fg-4)", cursor: "default" }}>○</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
      <div style={{ marginTop: 8, fontSize: 10, color: "var(--fg-3)" }}>
        Il peso è condiviso tra tutti i timeframe della stessa strategia &nbsp;·&nbsp;
        <span className="mono" style={{ color: "var(--long)" }}>●</span> abilitata &nbsp;·&nbsp;
        <span className="mono" style={{ color: "var(--fg-4)" }}>○</span> disabilitata &nbsp;·&nbsp;
        <span className="mono" style={{ color: "var(--fg-4)" }}>—</span> non configurata
      </div>
    </div>
  );
}

/* ── Summary pills ────────────────────────────────────────────────────────── */
function SummaryPills({ stats }: { stats: StratStat[] }) {
  const enough = stats.filter(s => s.trades >= 5);
  if (enough.length === 0) return null;

  const bestWR = [...enough].sort((a, b) => b.wr - a.wr)[0];
  const bestPnl = [...enough].sort((a, b) => b.totalPnl - a.totalPnl)[0];
  const bestR = [...enough].filter(s => s.avgR !== null).sort((a, b) => (b.avgR ?? 0) - (a.avgR ?? 0))[0];
  const worstPnl = [...enough].sort((a, b) => a.totalPnl - b.totalPnl)[0];
  const worstWR = [...enough].sort((a, b) => a.wr - b.wr)[0];

  const pill = (label: string, name: string, value: string, tone: "long" | "short") => (
    <div style={{ flex: 1, minWidth: 160, padding: "10px 14px", border: "1px solid var(--border)", borderRadius: "var(--radius)", background: "var(--bg-1)" }}>
      <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--fg-3)", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: "var(--fz-sm)", fontWeight: 600, color: tone === "long" ? "var(--long)" : "var(--short)", marginBottom: 2 }}>{name}</div>
      <div className="mono muted" style={{ fontSize: 11 }}>{value}</div>
    </div>
  );

  return (
    <div style={{ display: "flex", gap: 10, marginBottom: "var(--gap)", flexWrap: "wrap" }}>
      {bestWR && pill("Miglior WR", bestWR.name, `${bestWR.wr.toFixed(0)}% (${bestWR.trades}t)`, "long")}
      {bestPnl && pill("Maggior P&L", bestPnl.name, fmt.pnl(bestPnl.totalPnl), "long")}
      {bestR && pill("Miglior R medio", bestR.name, `${(bestR.avgR ?? 0) >= 0 ? "+" : ""}${(bestR.avgR ?? 0).toFixed(2)}R`, "long")}
      {worstWR && worstWR !== bestWR && pill("Peggior WR", worstWR.name, `${worstWR.wr.toFixed(0)}% (${worstWR.trades}t)`, "short")}
      {worstPnl && worstPnl !== bestPnl && pill("Maggior perdita", worstPnl.name, fmt.pnl(worstPnl.totalPnl), "short")}
    </div>
  );
}

/* ── Ranking tab ──────────────────────────────────────────────────────────── */
function RankingTab({ stats, strategies, tfFilter, availableTfs }: {
  stats: StratStat[];
  strategies: Strategy[];
  tfFilter: string | null;
  availableTfs: string[];
}) {
  const [sort, setSort] = useState<SortKey>("trades");
  const [dir, setDir] = useState<SortDir>("desc");
  const [minTrades, setMinTrades] = useState(1);
  const [showConfig, setShowConfig] = useState(true);

  function toggleSort(k: SortKey) {
    if (sort === k) setDir(d => d === "desc" ? "asc" : "desc");
    else { setSort(k); setDir("desc"); }
  }

  const sorted = useMemo(() => {
    const filtered = stats.filter(s => s.trades >= minTrades);
    return [...filtered].sort((a, b) => {
      let va: number, vb: number;
      if (sort === "wr") { va = a.wr; vb = b.wr; }
      else if (sort === "trades") { va = a.trades; vb = b.trades; }
      else if (sort === "avgR") { va = a.avgR ?? -99; vb = b.avgR ?? -99; }
      else if (sort === "pnl") { va = a.totalPnl; vb = b.totalPnl; }
      else if (sort === "pf") { va = a.pf ?? 0; vb = b.pf ?? 0; }
      else { va = a.agreementRate ?? 0; vb = b.agreementRate ?? 0; }
      return dir === "desc" ? vb - va : va - vb;
    });
  }, [stats, sort, dir, minTrades]);

  function health(s: StratStat): "good" | "warn" | "bad" {
    if (s.trades < 3) return "warn";
    if (s.wr >= 55 && (s.avgR === null || s.avgR >= 0)) return "good";
    if (s.wr < 40 && (s.avgR !== null && s.avgR < 0)) return "bad";
    return "warn";
  }

  const Th = ({ k, label }: { k: SortKey; label: string }) => (
    <th style={{ textAlign: "right", cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" }} onClick={() => toggleSort(k)}>
      {label} {sort === k ? (dir === "desc" ? "↓" : "↑") : ""}
    </th>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap)" }}>
      {/* Specchietto config */}
      <div className="card">
        <div
          className="card-head"
          style={{ cursor: "pointer" }}
          onClick={() => setShowConfig(v => !v)}
        >
          <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ display: "inline-flex", transition: "transform 0.15s", transform: showConfig ? "rotate(90deg)" : "none", color: "var(--fg-2)" }}>
              <Icon name="chevron" size={12} />
            </span>
            <span className="title">Configurazione per timeframe</span>
            {!showConfig && (
              <span className="muted mono" style={{ fontSize: "var(--fz-xs)" }}>
                {availableTfs.length} timeframe · {strategies.filter(s => s.enabled).length} configurazioni attive
              </span>
            )}
          </span>
          <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>
            {tfFilter ? `filtro attivo: ${tfFilter}` : "tutti i TF"}
          </div>
        </div>
        {showConfig && (
          <div style={{ padding: "4px 18px 16px" }}>
            <ConfigGrid strategies={strategies} highlightTf={tfFilter} />
          </div>
        )}
      </div>

      {/* Performance table */}
      <div className="card">
        <div className="card-head">
          <span className="title">
            Performance per strategia
            {tfFilter && <span className="badge muted" style={{ marginLeft: 8 }}>TF: {tfFilter}</span>}
          </span>
          <div className="right" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span className="muted" style={{ fontSize: "var(--fz-xs)" }}>Min trade:</span>
            {[1, 3, 5, 10].map(n => (
              <button key={n} className={"btn sm " + (minTrades === n ? "primary" : "")} onClick={() => setMinTrades(n)}>{n}+</button>
            ))}
          </div>
        </div>

        {stats.length === 0 ? (
          <div style={{ padding: 32, textAlign: "center", color: "var(--fg-3)" }}>
            {tfFilter
              ? `Nessun dato per il timeframe ${tfFilter} nel periodo selezionato.`
              : "Nessun dato — servono posizioni chiuse collegate a segnali."}
          </div>
        ) : (
          <>
            <div style={{ overflowX: "auto" }}>
              <table className="tbl" style={{ minWidth: 900 }}>
                <thead>
                  <tr>
                    <th style={{ width: 28 }} />
                    <th style={{ cursor: "pointer" }} onClick={() => toggleSort("trades")}>
                      Strategia {sort === "trades" ? (dir === "desc" ? "↓" : "↑") : ""}
                    </th>
                    <th style={{ textAlign: "center" }}>
                      {tfFilter ? `Stato (${tfFilter})` : "Stato TF"}
                    </th>
                    <Th k="trades" label="Trade" />
                    <th style={{ minWidth: 130 }}>WR</th>
                    <Th k="wr" label="%" />
                    <Th k="avgR" label="Avg R" />
                    <Th k="pf" label="PF" />
                    <th style={{ textAlign: "right" }}>Contr.</th>
                    <th style={{ textAlign: "right" }}>Accordo</th>
                    <Th k="pnl" label="P&L" />
                  </tr>
                </thead>
                <tbody>
                  {sorted.map(s => {
                    const h = health(s);
                    const dot = h === "good" ? "var(--long)" : h === "bad" ? "var(--short)" : "var(--warn)";
                    return (
                      <tr key={s.name}>
                        <td>
                          <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: dot }}
                            title={h === "good" ? "Performante" : h === "bad" ? "Problematica" : "Dati limitati o misti"} />
                        </td>
                        <td>
                          <div style={{ fontWeight: 500, fontSize: "var(--fz-sm)" }}>{s.name}</div>
                          <div className="muted mono" style={{ fontSize: 10 }}>{s.trades}W+L · conf {s.avgConfidence.toFixed(0)}</div>
                        </td>
                        <td style={{ textAlign: "center" }}>
                          <TfStateCell tfStates={s.tfStates} tfFilter={tfFilter} />
                        </td>
                        <td className="num muted">{s.trades}</td>
                        <td style={{ minWidth: 130 }}>
                          <WrBar wr={s.wr} wins={s.wins} losses={s.losses} />
                        </td>
                        <td className="num" style={{ color: s.wr >= 55 ? "var(--long)" : s.wr < 45 ? "var(--short)" : "var(--warn)" }}>
                          {s.wr.toFixed(0)}%
                        </td>
                        <td className="num" style={{ color: s.avgR === null ? "var(--fg-3)" : s.avgR >= 0 ? "var(--long)" : "var(--short)" }}>
                          {s.avgR !== null ? `${s.avgR >= 0 ? "+" : ""}${s.avgR.toFixed(2)}R` : "—"}
                        </td>
                        <td className="num muted">
                          {s.pf !== null ? s.pf.toFixed(2) : s.grossLoss === 0 && s.grossProfit > 0 ? "∞" : "—"}
                        </td>
                        <td className="num muted">{s.avgContribution.toFixed(1)}</td>
                        <td className="num muted">
                          {s.agreementRate !== null ? `${s.agreementRate.toFixed(0)}%` : "—"}
                        </td>
                        <td className={"num " + (s.totalPnl >= 0 ? "up" : "down")}>
                          {fmt.pnl(s.totalPnl)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div style={{ padding: "10px 16px", borderTop: "1px solid var(--border)", fontSize: "var(--fz-xs)", color: "var(--fg-3)" }}>
              <span className="mono"><span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: "var(--long)", marginRight: 5 }} />WR≥55% e avgR≥0</span>
              <span className="mono" style={{ marginLeft: 14 }}><span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: "var(--warn)", marginRight: 5 }} />misto o pochi dati</span>
              <span className="mono" style={{ marginLeft: 14 }}><span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: "var(--short)", marginRight: 5 }} />WR&lt;40% e avgR&lt;0</span>
              <span className="muted" style={{ marginLeft: 20 }}>· Accordo = % volte che la strategia ha votato nella direzione del segnale finale</span>
              <span className="muted" style={{ marginLeft: 20 }}>· Il W/R per indicatore indica quando quell&apos;indicatore era nella coalizione vincente, non che abbia deciso da solo — vedi tab Combinazioni</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ── TF state cell ────────────────────────────────────────────────────────── */
function TfStateCell({ tfStates, tfFilter }: { tfStates: Record<string, TfState>; tfFilter: string | null }) {
  const tfs = Object.keys(tfStates);
  if (tfs.length === 0) return <span className="muted mono" style={{ fontSize: 10 }}>—</span>;

  if (tfFilter !== null) {
    const s = tfStates[tfFilter];
    if (!s) return <span className="muted mono" style={{ fontSize: 10 }}>n/d</span>;
    return (
      <div style={{ display: "inline-flex", gap: 5, alignItems: "center" }}>
        <span className={"badge " + (s.enabled ? "long" : "muted")} style={{ fontSize: 9 }}>
          {s.enabled ? "ON" : "OFF"}
        </span>
        {s.enabled && (
          <span className="mono" style={{
            fontSize: "var(--fz-sm)", fontWeight: 700,
            color: s.weight >= 13 ? "var(--accent)" : s.weight >= 9 ? "var(--fg)" : "var(--fg-2)",
          }}>w{s.weight}</span>
        )}
      </div>
    );
  }

  // "Tutti": show enabled count vs total, list weights on hover
  const enabledCount = tfs.filter(tf => tfStates[tf].enabled).length;
  const total = tfs.length;
  const allOn = enabledCount === total;
  const allOff = enabledCount === 0;
  const title = tfs.sort().map(tf => `${tf}: ${tfStates[tf].enabled ? `ON w${tfStates[tf].weight}` : "OFF"}`).join("\n");

  return (
    <span
      title={title}
      className="mono"
      style={{
        fontSize: 10,
        color: allOn ? "var(--long)" : allOff ? "var(--fg-4)" : "var(--warn)",
        cursor: "default",
      }}
    >
      {enabledCount}/{total} TF
    </span>
  );
}

/* ── Segnale tab ──────────────────────────────────────────────────────────── */
function SignaleTab({ signals, strategies, tfFilter }: { signals: Signal[]; strategies: Strategy[]; tfFilter: string | null }) {
  const stratMap = useMemo(() => {
    const m = new Map<string, Strategy>();
    for (const s of strategies) m.set(s.strategy_name, s);
    return m;
  }, [strategies]);

  const recentSignals = useMemo(() => {
    let s = [...signals];
    if (tfFilter) s = s.filter(sig => sig.timeframe === tfFilter);
    return s.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).slice(0, 100);
  }, [signals, tfFilter]);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const effectiveId = selectedId ?? recentSignals[0]?.id ?? null;
  const selected = useMemo(() => recentSignals.find(s => s.id === effectiveId) ?? null, [recentSignals, effectiveId]);

  if (recentSignals.length === 0) {
    return (
      <div style={{ padding: 32, textAlign: "center", color: "var(--fg-3)" }}>
        {tfFilter ? `Nessun segnale per il timeframe ${tfFilter}.` : "Nessun segnale disponibile."}
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: "var(--gap)", alignItems: "start" }}>
      <div className="card" style={{ maxHeight: 640, overflowY: "auto" }}>
        <div className="card-head">
          <span className="title">Segnali recenti</span>
          {tfFilter && <span className="badge muted" style={{ fontSize: 9 }}>{tfFilter}</span>}
        </div>
        {recentSignals.map(sig => {
          const isLong = sig.direction.includes("LONG");
          const isShort = sig.direction.includes("SHORT");
          const isActive = (selectedId ?? recentSignals[0]?.id) === sig.id;
          return (
            <div
              key={sig.id}
              onClick={() => setSelectedId(sig.id)}
              style={{
                padding: "10px 14px", borderBottom: "1px solid var(--border)", cursor: "pointer",
                background: isActive ? "var(--bg-2)" : "transparent",
                borderLeft: isActive ? "2px solid var(--accent)" : "2px solid transparent",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                <span style={{ fontWeight: 500, fontSize: "var(--fz-sm)" }}>{sig.market?.symbol ?? "—"}</span>
                <span className={"dir mono " + (isLong ? "long" : isShort ? "short" : "neutral")} style={{ fontSize: 10 }}>
                  {isLong ? "↑" : isShort ? "↓" : "—"} {sig.direction.replace("STRONG_", "")}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 2 }}>
                <span className="mono muted" style={{ fontSize: 10 }}>{sig.timeframe} · score {sig.score}</span>
                <span className="muted" style={{ fontSize: 10 }}>
                  {new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(sig.created_at))}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {selected ? (
        <SignalBreakdown signal={selected} stratMap={stratMap} />
      ) : (
        <div className="card" style={{ padding: 40, textAlign: "center", color: "var(--fg-3)" }}>Seleziona un segnale</div>
      )}
    </div>
  );
}

function SignalBreakdown({ signal, stratMap }: { signal: Signal; stratMap: Map<string, Strategy> }) {
  const bd = signal.strategy_breakdown as Record<string, Record<string, unknown>>;
  const score = bd._score as Record<string, unknown> | undefined;
  const regime = bd._regime as Record<string, unknown> | undefined;
  const mtf = bd._mtf_confluence as Record<string, unknown> | undefined;
  const volatility = bd._volatility as Record<string, unknown> | undefined;
  const funding = bd._funding_rate as Record<string, unknown> | undefined;
  const ob = bd._order_book as Record<string, unknown> | undefined;

  type StratEntry = { name: string; direction: string; confidence: number; weight: number; contribution: number; enabled: boolean };
  const entries: StratEntry[] = [];
  let maxContrib = 0;

  for (const [name, info] of Object.entries(bd)) {
    if (name.startsWith("_") || typeof info !== "object" || info === null) continue;
    const dir = (info.direction as string) ?? "NEUTRAL";
    const conf = asNum(info.confidence as unknown) ?? 0;
    const w = asNum(info.weight as unknown) ?? 0;
    const contr = asNum(info.contribution as unknown) ?? 0;
    maxContrib = Math.max(maxContrib, contr);
    entries.push({ name, direction: dir, confidence: conf, weight: w, contribution: contr, enabled: stratMap.get(name)?.enabled ?? false });
  }

  const longs = entries.filter(e => e.direction.includes("LONG")).sort((a, b) => b.contribution - a.contribution);
  const shorts = entries.filter(e => e.direction.includes("SHORT")).sort((a, b) => b.contribution - a.contribution);
  const neutrals = entries.filter(e => e.direction === "NEUTRAL").sort((a, b) => b.weight - a.weight);
  const isLong = signal.direction.includes("LONG");
  const isShort = signal.direction.includes("SHORT");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap)" }}>
      <div className="card">
        <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 12 }}>
          <MetricBox label="Simbolo" value={signal.market?.symbol ?? "—"} />
          <MetricBox label="Timeframe" value={signal.timeframe} />
          <MetricBox label="Direzione" value={signal.direction.replace("STRONG_", "★ ")} tone={isLong ? "long" : isShort ? "short" : "muted"} />
          <MetricBox label="Score" value={String(signal.score)} tone={signal.score >= 75 ? "long" : signal.score >= 55 ? "warn" : "short"} />
          <MetricBox label="R/R" value={signal.risk_reward ?? "—"} />
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <span className="title">Voti delle strategie</span>
          <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>
            Long: {longs.length} · Short: {shorts.length} · Neutral: {neutrals.length}
          </div>
        </div>
        <div style={{ padding: "8px 18px 14px" }}>
          {longs.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--long)", marginBottom: 6 }}>↑ Long ({longs.length})</div>
              {longs.map(e => <VoteRow key={e.name} entry={e} max={maxContrib} />)}
            </div>
          )}
          {shorts.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--short)", marginBottom: 6 }}>↓ Short ({shorts.length})</div>
              {shorts.map(e => <VoteRow key={e.name} entry={e} max={maxContrib} />)}
            </div>
          )}
          {neutrals.length > 0 && (
            <div>
              <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--fg-3)", marginBottom: 6 }}>— Neutral ({neutrals.length})</div>
              {neutrals.map(e => <VoteRow key={e.name} entry={e} max={maxContrib} />)}
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head"><span className="title">Filtri di sistema</span></div>
        <div style={{ padding: "8px 18px 14px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {regime && <FilterPill label="Regime" data={regime} />}
          {mtf && <FilterPill label="MTF Confluence" data={mtf} />}
          {volatility && <FilterPill label="Volatility" data={volatility} />}
          {funding && <FilterPill label="Funding" data={funding} />}
          {ob && <FilterPill label="Order Book" data={{ obi: ob.obi, bid_walls: ob.bid_wall_count, ask_walls: ob.ask_wall_count }} />}
          {score && (
            <FilterPill label="Score finale" data={{
              long: `${asNum(score.long_normalized) ?? "—"}`,
              short: `${asNum(score.short_normalized) ?? "—"}`,
              voting_w: asNum(score.voting_weight),
            }} />
          )}
        </div>
      </div>
    </div>
  );
}

function VoteRow({ entry, max }: { entry: { name: string; direction: string; confidence: number; weight: number; contribution: number; enabled: boolean }; max: number }) {
  const isLong = entry.direction.includes("LONG");
  const isShort = entry.direction.includes("SHORT");
  const barColor = isLong ? "var(--long)" : isShort ? "var(--short)" : "var(--fg-4)";
  const barPct = max > 0 ? (entry.contribution / max) * 100 : entry.confidence;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "160px 1fr 50px 50px", alignItems: "center", gap: 10, padding: "5px 0", borderBottom: "1px solid var(--border)" }}>
      <div>
        <span style={{ fontSize: "var(--fz-sm)", fontWeight: 500 }}>{entry.name}</span>
        {!entry.enabled && <span className="badge muted" style={{ marginLeft: 6, fontSize: 9 }}>OFF</span>}
      </div>
      <div style={{ position: "relative", height: 6, background: "var(--bg-2)", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${Math.min(100, barPct)}%`, background: barColor, borderRadius: 3 }} />
      </div>
      <span className="mono muted" style={{ fontSize: 10, textAlign: "right" }}>c{entry.confidence}</span>
      <span className="mono" style={{
        fontSize: "var(--fz-sm)", fontWeight: 700, textAlign: "right",
        color: entry.weight >= 13 ? "var(--accent)" : entry.weight >= 9 ? "var(--fg)" : "var(--fg-3)",
      }}>w{entry.weight}</span>
    </div>
  );
}

function FilterPill({ label, data }: { label: string; data: Record<string, unknown> }) {
  const entries = Object.entries(data).map(([k, v]) => `${k}: ${typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(3)) : v}`).join(" · ");
  return (
    <div style={{ padding: "8px 10px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", background: "var(--bg-2)" }}>
      <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--fg-3)", marginBottom: 3 }}>{label}</div>
      <div className="mono" style={{ fontSize: 10, color: "var(--fg-2)", wordBreak: "break-word" }}>{entries}</div>
    </div>
  );
}

function MetricBox({ label, value, tone }: { label: string; value: string; tone?: "long" | "short" | "muted" | "warn" }) {
  const color = tone === "long" ? "var(--long)" : tone === "short" ? "var(--short)" : tone === "warn" ? "var(--warn)" : "var(--fg)";
  return (
    <div style={{ padding: "8px 10px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", background: "var(--bg-2)" }}>
      <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--fg-3)", marginBottom: 3 }}>{label}</div>
      <div className="mono" style={{ fontSize: "var(--fz-md)", color, fontWeight: 500 }}>{value}</div>
    </div>
  );
}

/* ── Confronto tab ────────────────────────────────────────────────────────── */
function ConfrontoTab({ stats, tfFilter }: { stats: StratStat[]; tfFilter: string | null }) {
  const [selected, setSelected] = useState<Set<string>>(new Set());

  function toggle(name: string) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else if (next.size < 6) next.add(name);
      return next;
    });
  }

  const compared = stats.filter(s => selected.has(s.name));

  const METRICS: { key: keyof StratStat; label: string; fmt: (v: StratStat) => string; good?: (v: StratStat) => boolean }[] = [
    { key: "trades", label: "Trade", fmt: s => String(s.trades) },
    { key: "wr", label: "Win Rate", fmt: s => `${s.wr.toFixed(1)}%`, good: s => s.wr >= 55 },
    { key: "wins", label: "W / L", fmt: s => `${s.wins} / ${s.losses}` },
    { key: "avgR", label: "Avg R", fmt: s => s.avgR !== null ? `${s.avgR >= 0 ? "+" : ""}${s.avgR.toFixed(2)}R` : "—", good: s => (s.avgR ?? 0) >= 0 },
    { key: "pf", label: "Profit Factor", fmt: s => s.pf !== null ? s.pf.toFixed(2) : s.grossLoss === 0 && s.grossProfit > 0 ? "∞" : "—", good: s => (s.pf ?? 0) >= 1 },
    { key: "totalPnl", label: "P&L totale", fmt: s => fmt.pnl(s.totalPnl), good: s => s.totalPnl >= 0 },
    { key: "avgContribution", label: "Contr. media", fmt: s => s.avgContribution.toFixed(1) },
    { key: "agreementRate", label: "Accordo con segnale", fmt: s => s.agreementRate !== null ? `${s.agreementRate.toFixed(0)}%` : "—" },
  ];

  const bestBy = (key: keyof StratStat): string | null => {
    if (compared.length < 2) return null;
    const metric = METRICS.find(m => m.key === key);
    if (!metric?.good) return null;
    const num = (s: StratStat) => {
      if (key === "wr") return s.wr;
      if (key === "avgR") return s.avgR ?? -99;
      if (key === "pf") return s.pf ?? 0;
      if (key === "totalPnl") return s.totalPnl;
      return 0;
    };
    return [...compared].sort((a, b) => num(b) - num(a))[0]?.name ?? null;
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "220px 1fr", gap: "var(--gap)", alignItems: "start" }}>
      <div className="card">
        <div className="card-head">
          <span className="title">Seleziona strategie</span>
          <span className="badge muted">{selected.size}/6</span>
        </div>
        {tfFilter && (
          <div style={{ padding: "6px 14px", borderBottom: "1px solid var(--border)", fontSize: "var(--fz-xs)", color: "var(--fg-3)" }}>
            Stats filtrate: TF {tfFilter}
          </div>
        )}
        <div style={{ padding: "4px 0 10px" }}>
          {stats.map(s => {
            const tfState = tfFilter ? s.tfStates[tfFilter] : null;
            return (
              <div
                key={s.name}
                onClick={() => toggle(s.name)}
                style={{
                  display: "flex", alignItems: "center", gap: 10, padding: "8px 14px",
                  cursor: "pointer", borderBottom: "1px solid var(--border)",
                  background: selected.has(s.name) ? "var(--bg-2)" : "transparent",
                }}
              >
                <div style={{
                  width: 14, height: 14, border: "1px solid var(--border)", borderRadius: 3, flexShrink: 0,
                  background: selected.has(s.name) ? "var(--accent)" : "transparent",
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}>
                  {selected.has(s.name) && <Icon name="check" size={10} />}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: "var(--fz-sm)", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.name}</div>
                  <div className="muted mono" style={{ fontSize: 10 }}>
                    WR {s.wr.toFixed(0)}% · {s.trades}t
                    {tfState && <span style={{ marginLeft: 4, color: tfState.enabled ? "var(--long)" : "var(--fg-4)" }}>
                      {" "}· {tfState.enabled ? `ON w${tfState.weight}` : "OFF"}
                    </span>}
                  </div>
                </div>
              </div>
            );
          })}
          {stats.length === 0 && <div className="muted" style={{ padding: "14px 14px", fontSize: "var(--fz-sm)" }}>Nessun dato disponibile</div>}
        </div>
      </div>

      {compared.length === 0 ? (
        <div className="card" style={{ padding: 40, textAlign: "center", color: "var(--fg-3)" }}>
          Seleziona 2+ strategie dalla lista per confrontarle
        </div>
      ) : (
        <div className="card" style={{ overflowX: "auto" }}>
          <div className="card-head">
            <span className="title">Confronto</span>
            {tfFilter && <span className="badge muted" style={{ marginLeft: 8, fontSize: 9 }}>TF: {tfFilter}</span>}
          </div>
          <table className="tbl" style={{ minWidth: 400 }}>
            <thead>
              <tr>
                <th>Metrica</th>
                {compared.map(s => {
                  const tfState = tfFilter ? s.tfStates[tfFilter] : null;
                  return (
                    <th key={s.name} style={{ textAlign: "right" }}>
                      <div>{s.name}</div>
                      {tfState ? (
                        <div style={{ fontWeight: 400, color: tfState.enabled ? "var(--long)" : "var(--fg-3)", fontSize: 10 }}>
                          {tfFilter}: {tfState.enabled ? `ON w${tfState.weight}` : "OFF"}
                        </div>
                      ) : (
                        <div style={{ fontWeight: 400, color: "var(--fg-3)", fontSize: 10 }}>tutti i TF</div>
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {METRICS.map(m => {
                const best = bestBy(m.key);
                return (
                  <tr key={String(m.key)}>
                    <td className="muted" style={{ fontSize: "var(--fz-sm)" }}>{m.label}</td>
                    {compared.map(s => {
                      const isBest = best === s.name;
                      const good = m.good ? m.good(s) : null;
                      const color = isBest ? "var(--long)" : good === false ? "var(--short)" : "var(--fg)";
                      return (
                        <td key={s.name} className="num mono" style={{ fontSize: "var(--fz-sm)", color }}>
                          {m.fmt(s)}
                          {isBest && <span style={{ marginLeft: 4, fontSize: 9, color: "var(--long)" }}>★</span>}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ── Combinazioni tab ─────────────────────────────────────────────────────── */
function CombinazioniTab({
  signals, positions, orders, tfFilter, totalSignals,
}: {
  signals: Signal[];
  positions: Position[];
  orders: Order[];
  tfFilter: string | null;
  totalSignals: number;
}) {
  const [minTrades, setMinTrades] = useState(2);
  const [expandedFp, setExpandedFp] = useState<string | null>(null);

  const { coalitions, consensus, scoreBrackets } = useMemo(
    () => computeCoalitions(signals, positions, orders, tfFilter),
    [signals, positions, orders, tfFilter],
  );

  const visibleCoalitions = coalitions.filter(c => c.trades >= minTrades);

  const totalTrades = consensus.reduce((s, l) => s + l.trades, 0);
  if (totalTrades === 0) {
    return (
      <div style={{ padding: 40, textAlign: "center", color: "var(--fg-3)" }}>
        {tfFilter
          ? `Nessuna posizione chiusa con segnale ${tfFilter} disponibile.`
          : "Nessun dato — servono posizioni chiuse collegate a segnali con breakdown."}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap)" }}>
      <div style={{ fontSize: "var(--fz-xs)", color: "var(--fg-3)" }}>
        Analisi su {totalSignals} segnali (max 250 più recenti) · {totalTrades} posizioni chiuse con breakdown
        {tfFilter && ` · filtro TF: ${tfFilter}`}
      </div>

      {/* Livello di consenso */}
      <div className="card">
        <div className="card-head">
          <span className="title">Livello di consenso</span>
          <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>
            n. strategie allineate alla direzione finale → W/R
          </div>
        </div>
        <div style={{ padding: "4px 18px 16px" }}>
          {consensus.map(lv => {
            const label = lv.count >= 6 ? "≥6" : String(lv.count);
            const wrColor = lv.wr >= 55 ? "var(--long)" : lv.wr >= 45 ? "var(--warn)" : "var(--short)";
            return (
              <div key={lv.count} style={{ display: "grid", gridTemplateColumns: "52px 60px 1fr 80px 80px 80px", alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
                <span className="mono" style={{ fontSize: "var(--fz-sm)", fontWeight: 600 }}>{label} strat.</span>
                <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{lv.trades}t</span>
                <div style={{ position: "relative", height: 6, background: "var(--bg-2)", borderRadius: 3, overflow: "hidden" }}>
                  <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${lv.wr}%`, background: wrColor, borderRadius: 3 }} />
                </div>
                <span className="mono" style={{ fontSize: "var(--fz-sm)", color: wrColor, textAlign: "right" }}>{lv.wr.toFixed(0)}%</span>
                <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
                  {lv.avgR !== null ? `${lv.avgR >= 0 ? "+" : ""}${lv.avgR.toFixed(2)}R` : "—"}
                </span>
                <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
                  score {lv.avgScore.toFixed(0)}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Coalizioni più frequenti */}
      <div className="card">
        <div className="card-head">
          <span className="title">Coalizioni più frequenti</span>
          <div className="right" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span className="muted" style={{ fontSize: "var(--fz-xs)" }}>Min trade:</span>
            {[1, 2, 3, 5].map(n => (
              <button key={n} className={"btn sm " + (minTrades === n ? "primary" : "")} onClick={() => setMinTrades(n)}>{n}+</button>
            ))}
          </div>
        </div>
        {visibleCoalitions.length === 0 ? (
          <div style={{ padding: 24, textAlign: "center", color: "var(--fg-3)", fontSize: "var(--fz-sm)" }}>
            Nessuna coalizione con ≥{minTrades} trade. Prova un filtro più basso.
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="tbl" style={{ minWidth: 780 }}>
              <thead>
                <tr>
                  <th style={{ width: 28 }} />
                  <th>Coalizione (strategie allineate)</th>
                  <th style={{ textAlign: "right" }}>Trade</th>
                  <th style={{ minWidth: 120 }}>W/R</th>
                  <th style={{ textAlign: "right" }}>%</th>
                  <th style={{ textAlign: "right" }}>Avg R</th>
                  <th style={{ textAlign: "right" }}>Avg score</th>
                </tr>
              </thead>
              <tbody>
                {visibleCoalitions.map(c => {
                  const isExpanded = expandedFp === c.fingerprint;
                  const wrColor = c.wr >= 55 ? "var(--long)" : c.wr < 45 ? "var(--short)" : "var(--warn)";
                  const isSingle = c.strategies.length === 1;
                  return (
                    <React.Fragment key={c.fingerprint}>
                      <tr
                        style={{ cursor: "pointer", opacity: isSingle ? 0.65 : 1 }}
                        onClick={() => setExpandedFp(isExpanded ? null : c.fingerprint)}
                      >
                        <td>
                          <span className="muted" style={{ display: "inline-flex", transform: isExpanded ? "rotate(90deg)" : "none" }}>
                            <Icon name="chevron" size={12} />
                          </span>
                        </td>
                        <td>
                          <div style={{ fontSize: "var(--fz-sm)", fontWeight: 500 }}>
                            {c.strategies.map(e => e.name).join(" + ")}
                          </div>
                          <div className="muted mono" style={{ fontSize: 10 }}>
                            {c.strategies.length} strategie · pesi dell&apos;epoca:{" "}
                            {c.strategies.map(e => `${e.name.split("_")[0]} w${e.weight}`).join(", ")}
                          </div>
                        </td>
                        <td className="num muted">{c.trades}</td>
                        <td>
                          <div style={{ position: "relative", height: 6, background: "var(--bg-2)", borderRadius: 3, overflow: "hidden" }}>
                            <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${c.wr}%`, background: wrColor, borderRadius: 3 }} />
                          </div>
                        </td>
                        <td className="num" style={{ color: wrColor }}>{c.wr.toFixed(0)}%</td>
                        <td className="num muted">
                          {c.avgR !== null ? `${c.avgR >= 0 ? "+" : ""}${c.avgR.toFixed(2)}R` : "—"}
                        </td>
                        <td className="num muted">{c.avgScore.toFixed(0)}</td>
                      </tr>
                      {isExpanded && (
                        <tr>
                          <td colSpan={7} style={{ background: "var(--bg)", padding: "10px 18px" }}>
                            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                              {c.strategies.map(e => {
                                const wColor = e.weight >= 13 ? "var(--accent)" : e.weight >= 9 ? "var(--long)" : "var(--warn)";
                                return (
                                  <div key={e.name} style={{ padding: "6px 10px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", background: "var(--bg-2)" }}>
                                    <div style={{ fontSize: "var(--fz-sm)", fontWeight: 500 }}>{e.name}</div>
                                    <div className="mono" style={{ fontSize: 11, color: wColor, fontWeight: 700 }}>w{e.weight}</div>
                                  </div>
                                );
                              })}
                              <div style={{ padding: "6px 10px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", background: "var(--bg-2)", marginLeft: "auto" }}>
                                <div className="muted" style={{ fontSize: 10, marginBottom: 2 }}>Risultato</div>
                                <div className="mono" style={{ fontSize: "var(--fz-sm)" }}>{c.wins}W / {c.losses}L</div>
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div style={{ padding: "8px 16px", borderTop: "1px solid var(--border)", fontSize: "var(--fz-xs)", color: "var(--fg-3)" }}>
          I pesi mostrati sono quelli usati al momento della generazione del segnale, non i valori attuali del DB.
          Le coalizioni a singola strategia appaiono in grigio (statistica poco significativa).
        </div>
      </div>

      {/* Performance per fascia di score */}
      <div className="card">
        <div className="card-head">
          <span className="title">Performance per fascia di score</span>
          <div className="right muted mono" style={{ fontSize: "var(--fz-xs)" }}>
            lo score del segnale predice le performance?
          </div>
        </div>
        <div style={{ padding: "4px 18px 16px" }}>
          {scoreBrackets.filter(b => b.trades > 0).map(b => {
            const wrColor = b.wr >= 55 ? "var(--long)" : b.wr >= 45 ? "var(--warn)" : "var(--short)";
            return (
              <div key={b.label} style={{ display: "grid", gridTemplateColumns: "64px 60px 1fr 80px 80px", alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
                <span className="mono" style={{ fontSize: "var(--fz-sm)", fontWeight: 600 }}>score {b.label}</span>
                <span className="mono muted" style={{ fontSize: "var(--fz-sm)" }}>{b.trades}t</span>
                <div style={{ position: "relative", height: 6, background: "var(--bg-2)", borderRadius: 3, overflow: "hidden" }}>
                  <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${b.wr}%`, background: wrColor, borderRadius: 3 }} />
                </div>
                <span className="mono" style={{ fontSize: "var(--fz-sm)", color: wrColor, textAlign: "right" }}>{b.wr.toFixed(0)}%</span>
                <span className="mono muted" style={{ fontSize: "var(--fz-sm)", textAlign: "right" }}>
                  {b.avgR !== null ? `${b.avgR >= 0 ? "+" : ""}${b.avgR.toFixed(2)}R` : "—"}
                </span>
              </div>
            );
          })}
          {scoreBrackets.every(b => b.trades === 0) && (
            <div className="muted mono" style={{ padding: "18px 0", fontSize: "var(--fz-sm)" }}>Nessun dato disponibile.</div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Heatmap tab ──────────────────────────────────────────────────────────── */
type HeatCell = { trades: number; wins: number; wr: number; avgR: number | null };

function computeHeatmap(
  signals: Signal[],
  positions: Position[],
  orders: Order[],
): Map<string, Map<string, HeatCell>> {
  const sigMap = new Map<number, Signal>();
  for (const s of signals) sigMap.set(s.id, s);

  const acc = new Map<string, Map<string, { w: number; l: number; r: number; rc: number }>>();

  for (const pos of positions) {
    const sigId = srcSignalId(pos);
    if (sigId === null) continue;
    const sig = sigMap.get(sigId);
    if (!sig) continue;
    const exitOrds = exitOrdersFor(pos, orders);
    const pnl = realizedPnl(pos, exitOrds);
    const r = rMultiple(pos, exitOrds);
    const bd = sig.strategy_breakdown as Record<string, Record<string, unknown>>;
    const finalDir = sig.direction;
    const tf = sig.timeframe;

    for (const [name, info] of Object.entries(bd)) {
      if (name.startsWith("_") || typeof info !== "object" || info === null) continue;
      const dir = info.direction as string;
      if (!dir || dir === "NEUTRAL") continue;
      const agrees =
        dir === finalDir ||
        (dir.includes("LONG") && finalDir.includes("LONG")) ||
        (dir.includes("SHORT") && finalDir.includes("SHORT"));
      if (!agrees) continue;

      if (!acc.has(name)) acc.set(name, new Map());
      const tfMap = acc.get(name)!;
      if (!tfMap.has(tf)) tfMap.set(tf, { w: 0, l: 0, r: 0, rc: 0 });
      const cell = tfMap.get(tf)!;
      if (pnl !== null) { if (pnl > 0) cell.w++; else cell.l++; }
      if (r !== null) { cell.r += r; cell.rc++; }
    }
  }

  const result = new Map<string, Map<string, HeatCell>>();
  for (const [name, tfMap] of acc.entries()) {
    const out = new Map<string, HeatCell>();
    for (const [tf, a] of tfMap.entries()) {
      const total = a.w + a.l;
      out.set(tf, { trades: total, wins: a.w, wr: total > 0 ? (a.w / total) * 100 : 0, avgR: a.rc > 0 ? a.r / a.rc : null });
    }
    result.set(name, out);
  }
  return result;
}

function cellStyle(cell: HeatCell | undefined): { background: string; color: string } {
  if (!cell || cell.trades === 0) return { background: "transparent", color: "var(--fg-4)" };
  if (cell.wr >= 60) return { background: "rgba(var(--success-rgb,34,197,94),0.18)", color: "var(--success,#22c55e)" };
  if (cell.wr >= 55) return { background: "rgba(var(--success-rgb,34,197,94),0.10)", color: "var(--success,#22c55e)" };
  if (cell.wr >= 45) return { background: "rgba(var(--warn-rgb,234,179,8),0.12)", color: "var(--warn,#eab308)" };
  return { background: "rgba(var(--danger-rgb,239,68,68),0.12)", color: "var(--danger,#ef4444)" };
}

type HeatMetric = "wr" | "avgR" | "trades";

function HeatmapTab({
  signals, positions, orders, strategies,
}: {
  signals: Signal[];
  positions: Position[];
  orders: Order[];
  strategies: Strategy[];
}) {
  const [metric, setMetric] = useState<HeatMetric>("wr");
  const [selectedCell, setSelectedCell] = useState<{ strat: string; tf: string } | null>(null);

  const { tfs, stratNames, matrix } = useMemo(() => {
    const hm = computeHeatmap(signals, positions, orders);
    const tfSet = new Set<string>();
    for (const s of strategies) tfSet.add(s.timeframe);
    for (const tfMap of hm.values()) for (const tf of tfMap.keys()) tfSet.add(tf);
    const sortedTfList = sortedTfs(Array.from(tfSet));
    const names = Array.from(hm.keys()).sort();
    return { tfs: sortedTfList, stratNames: names, matrix: hm };
  }, [signals, positions, orders, strategies]);

  const totalTrades = useMemo(() => {
    let n = 0;
    for (const tfMap of matrix.values()) for (const cell of tfMap.values()) n += cell.trades;
    return n;
  }, [matrix]);

  if (stratNames.length === 0 || totalTrades === 0) {
    return (
      <div style={{ padding: 40, textAlign: "center", color: "var(--fg-3)" }}>
        Nessun dato — servono posizioni chiuse collegate a segnali con breakdown strategia.
      </div>
    );
  }

  const selCell = selectedCell ? matrix.get(selectedCell.strat)?.get(selectedCell.tf) : null;

  function cellLabel(cell: HeatCell | undefined): string {
    if (!cell || cell.trades === 0) return "—";
    if (metric === "wr") return `${cell.wr.toFixed(0)}%`;
    if (metric === "avgR") return cell.avgR !== null ? `${cell.avgR >= 0 ? "+" : ""}${cell.avgR.toFixed(1)}R` : "—";
    return String(cell.trades);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap)" }}>
      {/* Controls */}
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em" }}>Metrica:</span>
        {(["wr", "avgR", "trades"] as HeatMetric[]).map(m => (
          <button key={m} className={"btn sm " + (metric === m ? "primary" : "")} onClick={() => setMetric(m)}>
            {m === "wr" ? "Win Rate" : m === "avgR" ? "Avg R" : "Trade"}
          </button>
        ))}
        <span className="muted" style={{ fontSize: 10, marginLeft: 16 }}>
          {stratNames.length} strategie · {tfs.length} timeframe · {totalTrades} trade totali
        </span>
      </div>

      {/* Heatmap grid */}
      <div className="card" style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--fz-xs)" }}>
          <thead>
            <tr>
              <th style={{ padding: "8px 14px", textAlign: "left", borderBottom: "1px solid var(--border)", fontWeight: 500, color: "var(--fg-3)", minWidth: 160 }}>
                Strategia
              </th>
              {tfs.map(tf => (
                <th key={tf} style={{ padding: "8px 8px", textAlign: "center", borderBottom: "1px solid var(--border)", fontWeight: 500, color: "var(--fg-2)", minWidth: 60 }}>
                  {tf}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {stratNames.map(name => {
              const tfMap = matrix.get(name);
              return (
                <tr key={name} style={{ borderBottom: "1px solid var(--border)" }}>
                  <td style={{ padding: "6px 14px", fontWeight: 500, fontSize: "var(--fz-sm)", color: "var(--fg-2)", whiteSpace: "nowrap" }}>
                    {name}
                  </td>
                  {tfs.map(tf => {
                    const cell = tfMap?.get(tf);
                    const { background, color } = cellStyle(cell);
                    const isSelected = selectedCell?.strat === name && selectedCell?.tf === tf;
                    return (
                      <td
                        key={tf}
                        style={{
                          padding: "6px 4px",
                          textAlign: "center",
                          cursor: cell && cell.trades > 0 ? "pointer" : "default",
                          background: isSelected ? "var(--bg-3,var(--bg-2))" : background,
                          outline: isSelected ? "2px solid var(--accent)" : "none",
                          outlineOffset: -2,
                        }}
                        onClick={() => {
                          if (!cell || cell.trades === 0) return;
                          setSelectedCell(isSelected ? null : { strat: name, tf });
                        }}
                        title={cell && cell.trades > 0 ? `${name} · ${tf} · ${cell.wins}W/${cell.trades - cell.wins}L · ${cell.wr.toFixed(0)}% WR` : undefined}
                      >
                        <span className="mono" style={{ fontSize: 11, fontWeight: 600, color }}>
                          {cellLabel(cell)}
                        </span>
                        {cell && cell.trades > 0 && (
                          <div style={{ fontSize: 9, color: "var(--fg-4)", marginTop: 1 }}>
                            {cell.trades}t
                          </div>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Detail panel for selected cell */}
      {selectedCell && selCell && selCell.trades > 0 && (
        <div className="card" style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 12, padding: "14px 18px" }}>
          <div>
            <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--fg-3)", marginBottom: 3 }}>Strategia</div>
            <div className="mono" style={{ fontSize: "var(--fz-sm)", fontWeight: 600 }}>{selectedCell.strat}</div>
          </div>
          <div>
            <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--fg-3)", marginBottom: 3 }}>Timeframe</div>
            <div className="mono" style={{ fontSize: "var(--fz-sm)", fontWeight: 600 }}>{selectedCell.tf}</div>
          </div>
          <div>
            <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--fg-3)", marginBottom: 3 }}>Trade</div>
            <div className="mono" style={{ fontSize: "var(--fz-sm)" }}>{selCell.wins}W / {selCell.trades - selCell.wins}L</div>
          </div>
          <div>
            <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--fg-3)", marginBottom: 3 }}>Win Rate</div>
            <div className="mono" style={{ fontSize: "var(--fz-sm)", color: selCell.wr >= 55 ? "var(--long)" : selCell.wr < 45 ? "var(--short)" : "var(--warn)" }}>
              {selCell.wr.toFixed(1)}%
            </div>
          </div>
          <div>
            <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--fg-3)", marginBottom: 3 }}>Avg R</div>
            <div className="mono" style={{ fontSize: "var(--fz-sm)", color: (selCell.avgR ?? 0) >= 0 ? "var(--long)" : "var(--short)" }}>
              {selCell.avgR !== null ? `${selCell.avgR >= 0 ? "+" : ""}${selCell.avgR.toFixed(2)}R` : "—"}
            </div>
          </div>
        </div>
      )}

      {/* Legend */}
      <div style={{ fontSize: 10, color: "var(--fg-3)", display: "flex", gap: 16, flexWrap: "wrap" }}>
        <span>
          <span style={{ display: "inline-block", width: 10, height: 10, background: "rgba(34,197,94,0.18)", borderRadius: 2, marginRight: 4 }} />
          WR ≥ 60%
        </span>
        <span>
          <span style={{ display: "inline-block", width: 10, height: 10, background: "rgba(34,197,94,0.10)", borderRadius: 2, marginRight: 4 }} />
          WR 55–60%
        </span>
        <span>
          <span style={{ display: "inline-block", width: 10, height: 10, background: "rgba(234,179,8,0.12)", borderRadius: 2, marginRight: 4 }} />
          WR 45–55%
        </span>
        <span>
          <span style={{ display: "inline-block", width: 10, height: 10, background: "rgba(239,68,68,0.12)", borderRadius: 2, marginRight: 4 }} />
          WR &lt; 45%
        </span>
        <span style={{ marginLeft: 8 }}>Click su una cella per il dettaglio · le celle vuote (—) non hanno trade nel periodo</span>
      </div>
    </div>
  );
}

/* ── shared widgets ───────────────────────────────────────────────────────── */
function WrBar({ wr, wins, losses }: { wr: number; wins: number; losses: number }) {
  const barColor = wr >= 55 ? "var(--long)" : wr >= 45 ? "var(--warn)" : "var(--short)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ flex: 1, position: "relative", height: 6, background: "var(--bg-2)", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${wr}%`, background: barColor, borderRadius: 3 }} />
      </div>
      <span className="mono muted" style={{ fontSize: 10, minWidth: 40 }}>{wins}W {losses}L</span>
    </div>
  );
}

function PeriodSeg({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="seg">
      {[["7d", "7d"], ["30d", "30d"], ["90d", "90d"], ["all", "All"]].map(([v, label]) => (
        <button key={v} className={"seg-opt " + (value === v ? "active" : "")} onClick={() => onChange(v)}>{label}</button>
      ))}
    </div>
  );
}

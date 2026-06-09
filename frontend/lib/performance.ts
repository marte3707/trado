import type { Position } from "@/lib/api";

/** Numero di partenza per le serie equity quando non c'è una base nota. */
export const DEFAULT_BASE_EQUITY = 10_000;

export function asNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Posizione chiusa normalizzata: timestamp di chiusura + PnL realizzato. */
interface ClosedTrade {
  time: number;
  pnl: number;
}

/** Estrae i trade chiusi ordinati per `closed_at` ascendente. */
function closedTrades(positions: Position[]): ClosedTrade[] {
  return positions
    .filter((p) => p.status.toLowerCase() === "closed" && p.closed_at)
    .map((p) => ({
      time: new Date(p.closed_at!).getTime(),
      pnl: asNumber(p.realized_pnl) ?? 0,
    }))
    .filter((p) => Number.isFinite(p.time))
    .sort((a, b) => a.time - b.time);
}

const dayKeyFmt = new Intl.DateTimeFormat("it-IT", {
  day: "2-digit",
  month: "2-digit",
});
const monthKeyFmt = new Intl.DateTimeFormat("it-IT", {
  month: "short",
  year: "2-digit",
});

/** Chiave giorno stabile (YYYY-MM-DD) usata per il raggruppamento. */
function isoDay(time: number): string {
  return new Date(time).toISOString().slice(0, 10);
}

/** Chiave mese stabile (YYYY-MM) usata per il raggruppamento. */
function isoMonth(time: number): string {
  return new Date(time).toISOString().slice(0, 7);
}

// ── Equity curve ──────────────────────────────────────────────────────────

export interface EquityPoint {
  time: number;
  label: string;
  equity: number;
}

/** Equity cumulata trade-per-trade a partire da `baseEquity`. */
export function deriveEquitySeries(
  positions: Position[],
  baseEquity = DEFAULT_BASE_EQUITY,
): EquityPoint[] {
  const closed = closedTrades(positions);
  if (closed.length === 0) return [];
  let cum = baseEquity;
  return closed.map((p) => {
    cum += p.pnl;
    return {
      time: p.time,
      label: dayKeyFmt.format(new Date(p.time)),
      equity: Math.round(cum * 100) / 100,
    };
  });
}

// ── Drawdown giornaliero ────────────────────────────────────────────────────

export interface DrawdownPoint {
  day: string;
  drawdownPct: number;
}

/**
 * Drawdown percentuale per giorno di trading: costruisce l'equity di fine
 * giornata, traccia il picco running e calcola (peak − equity) / peak · 100.
 */
export function deriveDailyDrawdown(
  positions: Position[],
  baseEquity = DEFAULT_BASE_EQUITY,
): DrawdownPoint[] {
  const closed = closedTrades(positions);
  if (closed.length === 0) return [];

  // PnL aggregato per giorno, mantenendo l'ordine cronologico.
  const pnlByDay = new Map<string, { time: number; pnl: number }>();
  for (const t of closed) {
    const key = isoDay(t.time);
    const entry = pnlByDay.get(key);
    if (entry) entry.pnl += t.pnl;
    else pnlByDay.set(key, { time: t.time, pnl: t.pnl });
  }

  const days = [...pnlByDay.entries()].sort((a, b) => a[1].time - b[1].time);
  let equity = baseEquity;
  let peak = baseEquity;
  return days.map(([, { time, pnl }]) => {
    equity += pnl;
    if (equity > peak) peak = equity;
    const drawdownPct = peak > 0 ? ((peak - equity) / peak) * 100 : 0;
    return {
      day: dayKeyFmt.format(new Date(time)),
      drawdownPct: Math.round(drawdownPct * 100) / 100,
    };
  });
}

// ── Distribuzione P&L ───────────────────────────────────────────────────────

export interface PnlBucket {
  bucketLabel: string;
  center: number;
  count: number;
  /** P&L medio dei trade caduti in questo bucket (0 se vuoto). */
  avgPnl: number;
  sign: 1 | -1;
}

/**
 * Istogramma dei PnL realizzati per trade: binning in bucket di dollari
 * (ampiezza auto-calcolata da min/max), conteggio per bucket.
 */
export function derivePnlDistribution(
  positions: Position[],
  binCount = 13,
): PnlBucket[] {
  const pnls = closedTrades(positions).map((t) => t.pnl);
  if (pnls.length === 0) return [];

  const min = Math.min(...pnls);
  const max = Math.max(...pnls);
  if (min === max) {
    return [
      {
        bucketLabel: Math.round(min).toString(),
        center: min,
        count: pnls.length,
        avgPnl: min,
        sign: min < 0 ? -1 : 1,
      },
    ];
  }

  // Bin "carini": arrotonda l'ampiezza grezza a 1/2/5 · 10^k.
  const rawSize = (max - min) / binCount;
  const mag = Math.pow(10, Math.floor(Math.log10(rawSize)));
  const norm = rawSize / mag;
  const niceSize = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;

  const start = Math.floor(min / niceSize) * niceSize;
  const buckets: PnlBucket[] = [];
  const sums: number[] = [];
  for (let edge = start; edge <= max; edge += niceSize) {
    const center = edge + niceSize / 2;
    buckets.push({
      bucketLabel: Math.round(edge).toString(),
      center,
      count: 0,
      avgPnl: 0,
      sign: center < 0 ? -1 : 1,
    });
    sums.push(0);
  }
  for (const pnl of pnls) {
    let idx = Math.floor((pnl - start) / niceSize);
    if (idx < 0) idx = 0;
    if (idx >= buckets.length) idx = buckets.length - 1;
    buckets[idx].count += 1;
    sums[idx] += pnl;
  }
  for (let i = 0; i < buckets.length; i += 1) {
    buckets[i].avgPnl = buckets[i].count > 0 ? sums[i] / buckets[i].count : 0;
  }
  return buckets;
}

// ── Trade per giorno ────────────────────────────────────────────────────────

export interface TradesPerDayPoint {
  day: string;
  count: number;
}

/** Numero di trade chiusi per giorno. */
export function deriveTradesPerDay(positions: Position[]): TradesPerDayPoint[] {
  const closed = closedTrades(positions);
  if (closed.length === 0) return [];

  const byDay = new Map<string, { time: number; count: number }>();
  for (const t of closed) {
    const key = isoDay(t.time);
    const entry = byDay.get(key);
    if (entry) entry.count += 1;
    else byDay.set(key, { time: t.time, count: 1 });
  }
  return [...byDay.values()]
    .sort((a, b) => a.time - b.time)
    .map(({ time, count }) => ({
      day: dayKeyFmt.format(new Date(time)),
      count,
    }));
}

// ── Rendimento mensile ──────────────────────────────────────────────────────

export interface MonthlyReturnPoint {
  month: string;
  pnl: number;
  returnPct: number;
}

/**
 * Rendimento percentuale per mese: PnL del mese / equity a inizio mese · 100,
 * dove l'equity a inizio mese parte da `baseEquity` e accumula i PnL passati.
 */
export function deriveMonthlyReturns(
  positions: Position[],
  baseEquity = DEFAULT_BASE_EQUITY,
): MonthlyReturnPoint[] {
  const closed = closedTrades(positions);
  if (closed.length === 0) return [];

  const byMonth = new Map<string, { time: number; pnl: number }>();
  for (const t of closed) {
    const key = isoMonth(t.time);
    const entry = byMonth.get(key);
    if (entry) entry.pnl += t.pnl;
    else byMonth.set(key, { time: t.time, pnl: t.pnl });
  }

  const months = [...byMonth.values()].sort((a, b) => a.time - b.time);
  let equityStart = baseEquity;
  return months.map(({ time, pnl }) => {
    const returnPct = equityStart > 0 ? (pnl / equityStart) * 100 : 0;
    equityStart += pnl;
    return {
      month: monthKeyFmt.format(new Date(time)),
      pnl: Math.round(pnl * 100) / 100,
      returnPct: Math.round(returnPct * 100) / 100,
    };
  });
}

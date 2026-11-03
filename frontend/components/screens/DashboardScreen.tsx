"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, BarChart3, DollarSign, TrendingUp } from "lucide-react";

import {
  AppSettings,
  DecisionLog,
  Health,
  MarketIntelligence,
  Order,
  Position,
  Signal,
  Strategy,
} from "@/lib/api";
import { fmt } from "@/components/atoms";
import { KpiCard } from "@/components/widgets/KpiCard";
import { EquityChart } from "@/components/widgets/EquityChart";
import { SignalFunnel } from "@/components/widgets/SignalFunnel";
import { TopSignalsList } from "@/components/widgets/TopSignalsList";
import { OpenPositionsLive } from "@/components/widgets/OpenPositionsLive";
import { MarketIntelligenceStrip } from "@/components/widgets/MarketIntelligenceStrip";
import { MacroCorrelationCard } from "@/components/widgets/MacroCorrelationCard";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  useAccountMonitor,
  usePositionHistory,
  useSignalRejections,
} from "@/lib/hooks/queries";
import { unrealizedPnl } from "@/lib/pnl";

type ModeFilter = "all" | "paper" | "live";

const EXIT_ORDER_TYPES = new Set([
  "tp1",
  "tp2",
  "tp3",
  "take_profit",
  "stop_loss",
  "manual",
  "time_stop",
  "btc_macro_protection_loss_exit",
  "liquidation_guard_failed",
]);
const EMPTY_ORDERS: Order[] = [];

interface Props {
  health: Health;
  signals: Signal[];
  positions: Position[];
  strategies: Strategy[];
  logs: DecisionLog[];
  settings: AppSettings;
  marketIntelligence: MarketIntelligence;
  threshold: number;
  onRefresh: () => void;
  onOpenPosition: (position: Position) => void;
  onOpenSignal?: (signal: Signal) => void;
}

function asNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function unrealized(position: Position): number {
  const direct = asNumber(position.unrealized_pnl);
  if (direct !== null) return direct;
  return (
    unrealizedPnl({
      entryPrice: position.entry_price,
      size: position.size,
      markPrice: position.metadata_json?.last_mark_price,
      side: position.side,
    }) ?? 0
  );
}

function sideMultiplier(side: string): 1 | -1 {
  return side.toLowerCase() === "short" ? -1 : 1;
}

function exitOrdersFor(position: Position, orders: Order[]): Order[] {
  return orders
    .filter((order) => order.position_id === position.id && EXIT_ORDER_TYPES.has(order.order_type) && order.status === "filled")
    .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());
}

function exitPnl(position: Position, order: Order): number | null {
  const entry = asNumber(position.entry_price);
  const price = asNumber(order.price);
  const size = asNumber(order.size);
  if (entry == null || price == null || size == null) return null;
  return (price - entry) * size * sideMultiplier(position.side);
}

function realizedPnl(position: Position, orders: Order[] = []): number | null {
  const stored = asNumber(position.realized_pnl);
  if (stored !== null) return stored;
  const exitPnls = orders.map((order) => exitPnl(position, order)).filter((value): value is number => value !== null);
  if (exitPnls.length === 0) return null;
  return exitPnls.reduce((sum, value) => sum + value, 0);
}

function sourceSignalId(position: Position): number | null {
  const raw = position.metadata_json?.source_signal_id;
  const parsed = asNumber(raw);
  return parsed == null ? null : parsed;
}

interface Kpis {
  openPnl: number;
  openCount: number;
  todayPnl: number;
  todayCount: number;
  winRate30: number | null;
  closed30Count: number;
  pnlSpark: number[];
}

function modeLabel(mode: ModeFilter): string {
  if (mode === "paper") return "Paper";
  if (mode === "live") return "Live";
  return "Paper + Live";
}

function computeOpenPnl(positions: Position[]): Pick<Kpis, "openPnl" | "openCount"> {
  let openPnl = 0;
  let openCount = 0;
  for (const p of positions) {
    if (p.status.toLowerCase() !== "open") continue;
    openPnl += unrealized(p);
    openCount += 1;
  }
  return { openPnl, openCount };
}

function computeClosedKpis(positions: Position[], orders: Order[]): Omit<Kpis, "openPnl" | "openCount"> {
  const now = Date.now();
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const todayMs = todayStart.getTime();
  const cut30 = now - 30 * 24 * 60 * 60 * 1000;

  let todayPnl = 0;
  let todayCount = 0;
  let wins30 = 0;
  let losses30 = 0;
  const closed30: { time: number; pnl: number }[] = [];

  for (const p of positions) {
    const pnl = realizedPnl(p, exitOrdersFor(p, orders));
    if (pnl === null) continue;
    const eventAt = p.closed_at ?? p.opened_at;
    if (!eventAt) continue;
    const closedTime = new Date(eventAt).getTime();
    if (!Number.isFinite(closedTime)) continue;
    if (closedTime < cut30) continue;

    closed30.push({ time: closedTime, pnl });
    if (pnl > 0) wins30 += 1;
    else losses30 += 1;

    if (closedTime >= todayMs) {
      todayPnl += pnl;
      todayCount += 1;
    }
  }

  const total30 = wins30 + losses30;
  const winRate30 = total30 > 0 ? (wins30 / total30) * 100 : null;

  const sortedClosed = closed30.sort((a, b) => a.time - b.time);
  let cum = 0;
  const pnlSpark = sortedClosed.map((c) => {
    cum += c.pnl;
    return cum;
  });

  return {
    todayPnl,
    todayCount,
    winRate30,
    closed30Count: total30,
    pnlSpark,
  };
}

export function DashboardScreen({
  signals,
  positions,
  marketIntelligence,
  settings,
  threshold,
  onOpenPosition,
  onOpenSignal,
}: Props) {
  const [mode, setMode] = useState<ModeFilter>("live");
  const [openPositionsPage, setOpenPositionsPage] = useState(1);
  const [openPositionsPageSize, setOpenPositionsPageSize] = useState(6);

  const filteredPositions = useMemo(() => {
    if (mode === "all") return positions;
    return positions.filter((p) => p.mode.toLowerCase() === mode);
  }, [positions, mode]);

  const paperPositions = useMemo(
    () => positions.filter((p) => p.mode.toLowerCase() === "paper"),
    [positions],
  );
  const livePositions = useMemo(
    () => positions.filter((p) => p.mode.toLowerCase() === "live"),
    [positions],
  );

  const { data: rejections } = useSignalRejections(50);
  const { data: accountSnapshot } = useAccountMonitor();
  const paperEquity = asNumber(settings.paper?.equity) ?? 10_000;

  // La curva CR-PnL deve usare lo storico COMPLETO dei trade chiusi, non le
  // ultime 250 posizioni di /api/positions (cap per opened_at che mescola
  // paper+live e troncava il cumulato). Vedi /positions/history.
  const { data: history } = usePositionHistory({
    mode,
    period: "all",
    market_type: "all",
    limit: 5000,
  });
  const { data: history30 } = usePositionHistory({
    mode,
    period: "30d",
    market_type: "all",
    limit: 5000,
  });
  const equityPositions = history?.positions ?? filteredPositions;
  const history30Positions = history30?.positions ?? filteredPositions;
  const history30Orders = history30?.orders ?? EMPTY_ORDERS;

  const localOpenKpis = useMemo(() => computeOpenPnl(filteredPositions), [filteredPositions]);
  const liveOpenPnl = useMemo(
    () => accountSnapshot?.positions.reduce((sum, position) => sum + (asNumber(position.unrealized_pnl) ?? 0), 0) ?? null,
    [accountSnapshot?.positions],
  );
  const liveOpenCount = accountSnapshot?.positions.length ?? null;
  const kpis = useMemo(() => {
    const closed = computeClosedKpis(history30Positions, history30Orders);
    if (mode === "live" && liveOpenPnl != null) {
      return { ...closed, openPnl: liveOpenPnl, openCount: liveOpenCount ?? localOpenKpis.openCount };
    }
    if (mode === "all" && liveOpenPnl != null) {
      const paperOpen = computeOpenPnl(paperPositions);
      return { ...closed, openPnl: paperOpen.openPnl + liveOpenPnl, openCount: paperOpen.openCount + (liveOpenCount ?? 0) };
    }
    return { ...closed, ...localOpenKpis };
  }, [history30Orders, history30Positions, liveOpenCount, liveOpenPnl, localOpenKpis, mode, paperPositions]);
  const openPositionsCount = kpis.openCount;
  const filteredSignalIds = useMemo(() => {
    if (mode === "all") return null;
    const ids = new Set<number>();
    for (const position of filteredPositions) {
      const id = sourceSignalId(position);
      if (id != null) ids.add(id);
    }
    return ids;
  }, [filteredPositions, mode]);
  const filteredSignals = useMemo(
    () => filteredSignalIds == null ? signals : signals.filter((signal) => filteredSignalIds.has(signal.id)),
    [filteredSignalIds, signals],
  );

  useEffect(() => {
    setOpenPositionsPage(1);
  }, [mode]);

  useEffect(() => {
    const totalPages = Math.max(1, Math.ceil(openPositionsCount / openPositionsPageSize));
    setOpenPositionsPage((page) => Math.min(page, totalPages));
  }, [openPositionsCount, openPositionsPageSize]);

  // Baseline equity dipende dalla mode:
  // - paper: equity iniziale paper trading (es. 10k)
  // - live:  baseline 0 (mostra cumulative realized PnL puro)
  // - all:   paper baseline (riferimento storico)
  const chartBaseline =
    mode === "live" ? 0 : paperEquity;

  const modeDescription =
    mode === "paper"
      ? `Paper trading · ${paperPositions.length} pos. totali`
      : mode === "live"
        ? `Live trading · ${livePositions.length} pos. totali`
        : `Paper + Live aggregati · ${positions.length} pos. totali`;

  return (
    <div className="space-y-7 px-7 py-7">
      {/* Page header con switcher Paper/Live */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Dashboard</h1>
          <p className="mt-1 text-xs text-fg-3">{modeDescription}</p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-medium uppercase tracking-wider text-fg-3">
            Trading mode
          </span>
          <ToggleGroup
            type="single"
            value={mode}
            onValueChange={(v: string) => v && setMode(v as ModeFilter)}
            size="sm"
          >
            <ToggleGroupItem value="all">Tutto</ToggleGroupItem>
            <ToggleGroupItem value="paper">
              <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-warn" />
              Paper
              <span className="ml-1.5 font-mono text-[10px] text-fg-4">
                {paperPositions.length}
              </span>
            </ToggleGroupItem>
            <ToggleGroupItem value="live">
              <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-danger" />
              Live
              <span className="ml-1.5 font-mono text-[10px] text-fg-4">
                {livePositions.length}
              </span>
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
      </div>

      {/* Row 1 — KPI cards (4) */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          label="Open PnL"
          value={fmt.pnl(kpis.openPnl)}
          icon={Activity}
          delta={
            kpis.openCount > 0
              ? {
                  value: `${kpis.openCount} pos.`,
                  tone: kpis.openPnl > 0 ? "up" : kpis.openPnl < 0 ? "down" : "muted",
                }
              : undefined
          }
          hint={kpis.openCount === 0 ? "Nessuna posizione aperta" : undefined}
        />
        <KpiCard
          label="Today PnL"
          value={fmt.pnl(kpis.todayPnl)}
          icon={DollarSign}
          delta={{
            value: `${kpis.todayCount} trade`,
            tone: kpis.todayPnl > 0 ? "up" : kpis.todayPnl < 0 ? "down" : "muted",
          }}
        />
        <KpiCard
          label="Win rate 30g"
          value={kpis.winRate30 != null ? `${kpis.winRate30.toFixed(1)}%` : "—"}
          icon={TrendingUp}
          delta={{
            value: `${kpis.closed30Count} trade`,
            tone:
              kpis.winRate30 == null
                ? "muted"
                : kpis.winRate30 >= 55
                  ? "up"
                  : kpis.winRate30 >= 45
                    ? "muted"
                    : "down",
          }}
          spark={kpis.pnlSpark.length > 1 ? kpis.pnlSpark : undefined}
        />
        <KpiCard
          label="Segnali attivi"
          value={String(filteredSignals.filter((s) => s.status === "new").length)}
          icon={BarChart3}
          hint={`Soglia ≥ ${threshold}`}
          delta={{
            value: `${filteredSignals.length} totali`,
            tone: "muted",
          }}
        />
      </div>

      {/* Row 2 — Equity curve (8) + Signal funnel (4) */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <EquityChart
          positions={equityPositions}
          initialEquity={chartBaseline}
          className="lg:col-span-2"
          label={modeLabel(mode)}
        />
        <SignalFunnel
          signals={filteredSignals}
          rejections={rejections ?? { total: 0, by_reason: {}, recent: [] }}
        />
      </div>

      {/* Row 3 — Top signals (6) + Open positions (6) */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        
        <OpenPositionsLive
          positions={filteredPositions}
          onOpen={onOpenPosition}
          label={modeLabel(mode)}
          page={openPositionsPage}
          pageSize={openPositionsPageSize}
          onPageChange={setOpenPositionsPage}
          onPageSizeChange={(pageSize) => {
            setOpenPositionsPageSize(pageSize);
            setOpenPositionsPage(1);
          }}
          className="h-104"
        />
        <TopSignalsList
          signals={filteredSignals}
          threshold={threshold}
          limit={6}
          onOpen={onOpenSignal}
          className="h-104"
        />
      </div>

      <details className="rounded-lg border border-border bg-bg-1">
        <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-fg-2 transition-colors hover:text-fg">
          Contesto mercato e segnali macro
          <span className="ml-2 text-xs font-normal text-fg-4">
            correlazioni e rischio
          </span>
        </summary>
        <div className="space-y-4 border-t border-border p-4">
          <MarketIntelligenceStrip
            data={marketIntelligence}
            settings={settings}
          />
          <MacroCorrelationCard />
        </div>
      </details>
    </div>
  );
}

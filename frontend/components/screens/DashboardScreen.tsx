"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, BarChart3, DollarSign, TrendingUp } from "lucide-react";

import {
  AppSettings,
  DecisionLog,
  Health,
  MarketIntelligence,
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
import { BitcoinNewsCard } from "@/components/widgets/BitcoinNewsCard";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useMarketDashboardSnapshot, useSignalRejections } from "@/lib/hooks/queries";

type ModeFilter = "all" | "paper" | "live";

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
  const mark = asNumber(position.metadata_json?.last_mark_price);
  const entry = asNumber(position.entry_price);
  const size = asNumber(position.size);
  if (mark == null || entry == null || size == null) return 0;
  return (mark - entry) * Math.abs(size) * (position.side.toLowerCase() === "short" ? -1 : 1);
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

function computeKpis(positions: Position[]): Kpis {
  const now = Date.now();
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const todayMs = todayStart.getTime();
  const cut30 = now - 30 * 24 * 60 * 60 * 1000;

  let openPnl = 0;
  let openCount = 0;
  let todayPnl = 0;
  let todayCount = 0;
  let wins30 = 0;
  let losses30 = 0;
  const closed30: { time: number; pnl: number }[] = [];

  for (const p of positions) {
    if (p.status.toLowerCase() === "open") {
      openPnl += unrealized(p);
      openCount += 1;
      continue;
    }
    if (!p.closed_at) continue;
    const closedTime = new Date(p.closed_at).getTime();
    if (!Number.isFinite(closedTime)) continue;
    const pnl = asNumber(p.realized_pnl) ?? 0;

    if (closedTime >= todayMs) {
      todayPnl += pnl;
      todayCount += 1;
    }
    if (closedTime >= cut30) {
      closed30.push({ time: closedTime, pnl });
      if (pnl > 0) wins30 += 1;
      else if (pnl < 0) losses30 += 1;
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
    openPnl,
    openCount,
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

  const kpis = useMemo(() => computeKpis(filteredPositions), [filteredPositions]);
  const openPositionsCount = useMemo(
    () => filteredPositions.filter((p) => p.status.toLowerCase() === "open").length,
    [filteredPositions],
  );
  const { data: rejections } = useSignalRejections(50);
  const { data: marketDashboard } = useMarketDashboardSnapshot();
  const paperEquity = asNumber(settings.paper?.equity) ?? 10_000;

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
          value={String(signals.filter((s) => s.status === "new").length)}
          icon={BarChart3}
          hint={`Soglia ≥ ${threshold}`}
          delta={{
            value: `${signals.length} totali`,
            tone: "muted",
          }}
        />
      </div>

      {/* Row 2 — Equity curve (8) + Signal funnel (4) */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <EquityChart
          positions={filteredPositions}
          initialEquity={chartBaseline}
          className="lg:col-span-2"
          label={modeLabel(mode)}
        />
        <SignalFunnel
          signals={signals}
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
          signals={signals}
          threshold={threshold}
          limit={6}
          onOpen={onOpenSignal}
          className="h-104"
        />
      </div>

      {/* Row 4 — Market intelligence + latest news */}
      <MarketIntelligenceStrip
        data={marketIntelligence}
        settings={settings}
        snapshot={marketDashboard}
      />
      <BitcoinNewsCard snapshot={marketDashboard} />
    </div>
  );
}

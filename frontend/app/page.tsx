import { dehydrate, HydrationBoundary } from "@tanstack/react-query";

import { AppShell } from "@/components/AppShell";
import {
  AppSettings,
  DecisionLog,
  Health,
  Market,
  MarketIntelligence,
  Order,
  Position,
  Signal,
  SignalRejections,
  Strategy,
  WorkerCycle,
  apiGet,
} from "@/lib/api";
import { hydrateOpenPositionSourceSignals } from "@/lib/signalHydration";
import { makeQueryClient } from "@/lib/query-client";
import { qk } from "@/lib/hooks/queryKeys";

export const dynamic = "force-dynamic";

type Candle = {
  open_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};

async function loadDashboard() {
  const [
    health,
    markets,
    chartMarkets,
    signals,
    positions,
    orders,
    settings,
    strategies,
    logs,
    workerCycles,
    signalRejections,
    marketIntelligence,
  ] = await Promise.all([
    apiGet<Health>("/api/health"),
    apiGet<Market[]>("/api/markets"),
    apiGet<Market[]>("/api/markets?active=true&has_candles=true"),
    apiGet<Signal[]>("/api/signals?min_score=0"),
    apiGet<Position[]>("/api/positions"),
    apiGet<Order[]>("/api/orders"),
    apiGet<AppSettings>("/api/settings"),
    apiGet<Strategy[]>("/api/strategies"),
    apiGet<DecisionLog[]>("/api/logs").catch(() => []),
    apiGet<WorkerCycle[]>("/api/analytics/worker-cycles?limit=20").catch(() => []),
    apiGet<SignalRejections>("/api/analytics/signal-rejections?limit=50").catch(() => ({ total: 0, by_reason: {}, recent: [] })),
    apiGet<MarketIntelligence>("/api/market-intelligence").catch(() => ({
      fear_greed: null,
      altcoin_season: null,
      global_metrics: null,
    } as MarketIntelligence)),
  ]);
  const hydratedSignals = await hydrateOpenPositionSourceSignals(signals, positions);
  const latestSignal = hydratedSignals[0];
  const configuredTimeframes = Array.isArray(settings.timeframes?.enabled)
    ? settings.timeframes.enabled.filter((value): value is string => typeof value === "string")
    : [];
  const chartMarket = latestSignal?.market ?? chartMarkets[0] ?? null;
  const chartTimeframe = latestSignal?.timeframe ?? configuredTimeframes[0] ?? "";
  const candles = chartMarket && chartTimeframe
    ? await apiGet<Candle[]>(
        `/api/candles?symbol=${encodeURIComponent(chartMarket.symbol)}&market_type=${encodeURIComponent(
          chartMarket.market_type,
        )}&timeframe=${encodeURIComponent(chartTimeframe)}`,
      ).catch(() => [])
    : [];
  return {
    health,
    markets,
    chartMarkets,
    signals: hydratedSignals,
    chartSignals: hydratedSignals,
    candles,
    chartMarket,
    chartTimeframe,
    positions,
    orders,
    settings,
    strategies,
    logs,
    workerCycles,
    signalRejections,
    marketIntelligence,
  };
}

export default async function Home() {
  const data = await loadDashboard();

  // Pre-popola la cache TanStack per gli hook usati dai nuovi shell components.
  const queryClient = makeQueryClient();
  queryClient.setQueryData(qk.health, data.health);
  queryClient.setQueryData(qk.markets(""), data.markets);
  queryClient.setQueryData(qk.signals("min_score=0"), data.signals);
  queryClient.setQueryData(qk.positions, data.positions);
  queryClient.setQueryData(qk.orders, data.orders);
  queryClient.setQueryData(qk.settings, data.settings);
  queryClient.setQueryData(qk.strategies, data.strategies);
  queryClient.setQueryData(qk.logs, data.logs);
  queryClient.setQueryData(qk.workerCycles(20), data.workerCycles);
  queryClient.setQueryData(qk.signalRejections(50), data.signalRejections);
  queryClient.setQueryData(qk.marketIntelligence, data.marketIntelligence);

  return (
    <HydrationBoundary state={dehydrate(queryClient)}>
      <AppShell initialData={data} />
    </HydrationBoundary>
  );
}

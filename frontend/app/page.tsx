import { AppShell } from "@/components/AppShell";
import {
  AppSettings,
  Calibration,
  DecisionLog,
  Health,
  Market,
  MarketIntelligence,
  Order,
  Position,
  Signal,
  Strategy,
  apiGet
} from "@/lib/api";

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
    calibration,
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
    apiGet<DecisionLog[]>("/api/logs"),
    apiGet<Calibration>("/api/analytics/calibration"),
    apiGet<MarketIntelligence>("/api/market-intelligence").catch(() => ({
      fear_greed: null,
      altcoin_season: null,
    }))
  ]);
  const latestSignal = signals[0];
  const configuredTimeframes = Array.isArray(settings.timeframes?.enabled)
    ? settings.timeframes.enabled.filter((value): value is string => typeof value === "string")
    : [];
  const chartMarket = latestSignal?.market ?? chartMarkets[0] ?? null;
  const chartTimeframe = latestSignal?.timeframe ?? configuredTimeframes[0] ?? "";
  const candles = chartMarket && chartTimeframe
    ? await apiGet<Candle[]>(
        `/api/candles?symbol=${encodeURIComponent(chartMarket.symbol)}&market_type=${encodeURIComponent(
          chartMarket.market_type
        )}&timeframe=${encodeURIComponent(chartTimeframe)}`
      ).catch(() => [])
    : [];
  return {
    health,
    markets,
    chartMarkets,
    signals,
    chartSignals: signals,
    candles,
    chartMarket,
    chartTimeframe,
    positions,
    orders,
    settings,
    strategies,
    logs,
    calibration,
    marketIntelligence
  };
}

export default async function Home() {
  const data = await loadDashboard();
  return <AppShell initialData={data} />;
}

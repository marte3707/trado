"use client";

import { useQuery, type UseQueryOptions } from "@tanstack/react-query";

import {
  apiGet,
  type AccountMonitor,
  type AppSettings,
  type AutoTunerSourceTests,
  type AutoTunerStatus,
  type Calibration,
  type CalibrationHistoryEntry,
  type CalibrationStability,
  type CoinGeckoUsage,
  type CoinMarketCapUsage,
  type DecisionLog,
  type DiagnosticReport,
  type EquitySlopeDiagnostics,
  type Health,
  type Market,
  type MarketIntelligence,
  type Order,
  type Position,
  type Signal,
  type SignalRejections,
  type Strategy,
  type WorkerCycle,
} from "@/lib/api";
import { qk } from "@/lib/hooks/queryKeys";

export { qk };

/* ──────────────────────────────────────────────────────────────────────────
 *  Queries
 * ─────────────────────────────────────────────────────────────────────── */

type QueryOpts<T> = Omit<UseQueryOptions<T, Error, T, readonly unknown[]>, "queryKey" | "queryFn">;

export function useHealth(opts?: QueryOpts<Health>) {
  return useQuery({
    queryKey: qk.health,
    queryFn: () => apiGet<Health>("/api/health"),
    refetchInterval: 15_000,
    ...opts,
  });
}

export function useMarkets(filters?: string, opts?: QueryOpts<Market[]>) {
  return useQuery({
    queryKey: qk.markets(filters),
    queryFn: () => apiGet<Market[]>(`/api/markets${filters ? `?${filters}` : ""}`),
    refetchInterval: 60_000,
    ...opts,
  });
}

export function useSignals(filters?: string, opts?: QueryOpts<Signal[]>) {
  return useQuery({
    queryKey: qk.signals(filters),
    queryFn: () => apiGet<Signal[]>(`/api/signals${filters ? `?${filters}` : "?min_score=0"}`),
    refetchInterval: 15_000,
    ...opts,
  });
}

export function usePositions(opts?: QueryOpts<Position[]>) {
  return useQuery({
    queryKey: qk.positions,
    queryFn: () => apiGet<Position[]>("/api/positions"),
    refetchInterval: 15_000,
    ...opts,
  });
}

export function useOrders(opts?: QueryOpts<Order[]>) {
  return useQuery({
    queryKey: qk.orders,
    queryFn: () => apiGet<Order[]>("/api/orders"),
    refetchInterval: 15_000,
    ...opts,
  });
}

export function useStrategies(opts?: QueryOpts<Strategy[]>) {
  return useQuery({
    queryKey: qk.strategies,
    queryFn: () => apiGet<Strategy[]>("/api/strategies"),
    refetchInterval: 30_000,
    ...opts,
  });
}

export function useSettings(opts?: QueryOpts<AppSettings>) {
  return useQuery({
    queryKey: qk.settings,
    queryFn: () => apiGet<AppSettings>("/api/settings"),
    refetchInterval: 30_000,
    ...opts,
  });
}

export function useDecisionLogs(opts?: QueryOpts<DecisionLog[]>) {
  return useQuery({
    queryKey: qk.logs,
    queryFn: () => apiGet<DecisionLog[]>("/api/logs"),
    refetchInterval: 30_000,
    ...opts,
  });
}

export interface DecisionLogFilters {
  signal_id?: number;
  position_id?: number;
  market_id?: number;
  event_type?: string;
  limit?: number;
}

export function useDecisionLogsFiltered(filters: DecisionLogFilters, opts?: QueryOpts<DecisionLog[]>) {
  const params = new URLSearchParams();
  if (filters.signal_id != null) params.set("signal_id", String(filters.signal_id));
  if (filters.position_id != null) params.set("position_id", String(filters.position_id));
  if (filters.market_id != null) params.set("market_id", String(filters.market_id));
  if (filters.event_type) params.set("event_type", filters.event_type);
  if (filters.limit != null) params.set("limit", String(filters.limit));
  const qs = params.toString();
  return useQuery({
    queryKey: ["logs-filtered", qs],
    queryFn: () => apiGet<DecisionLog[]>(`/api/logs${qs ? `?${qs}` : ""}`),
    refetchInterval: 30_000,
    ...opts,
  });
}

export function useCalibration(opts?: QueryOpts<Calibration>) {
  return useQuery({
    queryKey: qk.calibration,
    queryFn: () => apiGet<Calibration>("/api/analytics/calibration"),
    refetchInterval: 30_000,
    ...opts,
  });
}

export function useCalibrationHistory(opts?: QueryOpts<CalibrationHistoryEntry[]>) {
  return useQuery({
    queryKey: qk.calibrationHistory,
    queryFn: () => apiGet<CalibrationHistoryEntry[]>("/api/analytics/calibration/history"),
    refetchInterval: 60_000,
    ...opts,
  });
}

export function useCalibrationStability(opts?: QueryOpts<CalibrationStability | null>) {
  return useQuery({
    queryKey: qk.calibrationStability,
    queryFn: () =>
      apiGet<CalibrationStability>("/api/analytics/calibration/stability").catch(() => null),
    refetchInterval: 60_000,
    ...opts,
  });
}

export function useTunerStatus(opts?: QueryOpts<AutoTunerStatus | null>) {
  return useQuery({
    queryKey: qk.tunerStatus,
    queryFn: () => apiGet<AutoTunerStatus>("/api/auto-tuner/status").catch(() => null),
    refetchInterval: 10_000,
    ...opts,
  });
}

export function useTunerSourceTests(opts?: QueryOpts<AutoTunerSourceTests | null>) {
  return useQuery({
    queryKey: qk.tunerSourceTests,
    queryFn: () => apiGet<AutoTunerSourceTests>("/api/auto-tuner/source-tests").catch(() => null),
    refetchInterval: 15_000,
    ...opts,
  });
}

export function useWorkerCycles(limit = 20, opts?: QueryOpts<WorkerCycle[]>) {
  return useQuery({
    queryKey: qk.workerCycles(limit),
    queryFn: () =>
      apiGet<WorkerCycle[]>(`/api/analytics/worker-cycles?limit=${limit}`).catch(() => []),
    refetchInterval: 30_000,
    ...opts,
  });
}

export function useSignalRejections(limit = 50, opts?: QueryOpts<SignalRejections>) {
  return useQuery({
    queryKey: qk.signalRejections(limit),
    queryFn: () =>
      apiGet<SignalRejections>(`/api/analytics/signal-rejections?limit=${limit}`).catch(
        () => ({ total: 0, by_reason: {}, recent: [] }) as SignalRejections,
      ),
    refetchInterval: 30_000,
    ...opts,
  });
}

export function useMarketIntelligence(opts?: QueryOpts<MarketIntelligence>) {
  return useQuery({
    queryKey: qk.marketIntelligence,
    queryFn: () =>
      apiGet<MarketIntelligence>("/api/market-intelligence").catch(
        () => ({ fear_greed: null, altcoin_season: null, global_metrics: null }) as MarketIntelligence,
      ),
    refetchInterval: 60_000,
    ...opts,
  });
}

export function useEquitySlope(opts?: QueryOpts<EquitySlopeDiagnostics | null>) {
  return useQuery({
    queryKey: qk.equitySlope,
    queryFn: () => apiGet<EquitySlopeDiagnostics>("/api/analytics/equity-slope").catch(() => null),
    refetchInterval: 60_000,
    ...opts,
  });
}

export function useDiagnostics(opts?: QueryOpts<DiagnosticReport | null>) {
  return useQuery({
    queryKey: qk.diagnostics,
    queryFn: () => apiGet<DiagnosticReport>("/api/analytics/diagnostics").catch(() => null),
    refetchInterval: 60_000,
    ...opts,
  });
}

export function useAccountMonitor(opts?: QueryOpts<AccountMonitor | null>) {
  return useQuery({
    queryKey: qk.accountMonitor,
    queryFn: () => apiGet<AccountMonitor>("/api/account/snapshot").catch(() => null),
    refetchInterval: 5_000,
    ...opts,
  });
}

export function useCoinMarketCapUsage(opts?: QueryOpts<CoinMarketCapUsage | null>) {
  return useQuery({
    queryKey: qk.coinMarketCapUsage,
    queryFn: () =>
      apiGet<CoinMarketCapUsage>("/api/market-intelligence/coinmarketcap-usage").catch(() => null),
    refetchInterval: 5 * 60_000,
    ...opts,
  });
}

export function useCoinGeckoUsage(opts?: QueryOpts<CoinGeckoUsage | null>) {
  return useQuery({
    queryKey: qk.coinGeckoUsage,
    queryFn: () =>
      apiGet<CoinGeckoUsage>("/api/market-intelligence/coingecko-usage").catch(() => null),
    refetchInterval: 5 * 60_000,
    ...opts,
  });
}

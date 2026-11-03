/**
 * Query keys condivisi tra server (HydrationBoundary in app/page.tsx)
 * e client (hooks in queries.ts / mutations.ts).
 *
 * Questo modulo NON ha "use client" perche' deve poter essere importato
 * sia da Server Component (RSC) sia da Client Component senza errori.
 */

export const qk = {
  health: ["health"] as const,
  markets: (filters?: string) => ["markets", filters ?? ""] as const,
  signals: (filters?: string) => ["signals", filters ?? ""] as const,
  signalSnapshot: (signalId: number | null | undefined) => ["signals", signalId ?? "none", "snapshot"] as const,
  positions: ["positions"] as const,
  positionHistory: (filters: string) => ["positions", "history", filters] as const,
  orders: ["orders"] as const,
  strategies: ["strategies"] as const,
  settings: ["settings"] as const,
  logs: ["logs"] as const,
  outcomeProfile: (lookback: number, mode: string | null = null) =>
    ["outcome-profile", lookback, mode] as const,
  experimentOutcomes: (version: string | null = null) =>
    ["experiment-outcomes", version] as const,
  workerCycles: (limit: number) => ["worker", "cycles", limit] as const,
  systemCycles: ["system", "cycles"] as const,
  signalRejections: (limit: number) => ["signal-rejections", limit] as const,
  signalCandidateRejections: (limit: number) => ["signal-candidate-rejections", limit] as const,
  macroDirection: ["macro-direction"] as const,
  marketIntelligence: ["market-intelligence"] as const,
  macroCorrelation: (base: string, ref: string, timeframe: string) =>
    ["macro-correlation", base, ref, timeframe] as const,
  equitySlope: ["equity-slope"] as const,
  scoreCalibration: ["score-calibration"] as const,
  expectedRCalibration: ["expected-r-calibration"] as const,
  strategyEdge: ["strategy-edge"] as const,
  liveEligibility: ["live-eligibility"] as const,
  leaderboardAlpha: ["leaderboard-alpha"] as const,
  accountMonitor: ["account", "snapshot"] as const,
  accountEquityHistory: (days: number) => ["account", "equity-history", days] as const,
  coinMarketCapUsage: ["market-intelligence", "cmc-usage"] as const,
  coinGeckoUsage: ["market-intelligence", "cg-usage"] as const,
  botFunnel: ["bot-funnel"] as const,
} as const;

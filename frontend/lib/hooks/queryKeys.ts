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
  positions: ["positions"] as const,
  positionHistory: (filters: string) => ["positions", "history", filters] as const,
  orders: ["orders"] as const,
  strategies: ["strategies"] as const,
  settings: ["settings"] as const,
  logs: ["logs"] as const,
  calibration: ["calibration"] as const,
  calibrationHistory: ["calibration", "history"] as const,
  calibrationStability: ["calibration", "stability"] as const,
  outcomeProfile: (lookback: number) => ["outcome-profile", lookback] as const,
  tunerStatus: ["tuner", "status"] as const,
  tunerSourceTests: ["tuner", "source-tests"] as const,
  workerCycles: (limit: number) => ["worker", "cycles", limit] as const,
  signalRejections: (limit: number) => ["signal-rejections", limit] as const,
  marketIntelligence: ["market-intelligence"] as const,
  marketDashboard: ["market-intelligence", "dashboard"] as const,
  equitySlope: ["equity-slope"] as const,
  scoreCalibration: ["score-calibration"] as const,
  expectedRCalibration: ["expected-r-calibration"] as const,
  diagnostics: ["diagnostics"] as const,
  accountMonitor: ["account", "snapshot"] as const,
  accountEquityHistory: (days: number) => ["account", "equity-history", days] as const,
  coinMarketCapUsage: ["market-intelligence", "cmc-usage"] as const,
  coinGeckoUsage: ["market-intelligence", "cg-usage"] as const,
  botFunnel: ["bot-funnel"] as const,
} as const;

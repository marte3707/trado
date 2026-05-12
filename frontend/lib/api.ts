export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type Health = {
  status: string;
  mode: string;
  network: string;
  enable_live_trading: boolean;
  live_trading_allowed: boolean;
};

export type MarketIntelligence = {
  fear_greed: {
    value: number;
    classification: string | null;
    timestamp: string | null;
    source: string;
  } | null;
  altcoin_season: {
    altcoin_index: number;
    altcoin_marketcap: number | null;
    snapshot_time: string | null;
    yearly_high: number | null;
    yearly_high_date: string | null;
    yearly_low: number | null;
    yearly_low_date: string | null;
    source: string;
  } | null;
};

export type Market = {
  id: number;
  symbol: string;
  base: string;
  quote: string;
  market_type: string;
  active: boolean;
  metadata_json: Record<string, unknown>;
};

export type Signal = {
  id: number;
  market_id: number;
  market: Market;
  timeframe: string;
  direction: string;
  score: number;
  confidence_label: string;
  entry_price: string;
  stop_loss: string | null;
  take_profit_1: string | null;
  take_profit_2: string | null;
  take_profit_3: string | null;
  risk_reward: string | null;
  reason: string;
  strategy_breakdown: Record<string, unknown>;
  status: string;
  created_at: string;
};

export type IndicatorSnapshot = {
  indicator_name: string;
  timeframe: string;
  timestamp: string;
  values: Record<string, unknown>;
};

export type Position = {
  id: number;
  market_id: number;
  market?: Market;
  mode: string;
  side: string;
  entry_price: string;
  size: string;
  stop_loss: string | null;
  take_profit: string | null;
  status: string;
  opened_at: string;
  closed_at: string | null;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  metadata_json: Record<string, unknown>;
};

export type Order = {
  id: number;
  position_id: number | null;
  signal_id: number | null;
  market?: Market | null;
  mode: string;
  order_type: string;
  side: string;
  price: string | null;
  size: string;
  status: string;
  created_at: string;
};

export type Strategy = {
  id: number;
  strategy_name: string;
  enabled: boolean;
  timeframe: string;
  params: Record<string, unknown>;
  weight: number;
};

export type DecisionLog = {
  id: number;
  timestamp: string;
  event_type: string;
  message: string;
  data: Record<string, unknown>;
};

export type AppSettings = Record<string, Record<string, unknown>>;

export type CalibrationStats = {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  realized_pnl: number;
  gross_profit: number;
  gross_loss: number;
  profit_factor: number | null;
};

export type CalibrationChange = {
  target: string;
  key: string;
  before: number | Record<string, unknown> | null;
  after: number | Record<string, unknown>;
  reason: string;
};

export type Calibration = {
  applied: boolean;
  eligible: boolean;
  reason: string;
  overall: CalibrationStats;
  changes: CalibrationChange[];
  strategy_adjustments: Record<string, Record<string, unknown>>;
  settings: Record<string, unknown>;
  state?: Record<string, unknown>;
  baseline?: Record<string, unknown>;
};

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`API ${path} failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function apiPost<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { method: "POST" });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`API ${path} failed: ${response.status} ${detail}`);
  }
  return response.json() as Promise<T>;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { method: "DELETE" });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`API ${path} failed: ${response.status} ${detail}`);
  }
  return response.json() as Promise<T>;
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`API ${path} failed: ${response.status} ${detail}`);
  }
  return response.json() as Promise<T>;
}

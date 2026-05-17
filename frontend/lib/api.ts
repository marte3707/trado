export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type Health = {
  status: string;
  mode: string;
  network: string;
  enable_live_trading: boolean;
  live_trading_allowed: boolean;
  live_readiness?: {
    account_address_configured: boolean;
    private_key_configured: boolean;
    mainnet_live_enabled: boolean;
    checks: string[];
  };
  worker?: {
    last_run_at: string | null;
    seconds_since_last_run: number | null;
    healthy: boolean;
    phase: string | null;
    last_cycle_at: string | null;
    last_cycle: {
      markets?: number;
      timeframes?: string[] | number;
      active_timeframes?: string[] | number;
      synced?: number;
      skipped?: number;
      generated?: number;
      closed?: number;
      pnl_updated?: number;
      live_reconciled?: number;
      calibrated?: number;
    } | null;
    last_error_at: string | null;
    last_error_message: string | null;
  };
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
  global_metrics: {
    btc_price_usd: number | null;
    total_market_cap_usd: number | null;
    total_market_cap_ex_top10_usd: number | null;
    total_volume_24h_usd: number | null;
    volume_trend: "up" | "down" | null;
    btc_dominance_pct: number | null;
    market_cap_change_24h_pct: number | null;
    last_updated: string | null;
    source: string;
  } | null;
};

export type CoinMarketCapUsage = {
  enabled: boolean;
  available: boolean;
  checked_at: string | null;
  disabled_until: string | null;
  disabled_reason: string | null;
  plan: Record<string, unknown>;
  usage: Record<string, unknown>;
  source: string;
};

export type CoinGeckoUsage = {
  enabled: boolean;
  checked_at: string | null;
  data: Record<string, unknown>;
  source: string;
};

export type AccountMonitor = {
  status: string;
  network: string;
  checked_at: string;
  api: {
    status: string;
    latency_ms: number | null;
    info_url: string;
    ws_url: string;
    universe_count: number;
    mids_count: number;
    error: string | null;
  };
  account: {
    configured: boolean;
    address: string | null;
    account_value: string | null;
    effective_account_value: string | null;
    free_spot_usdc: string | null;
    withdrawable: string | null;
    total_margin_used: string | null;
    total_notional_position: string | null;
    cross_account_value: string | null;
    cross_margin_used: string | null;
    cross_maintenance_margin_used: string | null;
    open_positions: number;
    open_orders: number;
    error: string | null;
  };
  positions: AccountPosition[];
  spot_balances: AccountSpotBalance[];
  open_orders: AccountOpenOrder[];
  funding: {
    status: string;
    predicted: AccountFunding[];
    user_last_24h: Record<string, unknown>[];
    error: string | null;
  };
  last_snapshot: {
    timestamp: string;
    total_equity: string | null;
    available_balance: string | null;
    margin_used: string | null;
    unrealized_pnl: string | null;
  } | null;
};

export type AccountPosition = {
  coin: string | null;
  side: string | null;
  size: string | null;
  entry_price: string | null;
  position_value: string | null;
  unrealized_pnl: string | null;
  return_on_equity: string | null;
  liquidation_price: string | null;
  margin_used: string | null;
  leverage: Record<string, unknown> | null;
  cum_funding: Record<string, unknown> | null;
};

export type AccountSpotBalance = {
  coin: string | null;
  total: string | null;
  hold: string | null;
  entry_ntl: string | null;
};

export type AccountOpenOrder = {
  coin?: string | null;
  side?: string | null;
  sz?: string | number | null;
  origSz?: string | number | null;
  limitPx?: string | number | null;
  triggerPx?: string | number | null;
  triggerCondition?: string | null;
  oid?: string | number | null;
  timestamp?: number | null;
  reduceOnly?: boolean | null;
  orderType?: unknown;
  tif?: string | null;
  cloid?: string | null;
  [key: string]: unknown;
};

export type AccountFunding = {
  coin: string;
  hyperliquid: {
    exchange: string;
    funding_rate: string | null;
    next_funding_time: number | null;
    funding_interval_hours: number | null;
  } | null;
  exchanges: {
    exchange: string;
    funding_rate: string | null;
    next_funding_time: number | null;
    funding_interval_hours: number | null;
  }[];
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
  leverage: string | null;
  stop_loss: string | null;
  take_profit: string | null;
  status: string;
  opened_at: string;
  closed_at: string | null;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  initial_size: string | null;
  notional: string | null;
  initial_risk: string | null;
  total_pnl: string | null;
  return_pct: string | null;
  realized_return_pct: string | null;
  r_multiple: string | null;
  realized_r_multiple: string | null;
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
  market_id?: number | null;
  signal_id?: number | null;
  position_id?: number | null;
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

export type CalibrationHistoryEntry = {
  id: number;
  timestamp: string;
  event_type: string;
  message: string;
  status: string | null;
  reason: string | null;
  overall: CalibrationStats | null;
  changes: CalibrationChange[];
  error: string | null;
  force: boolean | null;
};

export type TunerStepMetrics = {
  trades: number;
  win_rate: number;
  realized_pnl?: number;
  profit_factor?: number;
  equity_slope: number;
};

export type TunerStepHistory = {
  key: string;
  label: string;
  status: "success" | "reverted" | "skipped";
  metrics_before: TunerStepMetrics | null;
  metrics_after: TunerStepMetrics | null;
  trades_evaluated: number;
  evaluated_at: string;
  manual?: boolean;
};

export type TunerProgress = {
  trades_done: number;
  trades_required: number;
};

export type AutoTunerStatus = {
  enabled: boolean;
  current_step_key: string | null;
  current_step_index: number | null;
  activated_at: string | null;
  metrics_before: TunerStepMetrics | null;
  next_step_key: string | null;
  trades_now: number;
  progress: TunerProgress | null;
  step_history: TunerStepHistory[];
  config: {
    min_trades_per_step: number;
    improvement_threshold: number;
  };
};

export type WorkerCycle = {
  id: number;
  timestamp: string;
  event_type: string;
  data: {
    markets?: number;
    synced?: number;
    skipped?: number;
    generated?: number;
    closed?: number;
    calibrated?: boolean;
    active_timeframes?: string[];
    error?: string;
  };
};

export type SignalRejections = {
  total: number;
  by_reason: Record<string, number>;
  recent: { id: number; timestamp: string; reasons: string[] }[];
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

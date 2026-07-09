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
  market_data?: {
    enabled: boolean;
    healthy: boolean;
    running: boolean;
    pid: number | null;
    label: string;
    source: string;
    connected?: boolean;
    ready?: boolean;
    expected_subscriptions?: number;
    active_subscriptions?: number;
    pending_events?: number;
    writer_queue_depth?: number;
    reconnect_count?: number;
    last_error?: string | null;
    last_message_at?: string | null;
    last_persisted_at?: string | null;
    seconds_since_last_message?: number | null;
  };
  bot?: {
    running: boolean;
    pid: number | null;
    label: string;
    source: string;
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
    btc_price_change_24h_pct: number | null;
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

export type MarketDashboardMover = {
  symbol: string;
  name: string;
  price_usd: number | null;
  price_change_24h_pct: number;
};

export type MarketDashboardNewsItem = {
  title: string;
  url: string;
  source: string | null;
  published_at: string | null;
  excerpt: string | null;
};

export type MarketDashboardSnapshot = {
  generated_at: string;
  source_url: string;
  cache: {
    hit: boolean;
    age_seconds: number;
    max_age_seconds: number;
    expires_at: string;
  };
  metrics: {
    btc_price_usd: number | null;
    btc_price_change_1h_pct: number | null;
    btc_price_change_24h_pct: number | null;
    btc_price_change_7d_pct: number | null;
    total_market_cap_usd: number | null;
    total_market_cap_ex_top10_usd: number | null;
    total_volume_24h_usd: number | null;
    btc_dominance_pct: number | null;
    market_cap_change_24h_pct: number | null;
    last_updated: string | null;
    source: string;
    top_gainers_24h: MarketDashboardMover[];
    top_losers_24h: MarketDashboardMover[];
  } | null;
  fear_greed: {
    value: number;
    classification: string | null;
    timestamp: string | null;
    source: string;
  } | null;
  altcoin_season: {
    altcoin_index: number;
    snapshot_time: string | null;
    source: string;
  } | null;
  news: MarketDashboardNewsItem[];
  errors: string[];
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

export type SystemService = "backend" | "worker" | "marketdata" | "bot";
export type SystemServiceAction = "start" | "stop" | "reboot";

export type SystemServiceResult = {
  status: string;
  service: SystemService;
  action: SystemServiceAction;
  label: string;
};

export type SystemErrorLog = {
  path: string;
  exists: boolean;
  updated_at: string | null;
  lines: string[];
};

export type SystemErrorLogs = Record<SystemService, SystemErrorLog>;

export type AccountEquityPoint = {
  ts: string;
  equity: number;
  drawdown_pct: number;
};

export type AccountEquityHistory = {
  points: AccountEquityPoint[];
  current_equity: number | null;
  peak_equity: number | null;
  max_drawdown_pct: number;
  days: number;
  source: "hyperliquid" | "local";
  hl_error: string | null;
};

export type AccountLedgerTransfer = {
  time: number | null;
  kind: "deposit" | "withdrawal";
  label: string;
  raw_type: string | null;
  asset: string | null;
  amount: string | null;
  signed_amount: string | null;
  hash: string | null;
};

export type AccountMonitor = {
  status: string;
  network: string;
  checked_at: string;
  api: {
    status: string;
    source: "websocket" | "rest";
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
    cross_ntl_pos: string | null;
    cross_account_value: string | null;
    cross_margin_used: string | null;
    cross_maintenance_margin_used: string | null;
    unified_account_ratio_pct: string | null;
    unified_account_ratio_status: string;
    unified_account_ratio_source: string;
    unified_account_ratio_threshold_pct: string | null;
    unified_account_ratio_numerator: string | null;
    unified_account_ratio_denominator: string | null;
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
  ledger: {
    status: string;
    lookback_days: number;
    transfers: AccountLedgerTransfer[];
    error: string | null;
  };
  performance: {
    status: string;
    basis: string;
    lookback_days: number;
    net_deposits_usdc: string | null;
    effective_equity_usdc: string | null;
    account_pnl_usdc: string | null;
    account_return_pct: string | null;
    open_unrealized_pnl_usdc: string | null;
    hl_closed_pnl_gross_usdc: string | null;
    hl_fees_usdc: string | null;
    hl_funding_usdc: string | null;
    known_components_pnl_usdc: string | null;
    reconciliation_gap_usdc: string | null;
    local_closed_realized_pnl_usdc: string | null;
    local_open_unrealized_pnl_usdc: string | null;
    local_filled_fees_usdc: string | null;
    fill_count: number;
    funding_count: number;
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
  protection?: AccountPositionProtection | null;
};

export type AccountPositionProtection = {
  status: "ok" | "intervention_required" | "untracked";
  needs_intervention: boolean;
  repairable: boolean;
  expected_orders: string[];
  active_orders: string[];
  missing_orders: string[];
  rejected_orders: string[];
  skipped_orders: string[];
  issues: string[];
  summary: string;
  local_position_ids: number[];
  stop_update_failed: boolean;
  stop_replacement_attempts: number;
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
  source_candle_open_time?: string | null;
  created_at: string;
  calibrated_expected_r?: number | null;
  calibrated_win_rate?: number | null;
  calibration_n_samples?: number | null;
  calibration_confidence?: "low" | "medium" | "high" | null;
};

export type IndicatorSnapshot = {
  indicator_name: string;
  timeframe: string;
  timestamp: string;
  values: Record<string, unknown>;
};

export type Candle = {
  open_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
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
  cum_funding_since_open: string | null;
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
  fee: string | null;
  created_at: string;
};

export type PositionHistoryFilters = {
  period?: "1d" | "7d" | "30d" | "all";
  mode?: "all" | "paper" | "live";
  market_type?: "all" | "perp" | "spot";
  limit?: number;
};

export type PositionHistoryResponse = {
  positions: Position[];
  orders: Order[];
  total: number;
  filters: {
    period: string;
    mode: string;
    market_type: string;
    limit: number;
    returned: number;
  };
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

export type SignalSnapshot = {
  signal: Signal;
  source_candle_open_time: string | null;
  candles: Candle[];
  indicators: IndicatorSnapshot[];
  positions: Position[];
  decision_logs: DecisionLog[];
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

export type OutcomeProfile = {
  filters: { lookback: number; mode?: string | null };
  overall: CalibrationStats;
  by_mode: Record<string, CalibrationStats>;
  by_direction: Record<string, CalibrationStats>;
  by_timeframe: Record<string, CalibrationStats>;
  by_score_bucket: Record<string, CalibrationStats>;
  by_market: Record<string, CalibrationStats>;
  by_exit_reason: Record<string, CalibrationStats>;
  by_tp_state: Record<string, CalibrationStats>;
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
  mean_pnl?: number;
  profit_factor?: number;
  equity_slope: number;
};

export type TunerStepHistory = {
  key: string;
  label: string;
  status: "success" | "reverted" | "skipped" | "promoted" | "rejected";
  mode?: string;
  ab_test?: Record<string, unknown>;
  metrics_before: TunerStepMetrics | null;
  metrics_after: TunerStepMetrics | null;
  trades_evaluated?: number;
  evaluated_at: string;
  manual?: boolean;
};

export type TunerProgress = {
  trades_done: number;
  trades_required: number;
};

export type AutoTunerAbProgress = {
  trades_control: number;
  trades_test: number;
  trades_required_per_arm: number;
  sample_balance?: number;
  mean_control?: number;
  mean_test?: number;
  mean_delta?: number;
  alpha?: number;
  assignment_method?: string;
  control_overlay?: Record<string, unknown>;
  test_overlay?: Record<string, unknown>;
  active_overlay?: Record<string, unknown>;
};

export type AutoTunerSourceTest = {
  source: string;
  key: string;
  label: string;
  control_overlay: Record<string, unknown>;
  test_overlay: Record<string, unknown>;
  currently_disabled: boolean;
  active: boolean;
  available: boolean;
};

export type AutoTunerStrategyTest = {
  timeframe: string;
  strategy: string;
  key: string;
  label: string;
  control_overlay: Record<string, unknown>;
  test_overlay: Record<string, unknown>;
  active: boolean;
  available: boolean;
  queued?: boolean;
  last_recommendation?: string | null;
};

export type AutoTunerSourceTests = {
  enabled: boolean;
  mode: string;
  active_step_key: string | null;
  active_mode?: string | null;
  blocked: boolean;
  tests: AutoTunerSourceTest[];
};

export type AutoTunerStrategyTests = {
  enabled: boolean;
  mode: string;
  active_step_key: string | null;
  active_mode?: string | null;
  blocked: boolean;
  queued_strategy_test?: {
    timeframe: string;
    strategy: string;
    step_key: string;
    queued_at?: string;
  } | null;
  tests: AutoTunerStrategyTest[];
};

export type AutoTunerStatus = {
  enabled: boolean;
  mode?: string;
  current_step_key: string | null;
  current_step_index: number | null;
  current_step_mode?: string | null;
  activated_at: string | null;
  metrics_before: TunerStepMetrics | null;
  metrics_now?: TunerStepMetrics | null;
  ab_test_state?: Record<string, unknown> | null;
  target_timeframe?: string | null;
  target_strategy?: string | null;
  recommendation?: string | null;
  queued_strategy_test?: {
    timeframe: string;
    strategy: string;
    step_key: string;
    queued_at?: string;
  } | null;
  next_step_key: string | null;
  trades_now: number;
  progress: TunerProgress | null;
  ab_progress?: AutoTunerAbProgress | null;
  step_history: TunerStepHistory[];
  config: {
    min_trades_per_step: number;
    improvement_threshold: number;
    mode?: string;
    ab_test_min_trades_per_arm?: number;
    ab_test_significance_level?: number;
    ab_test_allocation?: number;
    ab_test_assignment_salt?: string;
  };
  available_source_ab_sequence?: Array<Record<string, unknown>>;
};

export type EquitySlopeDiagnostics = {
  lookback: number;
  sample_count: number;
  status: "improving" | "neutral" | "declining";
  threshold: number;
  raw_slope: number;
  normalized_slope: number;
  recent_normalized_slope: number;
  median_abs_pnl: number;
  windows: Array<{
    window: number;
    sample_count: number;
    raw_slope: number;
    normalized_slope: number;
    median_abs_pnl: number;
  }>;
  deep_defensive_active: boolean;
  starvation_override_active: boolean;
  last_reason?: string | null;
  last_exit_reason?: string | null;
  positive_streak: number;
  recent_positive_streak: number;
  cooldown_remaining: number;
  consecutive_zero_signal_cycles: number;
  exit_conditions: {
    full_slope_non_negative_cycles: number;
    recent_slope_non_negative_cycles: number;
    starvation_hard_cycles: number;
  };
  recent_time_stop_impact: {
    hours: number;
    closed_count: number;
    realized_pnl: number;
  };
  baseline_vs_current: Record<string, unknown>;
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
    skip_reasons?: Record<string, number>;
    error?: string;
  };
};

export type SystemCycleService = {
  id: string;
  label: string;
  status: "ok" | "warn" | "failed" | "stale" | "skipped" | "unknown";
  state: "running" | "degraded" | "failing" | "stale" | "skipped" | "unknown";
  message: string | null;
  last_seen_at: string | null;
  last_ok_at: string | null;
  last_failure_at: string | null;
  expected_interval_seconds: number;
  stale_after_seconds: number;
  failure_count: number;
  failure_threshold: number;
  failure_window_seconds: number;
  consecutive_failures: number;
  data: Record<string, unknown>;
};

export type SystemCycleDiagnostics = {
  generated_at: string;
  status: "ok" | "degraded";
  degraded_count: number;
  services: SystemCycleService[];
};

export type SignalRejections = {
  total: number;
  by_reason: Record<string, number>;
  recent: { id: number; timestamp: string; reasons: string[] }[];
};

export type CalibrationStabilityEntry = {
  key: string;
  timeframe: string;
  strategy: string;
  enabled: boolean;
  weight: number;
  trades: number;
  factor_history: number[];
  factor_stddev: number;
  holdout: {
    train: CalibrationStats;
    test: CalibrationStats;
    wr_delta: number;
  } | null;
};

export type CalibrationStability = {
  settings: {
    walk_forward_train_ratio: number;
    walk_forward_min_per_side: number;
    factor_history_window: number;
    factor_history_std_threshold: number;
  };
  by_strategy: CalibrationStabilityEntry[];
};

export type DiagnosticReport = {
  lookback: number;
  trade_count: number;
  generated_at: string;
  overall: CalibrationStats;
  equity_slope: number;
  by_direction: Record<string, CalibrationStats>;
  by_strategy: Record<string, CalibrationStats>;
  by_timeframe: Record<string, CalibrationStats>;
  anomalies: string[];
  top_strategies: Array<CalibrationStats & { name: string }>;
  bottom_strategies: Array<CalibrationStats & { name: string }>;
  calibration_state: Record<string, unknown>;
  baseline_vs_current: Record<string, unknown>;
};

export class ApiError extends Error {
  status: number;
  detail: string;
  path: string;

  constructor(path: string, status: number, detail: string) {
    super(`API ${path} failed: ${status}${detail ? ` ${detail}` : ""}`);
    this.name = "ApiError";
    this.path = path;
    this.status = status;
    this.detail = detail;
  }
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const text = await response.text();
    if (!text) return "";
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") {
        const detail = (parsed as Record<string, unknown>).detail ?? (parsed as Record<string, unknown>).message;
        if (typeof detail === "string") return detail;
      }
    } catch {
      // fall through, return raw text
    }
    return text;
  } catch {
    return "";
  }
}

const API_TIMEOUT_MS = 15_000;

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(API_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new ApiError(path, response.status, await readErrorDetail(response));
  }
  return response.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method: "POST", signal: AbortSignal.timeout(API_TIMEOUT_MS) };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    throw new ApiError(path, response.status, await readErrorDetail(response));
  }
  return response.json() as Promise<T>;
}

export async function apiPut<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method: "PUT", signal: AbortSignal.timeout(API_TIMEOUT_MS) };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    throw new ApiError(path, response.status, await readErrorDetail(response));
  }
  return response.json() as Promise<T>;
}

export type ScoreCalibrationBucket = {
  range: string;
  min_score: number;
  count: number;
  win_rate: number;
  avg_pnl: number;
  profit_factor: number;
};

export type ScoreCalibrationPeriod = {
  period: string;
  avg_score: number;
  win_rate: number;
  count: number;
};

export type ScoreCalibration = {
  buckets: ScoreCalibrationBucket[];
  correlation: number | null;
  total_trades: number;
  by_week: ScoreCalibrationPeriod[];
};

export type MacroCorrelation = {
  base: string;
  ref: string;
  timeframe: string;
  lookback: number;
  samples: number;
  correlation: number | null;
  beta: number | null;
  base_last_close: number | null;
  ref_last_close: number | null;
};

export type ExpectedRBucket = {
  key: string;
  direction: "LONG" | "SHORT";
  timeframe: string;
  score_bucket: string;
  n_samples: number;
  wins: number;
  raw_win_rate: number | null;
  shrunk_win_rate: number | null;
  mean_r_win: number | null;
  mean_r_loss: number | null;
  expected_r: number | null;
  confidence: "low" | "medium" | "high";
};

export type ExpectedRCalibration = {
  buckets: Record<string, ExpectedRBucket>;
  computed_at: string;
  lookback_positions: number;
  excluded_count: number;
  prior_strength: number;
  min_samples_for_use: number;
};

export type StrategyEdgeClassification =
  | "robust"
  | "promising"
  | "negative"
  | "inert"
  | "insufficient";

export type StrategyEdgeOverall = {
  full: CalibrationStats;
  train: CalibrationStats | null;
  test: CalibrationStats | null;
  time_weighted: CalibrationStats;
  verdict: string;
  classification: StrategyEdgeClassification;
};

export type StrategyEdgeCell = {
  key: string;
  timeframe: string;
  strategy: string;
  enabled: boolean;
  classification: Exclude<StrategyEdgeClassification, "inert">;
  verdict: string;
  full: CalibrationStats;
  train: CalibrationStats | null;
  test: CalibrationStats | null;
};

export type StrategyEdgeStrategy = {
  name: string;
  classification: StrategyEdgeClassification;
  overall: CalibrationStats;
  recent: CalibrationStats;
  avg_contribution: number | null;
  participation: {
    aligned_signals_recent?: number;
    traded_positions?: number;
    traded_share?: number | null;
    inert?: boolean;
  };
};

export type StrategyEdgeSummary = {
  cells: Record<string, number>;
  strategies: Record<string, number>;
  positions_linked: number;
};

export type StrategyEdgeReport = {
  filters: {
    lookback: number;
    recent_window: number;
    min_test_samples: number;
  };
  overall: StrategyEdgeOverall;
  summary: StrategyEdgeSummary;
  cells: StrategyEdgeCell[];
  strategies: StrategyEdgeStrategy[];
  note: string;
};

export type LiveEligibilityClass =
  | "eligible"
  | "watch"
  | "quarantine"
  | "insufficient";

export type LiveEligibilityStats = {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  gross_r: number;
  fee_r: number;
  net_r: number;
  gross_profit_r: number;
  gross_loss_r: number;
  net_profit_factor: number | null;
  avg_net_r: number | null;
  median_net_r: number | null;
  avg_fee_r: number | null;
};

export type LiveEligibilityCohort = {
  key: string;
  type: "direction_timeframe" | "strategy" | "score_bucket";
  direction: "LONG" | "SHORT";
  timeframe: string;
  strategy: string | null;
  score_bucket: string | null;
  classification: LiveEligibilityClass;
  recommended_action: string;
  reasons: string[];
  active_risk_overlays: Array<{
    type: string;
    key: string;
    adjustment: number | null;
    payload: unknown;
  }>;
  full: LiveEligibilityStats;
  recent: LiveEligibilityStats;
  train: LiveEligibilityStats | null;
  test: LiveEligibilityStats | null;
};

export type LiveEligibilitySettingConflict = {
  severity: "info" | "warn";
  type: string;
  cohort_key: string;
  cohort_type: string;
  classification: LiveEligibilityClass;
  active_risk_overlays: LiveEligibilityCohort["active_risk_overlays"];
  message: string;
};

export type LiveEligibilityReport = {
  filters: {
    lookback: number;
    recent_window: number;
    taker_fee_rate: number;
  };
  thresholds: {
    min_trades: number;
    min_recent_trades: number;
    min_net_profit_factor: number;
    min_recent_net_profit_factor: number;
    min_avg_net_r: number;
    min_recent_avg_net_r: number;
  };
  macro_context: Record<string, unknown> & {
    direction?: string;
    reason?: string;
    suspended_penalty_family?: string | null;
  };
  summary: Record<LiveEligibilityClass, number> & {
    total_cohorts: number;
    paper_trades_loaded: number;
    paper_trades_usable: number;
    setting_conflicts: number;
    excluded: Record<string, number>;
  };
  overall: {
    full: LiveEligibilityStats;
    recent: LiveEligibilityStats;
  };
  direction_timeframes: LiveEligibilityCohort[];
  strategies: LiveEligibilityCohort[];
  score_buckets: LiveEligibilityCohort[];
  setting_conflicts: LiveEligibilitySettingConflict[];
  note: string;
};

export type LeaderboardAlphaClassification =
  | "candidate"
  | "watch"
  | "quarantine"
  | "insufficient";

export type LeaderboardAlphaSnapshot = {
  id: number;
  lookback_days: number;
  fills_count: number;
  closing_fills_count: number;
  open_fills_count: number;
  net_pnl_usdc: string | null;
  closed_pnl_usdc: string | null;
  fees_usdc: string | null;
  volume_usdc: string | null;
  win_rate: number | null;
  win_rate_pct: number | null;
  profit_factor: number | null;
  quality_score: number;
  classification: LeaderboardAlphaClassification;
  metrics: Record<string, unknown>;
  created_at: string;
};

export type LeaderboardAlphaTrader = {
  id: number;
  address: string;
  label: string | null;
  source: string;
  enabled: boolean;
  notes: string | null;
  last_sync_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
  last_seen_fill_at: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  latest_snapshot: LeaderboardAlphaSnapshot | null;
};

export type LeaderboardAlphaFill = {
  id: number;
  trader_id: number;
  trader_label: string | null;
  trader_address: string | null;
  coin: string;
  side: string | null;
  direction: string | null;
  intent: string | null;
  price: string | null;
  size: string | null;
  notional_usd: string | null;
  closed_pnl: string | null;
  fee: string | null;
  crossed: boolean | null;
  filled_at: string;
};

export type LeaderboardAlphaSignal = {
  coin: string;
  direction: "long" | "short" | string;
  trader_count: number;
  fill_count: number;
  notional_usd: string | null;
  latest_fill_at: string;
  avg_quality_score: number | null;
  mode: "confirm_only";
  confirmation_level: "single" | "cluster" | string;
  traders: Array<{
    id: number;
    label: string | null;
    address: string;
    quality_score: number | null;
    fill_notional_usd: string | null;
    filled_at: string;
  }>;
};

export type LeaderboardCopyPaperPosition = {
  id: number;
  trader_id: number;
  trader_label: string | null;
  trader_address: string | null;
  coin: string;
  direction: "long" | "short" | string;
  status: "open" | "closed" | string;
  entry_price: string | null;
  exit_price: string | null;
  copy_size: string | null;
  copy_notional_usd: string | null;
  original_notional_usd: string | null;
  entry_fee_usdc: string | null;
  exit_fee_usdc: string | null;
  gross_pnl_usdc: string | null;
  net_pnl_usdc: string | null;
  pnl_pct: number | null;
  opened_at: string;
  closed_at: string | null;
  close_reason: string | null;
  metadata_json: Record<string, unknown>;
};

export type LeaderboardCopyPaperRanking = {
  trader_id: number;
  trader_label: string | null;
  trader_address: string | null;
  leaderboard_rank: number | null;
  open_positions: number;
  closed_positions: number;
  net_pnl_usdc: string | null;
  win_rate_pct: number | null;
  profit_factor: number | null;
  avg_trade_pnl_usdc: string | null;
};

export type LeaderboardCopyPaper = {
  enabled: boolean;
  summary: {
    starting_equity_usd: string | null;
    equity_usd: string | null;
    realized_pnl_usdc: string | null;
    return_pct: number | null;
    open_positions: number;
    closed_positions: number;
    total_positions: number;
    win_rate_pct: number | null;
    profit_factor: number | null;
    avg_trade_pnl_usdc: string | null;
    total_fees_usdc: string | null;
    copied_traders: number;
    position_notional_usd: string;
    fee_rate: string;
  };
  open_positions: LeaderboardCopyPaperPosition[];
  recent_closed_positions: LeaderboardCopyPaperPosition[];
  trader_rankings: LeaderboardCopyPaperRanking[];
  by_status: Record<string, number>;
  tracked_traders: Array<{
    trader_id: number;
    label: string | null;
    address: string;
    leaderboard_rank: number | null;
  }>;
};

export type LeaderboardCopyLiveTrader = {
  trader_id: number;
  trader_label: string | null;
  trader_address: string | null;
  leaderboard_rank: number | null;
  classification: LeaderboardAlphaClassification | null;
  quality_score: number | null;
  live_candidate_score: number;
  eligible: boolean;
  reasons: string[];
  closed_positions: number;
  open_positions: number;
  planned_open_positions: number;
  net_pnl_usdc: string | null;
  avg_trade_pnl_usdc: string | null;
  profit_factor: number | null;
  win_rate_pct: number | null;
  recent_closed_positions: number;
  recent_net_pnl_usdc: string | null;
};

export type LeaderboardCopyLiveShadowPosition = {
  paper_position_id: number;
  trader_id: number;
  trader_label: string | null;
  trader_address: string | null;
  coin: string;
  direction: "long" | "short" | string;
  paper_notional_usd: string | null;
  live_notional_usd: string | null;
  opened_at: string;
};

export type LeaderboardCopyLive = {
  enabled: boolean;
  mode: "shadow" | string;
  orders_enabled: boolean;
  status: "shadow_ready" | "no_live_candidates" | string;
  reason: string;
  summary: {
    evaluated_traders: number;
    selected_traders: number;
    rejected_traders: number;
    planned_open_positions: number;
    estimated_open_notional_usd: string | null;
    position_notional_usd: string;
    max_traders: number;
    max_open_positions: number;
    max_open_positions_per_trader: number;
    skipped_open_positions: Record<string, number>;
  };
  selected_traders: LeaderboardCopyLiveTrader[];
  rejected_traders: LeaderboardCopyLiveTrader[];
  shadow_open_positions: LeaderboardCopyLiveShadowPosition[];
};

export type LeaderboardAlphaDashboard = {
  settings: {
    enabled: boolean;
    mode: "observe_only" | string;
    lookback_days: number;
    recent_signal_hours: number;
    min_traders_for_signal: number;
    min_quality_score: number;
    max_entry_lag_pct: number;
    sync_limit_per_run: number;
    auto_import_top_traders: boolean;
    auto_import_limit: number;
    auto_import_window: string;
    auto_import_min_volume_usd: string;
    auto_import_refresh_hours: number;
    auto_sync_enabled: boolean;
    auto_sync_interval_hours: number;
    include_auto_imported_in_signals: boolean;
    copy_paper_enabled: boolean;
    copy_paper_starting_equity_usd: string;
    copy_paper_position_notional_usd: string;
    copy_paper_fee_rate: string;
    copy_paper_min_source_notional_usd: string;
    copy_paper_max_open_positions: number;
    copy_paper_max_open_positions_per_trader: number;
    copy_paper_allowed_rank_limit: number;
    copy_paper_only_auto_top_traders: boolean;
    copy_paper_close_on_opposite_open: boolean;
    copy_paper_replay_batch_size: number;
    copy_live_enabled: boolean;
    copy_live_mode: string;
    copy_live_max_traders: number;
    copy_live_allowed_rank_limit: number;
    copy_live_allowed_classes: string[];
    copy_live_min_quality_score: number;
    copy_live_min_copy_closed_positions: number;
    copy_live_min_profit_factor: string;
    copy_live_min_win_rate_pct: number;
    copy_live_min_avg_trade_pnl_usdc: string;
    copy_live_min_net_pnl_usdc: string;
    copy_live_recent_days: number;
    copy_live_min_recent_closed_positions: number;
    copy_live_position_notional_usd: string;
    copy_live_max_open_positions: number;
    copy_live_max_open_positions_per_trader: number;
    copy_live_max_estimated_notional_usd: string;
    copy_live_excluded_coin_prefixes: string[];
  } & Record<string, unknown>;
  state: Record<string, unknown>;
  summary: {
    traders: number;
    enabled_traders: number;
    candidates: number;
    signals: number;
    last_sync_at: string | null;
    mode: "observe_only";
  };
  copy_paper: LeaderboardCopyPaper;
  copy_live: LeaderboardCopyLive;
  traders: LeaderboardAlphaTrader[];
  signals: LeaderboardAlphaSignal[];
  recent_fills: LeaderboardAlphaFill[];
};

export type LeaderboardAlphaSyncResult = {
  status: string;
  reason?: string | null;
  synced?: number;
  rate_limited?: number;
  fetched?: number;
  inserted?: number;
  trader?: LeaderboardAlphaTrader;
  snapshot?: LeaderboardAlphaSnapshot | null;
  results?: Array<{
    trader_id: number;
    address: string;
    status?: string;
    reason?: string | null;
    fetched: number;
    inserted: number;
    snapshot: LeaderboardAlphaSnapshot | null;
  }>;
};

export type LeaderboardAlphaImportResult = {
  status: string;
  source_rows: number;
  selected: number;
  inserted: number;
  updated: number;
  window: string;
  min_volume_usd: string | null;
};

export type LeaderboardCopyPaperRunResult = {
  status: string;
  reason?: string | null;
  eligible_traders?: number;
  fills_seen?: number;
  batch_size?: number;
  seeded_cursor?: boolean;
  opened?: number;
  closed?: number;
  skipped?: number;
  summary?: LeaderboardCopyPaper;
};

export type BotFunnelUser = {
  telegram_username: string | null;
  first_name: string | null;
  language: string;
  trade_capital: string | null;
  source: string | null;
  onboarding_stage: string;
  paused: boolean;
  created_at: string | null;
  risk_ack_at: string | null;
  joined_channel_at: string | null;
};

export type BotFunnelStats = {
  total: number;
  completed: number;
  declined: number;
  paused: number;
  completion_rate: number;
  completed_last_24h: number;
  completed_last_7d: number;
  by_stage: Record<string, number>;
  by_source: Record<string, number>;
  by_language: Record<string, number>;
  by_capital: Record<string, number>;
  source_funnel: Record<string, { started: number; completed: number }>;
  recent: BotFunnelUser[];
};

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    signal: AbortSignal.timeout(API_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new ApiError(path, response.status, await readErrorDetail(response));
  }
  return response.json() as Promise<T>;
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(API_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new ApiError(path, response.status, await readErrorDetail(response));
  }
  return response.json() as Promise<T>;
}

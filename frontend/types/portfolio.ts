/**
 * Contract of the read-only /api/portfolio/* API (backend/app/api/portfolio.py). Money is computed by the backend only;
 * every figure here is displayed, never derived. null always means "unknown", never zero.
 */
export type Dict = Record<string, unknown>;

export type ChargeBreakdown = { total: number; orders: number | null; turnover: number | null; items: Record<string, number> };
export type PositionCharges = { incurred: ChargeBreakdown | null; entry: ChargeBreakdown | null; exit: ChargeBreakdown | null };
export type Mark = {
  basis: string; price: number | null; at: string | null; age_seconds: number | null; bid: number | null; ask: number | null;
  reason: string | null; last_known_price: number | null;
};
export type Level = { basis: string | null; level: number | null; rule: string | null };
export type Leg = {
  role: string; side_entry: string | null; option_type: string | null; strike: number | null; symbol: string | null;
  quantity: number | null; entry_price: number | null; exit_price: number | null;
  bid: number | null; ask: number | null; bid_qty: number | null; ask_qty: number | null;
};
export type Journal = { table: string; id: string; first_sequence: number | null; last_sequence: number | null; events: number | null };
export type Provenance = { source: string; pnl_basis: string; charges_status: Record<string, string> };

export type Position = {
  position_id: string; trade_id: string; source: string; engine_id: string; strategy_id: string; strategy_label: string;
  strategy_version: string; family: string; instrument: string; structure: string; contract: string; expiry: string | null;
  direction: string | null; mode: string; status: string; flags: string[]; lots: number | null; lot_size: number | null;
  quantity: number | null; multiplier: number | null; decided_at: string | null; entry_at: string | null; exit_at: string | null;
  entry_price: number | null; entry_price_basis: string | null; exit_price: number | null; exit_price_basis: string | null;
  mark: Mark; spread_width: number | null; defined_max_loss: number | null; stop: Level | null; trailing_stop: Level | null;
  target: Level | null; unrealized_gross: number | null; est_exit_charges: number | null; est_net_liquidation: number | null;
  realized_gross: number | null; realized_gross_engine: number | null; charges: PositionCharges; realized_net: number | null;
  holding_seconds: number | null; entry_reason: string | null; exit_reason: string | null; score: number | null;
  score_band: string | null; score_components: Record<string, number> | null; cycle_id: string | null;
  rearm_reason: string | null; rejection_reason: string | null; unresolved_reason: string | null;
  mae: number | null; mfe: number | null; legs: Leg[]; journal: Journal | null; provenance: Provenance | null;
};
export type PositionTabCounts = { open: number; unresolved: number; closed: number };
export type PositionsPage = { tab: string; tab_counts: PositionTabCounts; total: number; limit: number; offset: number; items: Position[] };

export type Order = {
  order_id: string; broker_order_id: string | null; simulated: boolean; mode: string; position_id: string; trade_id: string | null;
  engine_id: string; strategy_id: string | null; strategy_version: string | null; strategy_label: string; instrument: string;
  contract: string | null; side: string; intent: string; order_type: string; requested_quantity: number | null;
  filled_quantity: number | null; remaining_quantity: number | null; requested_price: number | null;
  avg_fill_price: number | null; trigger_price: number | null; submitted_at: string | null; last_update_at: string | null;
  status: string; rejection_reason: string | null;
};
export type OrdersPage = { status_counts: Record<string, number>; note: string | null; total: number; limit: number; offset: number; items: Order[] };
export type Fill = {
  fill_id: string; order_id: string; position_id: string; engine_id: string; strategy_label: string; instrument: string;
  contract: string | null; side: string; intent: string; quantity: number | null; price: number | null; at: string | null;
  simulated: boolean; mode: string;
};
export type FillsPage = { total: number; limit: number; offset: number; items: Fill[] };

export type Trade = {
  trade_id: string; position_id: string; engine: string; strategy: string; version: string; instrument: string;
  contract: string | null; direction: string | null; mode: string; entry_at: string | null; entry_price: number | null;
  exit_at: string | null; exit_price: number | null; holding_seconds: number | null; lots: number | null;
  quantity: number | null; gross_pnl: number | null; charges: number | null; net_pnl: number | null;
  entry_reason: string | null; exit_reason: string | null; entry_score: number | null; cycle_id: string | null;
  market_regime: string | null; return_on_defined_risk_pct: number | null;
};
export type TradesPage = {
  totals: { trades: number; gross_pnl: number | null; charges: number | null; net_pnl: number | null };
  total: number; limit: number; offset: number; items: Trade[];
};

export type Pnl = {
  realized_gross: number | null; realized_net: number | null; unrealized_gross: number | null; incurred_charges: number | null;
  incurred_charges_detail: ChargeBreakdown | null; estimated_exit_charges: number | null; total_net_pnl: number | null;
  net_liquidation_pnl: number | null; partial: boolean; excluded_open_positions: number; excluded_unpriced_closed: number; excluded_unpriced_exposure?: number; charges_on_closed_trades?: number; charges_not_in_closed_trades?: number; incurred_charges_scope?: string;
  note: string | null;
};
export type AttentionItem = { position_id: string; engine_id: string; label: string; contract: string; flags: string[] };
export type Summary = {
  as_of: string; window: { from: string | null; to: string | null; timezone: string }; mode: string; currency: string; pnl: Pnl;
  counts: {
    open_positions: number; unresolved_positions: number; executed_trades: number; closed_trades: number;
    winning_trades: number; pending_orders: number; rejected_orders: number; orders_in_window: number;
  };
  charge_model: { brokerage_per_order: number | null; status: Record<string, string> };
  attention: AttentionItem[];
};

export type PerfStats = {
  trades: number; wins: number | null; losses: number | null; win_rate: number | null; gross_pnl: number | null;
  net_pnl: number | null; charges: number | null; gross_profit: number | null; gross_loss: number | null;
  expectancy_net: number | null; avg_win: number | null; avg_loss: number | null; profit_factor: number | null;
  largest_win: number | null; largest_loss: number | null; max_drawdown: number | null; current_drawdown: number | null;
  max_consecutive_wins: number | null; max_consecutive_losses: number | null; avg_holding_seconds: number | null;
  first_exit: string | null; last_exit: string | null; avg_defined_risk: number | null; net_to_max_drawdown: number | null;
};
export type StrategyPerf = PerfStats & { engine_id: string; label: string; instrument: string };
export type DirectionPerf = PerfStats & { direction: string };
export type Bucket = { bucket: string; trades: number; net_pnl: number | null; win_rate: number | null };
export type Performance = {
  as_of: string; window: { from: string | null; to: string | null }; mode: string; overall: PerfStats;
  excluded_trades_without_exit_price: number;
  daily: Array<{ date: string; trades: number; gross: number | null; net: number | null; cum_net: number | null }>;
  monthly: Array<{ month: string; net_pnl: number | null }>;
  by_strategy: StrategyPerf[]; by_direction: DirectionPerf[]; exit_reason_contribution: Bucket[];
  entry_score_vs_outcome: Bucket[]; time_of_day: Bucket[]; holding_duration: Bucket[];
  excursions: { trades_with_data: number; avg_mae: number | null; avg_mfe: number | null };
  cost_sensitivity_net_pnl: Record<string, number | null>;
  opportunity_rejections: Array<{ reason: string; count: number }>;
  unavailable: Record<string, string>; comparison_note: string | null;
};

export type RiskEngine = {
  engine_id: string; label: string; instrument: string; open_positions: number; open_position_risk: number | null;
  daily_realized_net: number | null; daily_realized_loss: number | null; daily_loss_limit: number | null;
  remaining_daily_loss_budget: number | null; max_risk_per_trade: number | null; entries_today: number | null;
  max_entries_per_day: number | null; limits_source: string | null; stop_coverage: { with_stop: number; open: number };
};
export type Risk = {
  as_of: string; mode: string; engines: RiskEngine[];
  exposure: {
    defined_risk_total: number | null; futures_long_notional: number | null; futures_short_notional: number | null;
    notional_note: string | null; margin_utilization: number | null; capital_allocation: number | null; capital_note: string | null;
  };
  concentration: Array<{ engine_id: string; share_of_open_risk: number | null }>;
  unresolved_exposure: Array<{ position_id: string; label: string; contract: string; flags: string[]; defined_max_loss: number | null }>;
  limitations: string[];
};

export type EquityPoint = {
  t: string; realized_gross: number | null; realized_net: number | null; unrealized_gross: number | null;
  combined_net: number | null; open_positions: number | null; partial: boolean;
};
export type EquityMarker = { t: string; type: string; engine_id: string | null; position_id: string | null; direction: string | null; price: number | null };
export type Equity = { date: string; available: boolean; points: EquityPoint[]; markers: EquityMarker[]; source: string | null; note: string | null };

export type StrategyToday = { trades: number; open_positions: number; realized_net: number | null; unrealized_gross: number | null; total_net: number | null; partial: boolean };
export type StrategyCard = {
  engine_id: string; label: string; family: string; instrument: string; version: string; mode: string; retired: boolean;
  operational_state: string; state_reason: string | null; last_update: string | null; today: StrategyToday; unresolved: number;
  dimensions: {
    enablement: Dict | null; session: Dict | null; feed: Dict | null; readiness: Dict | null; signal: Dict | null;
    position: Dict | null; risk: Dict | null; contract: Dict | null; calibration: string | null;
  };
  /** Untouched dimensions object, shown verbatim in Diagnostics. */
  dimensions_raw: unknown;
};
export type StrategyConfig = {
  available: boolean; source: string | null; note: string | null; policy_hash: string | null; from_trade: string | null;
  decided_at: string | null; config_id: string | null; persisted_config_hash: string | null; matches_running_worker: boolean | null;
  calibration_status: string | null; effective: Dict | null; parameters: Dict | null; exit_policy: Dict | null;
  entry_policy: Dict | null; opportunity_cycle_policy: Dict | null; error: string | null;
};
export type StrategiesList = { as_of: string; items: StrategyCard[] };
export type StrategyDetail = StrategyCard & { config: StrategyConfig; performance: Performance };

export type Alert = {
  engine_id: string | null; engine: string | null; position_id: string | null; severity: string; code: string;
  message: string; since: string | null; action: string | null;
};
export type Alerts = { as_of: string; critical: number; warning: number; items: Alert[] };
export type Header = {
  as_of: string; now_ist: string; timezone: string; nse: { status: string };
  mcx: { status: string; window: string | null; next_open: string | null };
  broker_connectivity: { state: string; basis: string };
  data_freshness: { fresh_feeds: number; stale_feeds: number; unavailable_feeds?: number; market_closed_feeds?: number; feeds: number; inactive_feeds?: number; stale: string[]; by_feed?: { id: string; label: string; state: string; classification: string; reason: string | null }[] };
  critical_alerts: number; warning_alerts: number; default_mode: string; live_execution: string;
};
export type SourceRow = { source: string; status: string; records: number; note: string | null };
export type Sources = {
  as_of: string; sources: SourceRow[]; charge_model: Record<string, string>; brokerage_per_order: number | null; definitions: string | null;
};

export type TimelineEvent = { sequence: number | null; type: string; at: string | null; reason: string | null; state: string | null };
export type PositionDetail = {
  position: Position; orders: Order[]; fills: Fill[]; timeline: TimelineEvent[];
  marks: Array<{ t: string; price: number | null; unrealized: number | null }>; mark_count: number;
  entry_evidence: { score: number | null; score_band: string | null; score_components: Record<string, number> | null; cycle_id: string | null; rearm_reason: string | null; entry_reason: string | null };
  pnl_calculation: {
    formula: string; quantity: number | null; realized_gross: number | null; engine_recorded_gross: number | null;
    charges: PositionCharges; realized_net: number | null; engine_recorded_net: number | null; charge_status: Record<string, string>;
  };
};

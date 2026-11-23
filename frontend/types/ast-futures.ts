/** Read-only AST_FUTURES_V1_DIRECT monitoring payloads. Null always means the backend does not know. */
export type AstBar = { start: string; end: string; open: number | null; high: number | null; low: number | null; close: number | null;
  valid: boolean; reason: string | null; future_instrument_id: string | null; future_expiry: string | null };
export type AstOpeningRange = { high: number; low: number; range_start: string; range_end: string; frozen_at?: string };
export type AstIndicator = { atr_period: number; multiplier: number; direction: "BULL" | "BEAR" | null; value: number | null;
  atr: number | null; transition: string | null };
export type AstFutures = { instrument_id: string | null; expiry: string | null; observed_lot_size: number | null; ltp: number | null;
  timing_ok: boolean; observation_blocker: string | null };
export type AstMark = { trade_id: string; at: string; price: number; points: number; mfe_points: number; mae_points: number };
export type AstTrade = {
  trade_id: string; trading_date: string; state: string; side: "LONG" | "SHORT" | null; direction?: string | null;
  future_instrument_id: string | null; future_expiry: string | null; lots?: number | null; lot_size: number | null; quantity?: number | null;
  signal_timestamp: string | null; signal_price?: number | null; decision_timestamp?: string | null;
  entry_timestamp: string | null; entry_price: number | null; exit_timestamp: string | null; exit_price: number | null;
  exit_reason: string | null; exit_category?: string | null; pending_exit_reason?: string | null;
  gross_points: number | null; gross_inr?: number | null; friction_points?: number | null; net_research_points: number | null;
  mfe_points?: number | null; mae_points?: number | null; rejection_reason: string | null; unresolved_reason: string | null;
  fill_basis?: string | null; atr_period?: number | null; multiplier?: number | null; opening_range?: AstOpeningRange | null;
  ast?: { direction: string | null; value: number | null; atr: number | null; transition: string | null } | null;
};
export type AstStatus = {
  strategy_id: string; strategy_version: string; mode: string; live_trading: boolean;
  worker: "RUNNING" | "STOPPED" | "UNKNOWN"; worker_enabled: boolean | null; heartbeat: Record<string, unknown> | null; heartbeat_age_seconds?: number;
  kill_switch: boolean; policy_hash: string | null;
  fixed_parameters: { atr_period: number; multiplier: number; regime: string; optimizer: string };
  policy_configuration: { history_sessions: number; lot_size: number; lots: number; opening_range_ist: string;
    session_exit_time_ist: string; friction_points_per_round_trip: number | null } | null;
  trading_date: string; in_session: boolean; futures_instrument: AstFutures | null;
  last_observation_at: string | null; last_completed_bar: AstBar | null; opening_range: AstOpeningRange | null; ast: AstIndicator | null;
  status: string | null; blockers: string[] | null; fill_blocker: string | null;
  position: AstTrade | null; position_detail: AstTrade | null; mark: AstMark | null; position_mark_gross_inr: number | null;
  trades_today: AstTrade[]; today_gross_points: number;
  cumulative_closed_trades: number; cumulative_gross_points: number; cumulative_net_research_points: number | null;
  forced_exit_status: string; journal_sequence: number; journal_head: string; journal_verification: string;
  fill_basis: string; fill_basis_note: string;
};
export type AstPage<T> = { items: T[]; total: number; limit: number; offset: number; has_more: boolean };
export type AstEvent = { sequence: number; event_type: string; timestamp: string | null; snapshot_id: number; trade_id: string | null;
  event_hash: string; previous_hash: string; summary: Record<string, unknown> };
export type AstEventPage = AstPage<AstEvent> & { scope: string; journal_sequence: number | null; journal_head: string | null };
export type AstPerformance = {
  label: "FORWARD_PAPER_PERFORMANCE"; basis: string; drawdown_basis: string; closed_trades: number; wins: number; losses: number; flat: number;
  win_rate: number | null; gross_points: number; gross_inr: number | null; net_research_points: number | null; average_points: number | null;
  best_trade: { trade_id: string; trading_date: string; side: string; gross_points: number } | null;
  worst_trade: { trade_id: string; trading_date: string; side: string; gross_points: number } | null;
  daily: Array<{ date: string; trades: number; points: number; cumulative_points: number }>;
  current_peak_points: number; current_drawdown_points: number; max_realized_drawdown_points: number; excluded_unresolved_exposures: number;
};

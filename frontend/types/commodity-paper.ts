import type { CommodityInstrument } from "./commodity-acts";
export type PaperKind = "positions" | "orders" | "fills" | "events";
export interface PaperCandidate {
  strategy_type?: "SHORT_PUT" | "SHORT_CALL" | "BULL_PUT_SPREAD" | "BEAR_CALL_SPREAD";
  contract_quantity?: number; stress_loss_estimate?: number; risk_reservation?: number; maximum_loss?: null; loss_is_bounded?: false;
  short_leg?: { trading_symbol?: string; strike?: number }; long_leg?: { trading_symbol?: string; strike?: number };
  spread_width?: number; executable_credit?: number; defined_max_loss_per_lot?: number;
  construction_evidence?: { margin?: { status?: string; required_margin?: number | null; premium_to_verified_margin_estimate?: number | null; standalone_margin?: number | null; spread_margin?: number | null; margin_saved_pct?: number | null; credit_to_blocked_margin?: number | null; credit_to_verified_margin_estimate?: number | null } };
}
export interface PaperDecision {
  status?: string; direction?: string | null; market_state?: string; opportunity_score?: number; score_band?: string;
  blockers?: string[] | null; candidate_rejections?: Record<string, number> | null; score_components?: Record<string, number> | null;
}
export interface PaperLatest {
  contract_renewal_status?: string;
  future_price?: number | null; future_symbol?: string; future_expiry?: string; option_expiry?: string; source_market_timestamp?: string | null;
  data_quality?: { contracts?: number; bid_ask_coverage?: number | null; depth_coverage?: number | null; source_timestamp_coverage?: number | null } | null;
  observed_at?: string; processed_at?: string; decision?: PaperDecision | null; candidate?: PaperCandidate | null;
  risk_blocks?: string[] | null;
}
export interface PaperInstrumentStatus {
  instrument_id: CommodityInstrument; paper_enabled?: boolean; status?: string; market_open?: boolean | null; worker_state?: string;
  observation_freshness?: string; observation_age_seconds?: number | null;
  latest?: PaperLatest | null; pending_entries?: number; open_positions?: number; pending_exits?: number; unresolved_exposure?: number;
  rejected_entries?: number; closed_trades?: number; wins?: number; losses?: number; sample_sessions?: number;
  trades_today?: number; realized_gross_today?: number | null; realized_net_today?: number | null;
  contract_renewal_status?: string;
  realized_gross?: number | null; realized_net?: number | null; unrealized_gross?: number | null; unrealized_net?: number | null;
  reserved_defined_risk?: number; reserved_stress_risk?: number; expiry_exit_overdue?: number; margin_status?: string;
  execution_evidence?: { depth_unit?: string; source?: string; lot_size?: number; rupees_per_point_per_lot?: number; valid_until?: string } | null;
}
export interface PaperSummary { version?: string; as_of?: string; broker_orders_enabled: false; instruments?: PaperInstrumentStatus[] | null }
export interface PaperRecord {
  id?: string; state?: string; kind?: string; sequence?: number; event_type?: string; reason?: string | null;
  observed_at?: string; processed_at?: string; decision_timestamp?: string; entry_timestamp?: string; exit_timestamp?: string;
  candidate?: PaperCandidate | null; direction?: string; entry_credit?: number | null; exit_debit?: number | null;
  gross_realized_pnl?: number | null; net_realized_pnl?: number | null; unrealized_gross?: number | null;
  unrealized_net?: number | null; defined_risk?: number; exit_reason?: string; rejection_reason?: string; unresolved_reason?: string | null;
  margin_status?: string; return_on_verified_margin?: number | null; latest?: PaperLatest | null;
  strategy?: string; contract_quantity?: number; risk_reservation?: number; stress_loss_estimate?: number; maximum_loss?: null; action?: string;
  execution?: { value?: number; atomic?: boolean; action?: string; short?: { price?: number }; long?: { price?: number } } | null;
  policy?: { cost_source?: string | null; cost_model?: string };
  last_attempt?: { reason?: string };
}
export interface PaperPage { status?: string; items?: PaperRecord[] | null; has_more?: boolean; limit?: number; offset?: number }

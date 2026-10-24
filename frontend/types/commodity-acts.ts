export type CommodityInstrument = "CRUDEOILM" | "NATGASMINI";
export interface CommodityDecision {
  instrument_id: CommodityInstrument; readiness: string; decision_kind?: string;
  stage?: "SCORE_TRANSFERABILITY" | "DIRECTIONAL_CONTEXT_RESEARCH"; research_scope?: string;
  observed_at?: string; decided_at?: string; snapshot_id?: number; session_id?: string;
  reference_price?: number | null; future_price?: number | null; reference_basis?: string;
  future_instrument_id?: string; future_expiry?: string; option_expiry?: string; lot_size?: number; atm?: number;
  maximum_score_without_execution?: number; unavailable_components?: string[]; blockers?: string[];
  data_provenance?: Record<string, string>;
  alpha: { alpha_1?: number | null; alpha_2?: number | null; alpha_1_direction?: string;
    alpha_2_direction?: string; validity_state?: string; status?: string; warnings?: string[];
    rank_observations_alpha1?: number; rank_observations_alpha2?: number };
  acts: { directional_score?: number; directional_score_max?: number; directional_components?: Record<string, number>; raw_context_points?: number; raw_context_max?: number; timestamp?: string; status?: string; market_state?: string; direction?: string | null; opportunity_score?: number;
    score_band?: string; score_components?: Record<string, number>; blockers?: string[] };
  geometry: { modal_strike_step?: number | null; complete_pairs?: number; contracts?: number;
    bid_ask_coverage?: number; depth_coverage?: number; oi_coverage?: number; volume_coverage?: number;
    median_bid_ask_spread_pct?: number | null; max_bid_ask_spread_pct?: number | null;
    strikes?: number[]; adjacent_gaps?: number[]; depth_unit?: string;
    credit_geometry_summary?: Array<{ observed_steps: string; comparisons: number; min_observed_credit: number; max_observed_credit: number }> };
}
export interface CommoditySummary {
  stage: "SCORE_TRANSFERABILITY" | "DIRECTIONAL_CONTEXT_RESEARCH"; research_scope: string;
  as_of: string; version: string; mode: "PAPER"; broker_orders_enabled: false; spread_execution_enabled: false; pnl: null;
  instruments: Array<{ instrument_id: CommodityInstrument; display_name: string; configured_enabled: boolean;
    session_configured: boolean; capture_state?: string; worker_state: string; observation_count: number; decision_count: number;
    readiness: string; latest: CommodityDecision | null }>;
}
export interface CommodityDecisionPage { instrument_id: CommodityInstrument; limit: number; offset: number; has_more: boolean; items: CommodityDecision[] }

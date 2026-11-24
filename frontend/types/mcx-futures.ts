import type { EngineState } from "@/types/engines";

/** Read-only MCX futures PAPER engine contracts. Null always means "unknown", never zero. */
export const MCX_INSTRUMENTS = ["CRUDEOILM", "NATGASMINI"] as const;
export type McxInstrumentId = typeof MCX_INSTRUMENTS[number];
export const isMcxInstrument = (value: string): value is McxInstrumentId => (MCX_INSTRUMENTS as readonly string[]).includes(value);

export type McxContract = {
  trading_symbol: string; token: string | null; expiry: string | null; lot_size: number | null;
  tick_size: number | null; quotation_unit: string | null; source: string | null;
};
export type McxSession = {
  status: "OPEN" | "CLOSED" | "PRE_OPEN"; window: string | null; next_open: string | null;
  last_entry_time: string | null; forced_exit_time: string | null;
};
export type McxFeed = {
  last_quote_at: string | null; age_seconds: number | null; stale: boolean;
  observed_at: string | null; source_timestamp_available: boolean | null;
  bid: number | null; ask: number | null; bid_qty: number | null; ask_qty: number | null;
  ltp: number | null; spread_ticks: number | null;
};
export type McxDecision = {
  at: string | null; status: string | null; direction: string | null; score: number | null;
  components: Record<string, number>; component_max: Record<string, number>; vetoes: string[]; market_state: string | null;
  cycle_id: string | null; cycle_state: string | null; rearm_reason: string | null;
};
export type McxPosition = {
  direction: string; entry_at: string | null; entry_price: number | null; stop_price: number | null;
  quantity_lots: number | null; mark_price: number | null; mark_at: string | null; unrealized_pnl: number | null;
};
export type McxRisk = {
  daily_loss_limit: number | null; daily_realized: number | null; max_risk_per_trade: number | null;
  entries_today: number | null; max_entries: number | null;
};
export type McxWarmup = { ready: boolean; bars_available: number; bars_required: number; timeframe_minutes: number; blocking: string[] };
export type McxInstrument = {
  instrument: McxInstrumentId; state: EngineState; state_reason: string | null; contract: McxContract | null;
  execution_state: string | null; execution_reason: string | null;
  open_position_count: number | null; unresolved_exposure_count: number | null;
  session: McxSession; feed: McxFeed; warmup: McxWarmup | null; last_decision: McxDecision | null; position: McxPosition | null;
  trades_today: number; realized_pnl_today: number; entries_rejected_today: number;
  last_rejection: { at: string | null; reason: string | null } | null; risk: McxRisk;
};
export type McxStatus = {
  generated_at: string; engine_version: string; mode: "PAPER"; calibration_status: string; instruments: McxInstrument[];
};
export type McxTrade = {
  trade_id: string; instrument: McxInstrumentId; contract: string | null; direction: string | null; version: string | null;
  mode: "PAPER"; quantity_lots: number | null; lot_size: number | null; entry_at: string | null; entry_price: number | null;
  exit_at: string | null; exit_price: number | null; exit_reason: string | null; gross_pnl: number | null; fees: number | null;
  slippage_cost: number | null; net_pnl: number | null; state: string; journal_sequence: number | null; rejection_reason: string | null;
};
export type McxTradePage = { items: McxTrade[]; next_before: string | null };
export type McxDecisionRecord = {
  at: string; instrument: McxInstrumentId; status: string | null; direction: string | null; score: number | null;
  components: Record<string, number>; component_max: Record<string, number>; vetoes: string[]; cycle_id: string | null; cycle_state: string | null;
  transition: string | null; evidence_at: string | null;
};
export type McxDecisionPage = { items: McxDecisionRecord[] };
export type McxPerf = {
  trades: number; win_rate: number | null; expectancy: number | null; avg_win: number | null; avg_loss: number | null;
  profit_factor: number | null; max_drawdown: number | null; gross: number | null; fees: number | null; net: number | null;
  realized: number | null; unrealized: number | null;
};
export type McxPerformance = { by_instrument: Record<string, McxPerf>; overall: McxPerf };

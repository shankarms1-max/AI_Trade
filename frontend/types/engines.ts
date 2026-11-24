/** GET /api/engines/status — read-only cross-engine overview. Null always means "unknown", never zero. */
export const ENGINE_FAMILIES = ["ACTS", "MCX_FUTURES", "AST", "PARALLEL", "RESEARCH"] as const;
export const ENGINE_MODES = ["PAPER", "RESEARCH", "LIVE"] as const;
export const ENGINE_STATES = ["RUNNING", "IDLE", "MARKET_CLOSED", "DATA_STALE", "DATA_UNAVAILABLE", "ERROR", "DISABLED", "NOT_CONFIGURED"] as const;
export type EngineFamily = typeof ENGINE_FAMILIES[number];
export type EngineMode = typeof ENGINE_MODES[number];
export type EngineState = typeof ENGINE_STATES[number];

export type EngineDecision = {
  at: string | null; status: string | null; direction: string | null; score: number | null; primary_blocker: string | null;
};
export type EnginePosition = {
  direction: string; contract: string | null; quantity: number | null; entry_price: number | null;
  mark_price: number | null; mark_at: string | null; unrealized_pnl: number | null;
};
export type Engine = {
  id: string; label: string; family: EngineFamily; version: string | null; instrument: string | null;
  mode: EngineMode | null; state: EngineState; state_reason: string | null; last_update: string | null;
  last_decision: EngineDecision | null; active_position: EnginePosition | null;
  trades_today: number | null; realized_pnl_today: number | null; unrealized_pnl: number | null; detail_href: string | null;
};
export type EngineStatus = { generated_at: string; timezone: "Asia/Kolkata"; engines: Engine[] };

import { API_BASE, fetchJson } from "@/lib/http";
import { array, boolean, finite, nFinite, nString, nTimestamp, nullable, oneOf, record, shape, string, timestamp, type Guard } from "@/lib/guards";
import { ENGINE_STATES } from "@/types/engines";
import { MCX_INSTRUMENTS, type McxContract, type McxDecision, type McxDecisionPage, type McxDecisionRecord, type McxFeed, type McxInstrument,
  type McxInstrumentId, type McxPerf, type McxPerformance, type McxPosition, type McxRisk, type McxSession, type McxStatus,
  type McxTrade, type McxTradePage } from "@/types/mcx-futures";

const instrument = oneOf(MCX_INSTRUMENTS);
const components = record(finite);
const vetoes = array(string);
const contract = shape<McxContract>({ trading_symbol: string, token: nString, expiry: nString, lot_size: nFinite, tick_size: nFinite,
  quotation_unit: nString, source: nString });
const session = shape<McxSession>({ status: oneOf(["OPEN", "CLOSED", "PRE_OPEN"] as const), window: nString, next_open: nTimestamp,
  last_entry_time: nString, forced_exit_time: nString });
const feed = shape<McxFeed>({ last_quote_at: nTimestamp, age_seconds: nFinite, stale: boolean, bid: nFinite, ask: nFinite, ltp: nFinite,
  spread_ticks: nFinite });
const decision = shape<McxDecision>({ at: nTimestamp, status: nString, direction: nString, score: nFinite, components, vetoes,
  market_state: nString, cycle_id: nString, cycle_state: nString, rearm_reason: nString });
const position = shape<McxPosition>({ direction: string, entry_at: nTimestamp, entry_price: nFinite, stop_price: nFinite,
  quantity_lots: nFinite, mark_price: nFinite, mark_at: nTimestamp, unrealized_pnl: nFinite });
const risk = shape<McxRisk>({ daily_loss_limit: nFinite, daily_realized: nFinite, max_risk_per_trade: nFinite, entries_today: nFinite,
  max_entries: nFinite });
const instrumentStatus = shape<McxInstrument>({ instrument, state: oneOf(ENGINE_STATES), state_reason: nString, contract: nullable(contract),
  session, feed, last_decision: nullable(decision), position: nullable(position), trades_today: finite, realized_pnl_today: finite,
  entries_rejected_today: finite, last_rejection: nullable(shape({ at: nTimestamp, reason: nString })), risk });

export const parseMcxStatus: Guard<McxStatus> = shape<McxStatus>({ generated_at: timestamp, engine_version: string,
  mode: oneOf(["PAPER"] as const), calibration_status: string, instruments: array(instrumentStatus) });
export const parseMcxTrades: Guard<McxTradePage> = shape<McxTradePage>({ next_before: nString, items: array(shape<McxTrade>({
  trade_id: string, instrument, contract: nString, direction: nString, version: nString, mode: oneOf(["PAPER"] as const),
  quantity_lots: nFinite, lot_size: nFinite, entry_at: nTimestamp, entry_price: nFinite, exit_at: nTimestamp, exit_price: nFinite,
  exit_reason: nString, gross_pnl: nFinite, fees: nFinite, slippage_cost: nFinite, net_pnl: nFinite, state: string,
  journal_sequence: nFinite, rejection_reason: nString })) });
export const parseMcxDecisions: Guard<McxDecisionPage> = shape<McxDecisionPage>({ items: array(shape<McxDecisionRecord>({
  at: timestamp, instrument, status: nString, direction: nString, score: nFinite, components, vetoes, cycle_id: nString,
  cycle_state: nString, transition: nString, evidence_at: nTimestamp })) });
const perf = shape<McxPerf>({ trades: finite, win_rate: nFinite, expectancy: nFinite, avg_win: nFinite, avg_loss: nFinite,
  profit_factor: nFinite, max_drawdown: nFinite, gross: nFinite, fees: nFinite, net: nFinite, realized: nFinite, unrealized: nFinite });
export const parseMcxPerformance: Guard<McxPerformance> = shape<McxPerformance>({ by_instrument: record(perf), overall: perf });

const query = (params: Record<string, string | number | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== "") search.set(key, String(value));
  const text = search.toString();
  return text ? `?${text}` : "";
};
const read = async <T,>(path: string, guard: Guard<T>, signal?: AbortSignal) => guard(await fetchJson(`${API_BASE}/api/mcx-futures/${path}`, { signal }), "");

/** Read-only PAPER engine client; there is deliberately no write method. */
export const mcxApi = {
  status: (signal?: AbortSignal) => read("status", parseMcxStatus, signal),
  trades: (instrument: McxInstrumentId, options: { limit?: number; before?: string } = {}, signal?: AbortSignal) =>
    read(`trades${query({ instrument, limit: options.limit ?? 50, before: options.before })}`, parseMcxTrades, signal),
  decisions: (instrument: McxInstrumentId, limit = 50, signal?: AbortSignal) =>
    read(`decisions${query({ instrument, limit })}`, parseMcxDecisions, signal),
  performance: (instrument?: McxInstrumentId, signal?: AbortSignal) =>
    read(`performance${query({ instrument })}`, parseMcxPerformance, signal),
};

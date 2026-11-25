import { array, boolean, finite, nFinite, nString, nTimestamp, nullable, object, record, shape, string, type Guard } from "@/lib/guards";
import type {
  Alerts, ChargeBreakdown, Dict, Equity, FillsPage, Header, Level, OrdersPage, Performance, PerfStats, PositionDetail,
  PositionsPage, Risk, Sources, StrategiesList, StrategyCard, StrategyConfig, StrategyDetail, Summary, TradesPage,
} from "@/types/portfolio";

/**
 * Runtime guards for /api/portfolio/*. Written against backend/tests/tools/dump_portfolio_fixture.py output and the live API:
 * every field the UI renders is checked; optional keys (absent on some engines) are accepted as null; unknown extra
 * keys are ignored. A violated contract rejects the payload (PayloadError) so the UI shows MALFORMED RESPONSE instead
 * of guessing a value.
 */
const dict: Guard<Dict | null> = nullable((value, path) => object(value, path));
const count: Guard<number> = finite;
const nBool = nullable(boolean);
const stringList = array(string);

const charges: Guard<ChargeBreakdown> = shape<ChargeBreakdown>({ total: finite, orders: nFinite, turnover: nFinite, items: record(finite) });
const positionCharges = shape({ incurred: nullable(charges), entry: nullable(charges), exit: nullable(charges) });
const mark = shape({ basis: string, price: nFinite, at: nTimestamp, age_seconds: nFinite, bid: nFinite, ask: nFinite, reason: nString, last_known_price: nFinite });
const level: Guard<Level> = shape<Level>({ basis: nString, level: nFinite, rule: nString });
const leg = shape({
  role: string, side_entry: nString, option_type: nString, strike: nFinite, symbol: nString, quantity: nFinite,
  entry_price: nFinite, exit_price: nFinite, bid: nFinite, ask: nFinite, bid_qty: nFinite, ask_qty: nFinite,
});
const journal = shape({ table: string, id: string, first_sequence: nFinite, last_sequence: nFinite, events: nFinite });
const provenance = shape({ source: string, pnl_basis: string, charges_status: record(string) });

export const positionGuard = shape({
  position_id: string, trade_id: string, source: string, engine_id: string, strategy_id: string, strategy_label: string,
  strategy_version: string, family: string, instrument: string, structure: string, contract: string, expiry: nString,
  direction: nString, mode: string, status: string, flags: stringList, lots: nFinite, lot_size: nFinite, quantity: nFinite,
  multiplier: nFinite, decided_at: nTimestamp, entry_at: nTimestamp, exit_at: nTimestamp, entry_price: nFinite,
  entry_price_basis: nString, exit_price: nFinite, exit_price_basis: nString, mark, spread_width: nFinite,
  defined_max_loss: nFinite, stop: nullable(level), trailing_stop: nullable(level), target: nullable(level),
  unrealized_gross: nFinite, est_exit_charges: nFinite, est_net_liquidation: nFinite, realized_gross: nFinite,
  realized_gross_engine: nFinite, charges: positionCharges, realized_net: nFinite, holding_seconds: nFinite,
  entry_reason: nString, exit_reason: nString, score: nFinite, score_band: nString,
  score_components: nullable(record(finite)), cycle_id: nString, rearm_reason: nString, rejection_reason: nString,
  unresolved_reason: nString, mae: nFinite, mfe: nFinite, legs: array(leg), journal: nullable(journal), provenance: nullable(provenance),
});
export const parsePositions = shape({
  tab: string, tab_counts: shape({ open: count, unresolved: count, closed: count }), total: count, limit: count, offset: count,
  items: array(positionGuard),
}) as Guard<PositionsPage>;

export const orderGuard = shape({
  order_id: string, broker_order_id: nString, simulated: boolean, mode: string, position_id: string, trade_id: nString,
  engine_id: string, strategy_id: nString, strategy_version: nString, strategy_label: string, instrument: string,
  contract: nString, side: string, intent: string, order_type: string, requested_quantity: nFinite, filled_quantity: nFinite,
  remaining_quantity: nFinite, requested_price: nFinite, avg_fill_price: nFinite, trigger_price: nFinite,
  submitted_at: nTimestamp, last_update_at: nTimestamp, status: string, rejection_reason: nString,
});
export const parseOrders = shape({
  status_counts: record(finite), note: nString, total: count, limit: count, offset: count, items: array(orderGuard),
}) as Guard<OrdersPage>;
export const fillGuard = shape({
  fill_id: string, order_id: string, position_id: string, engine_id: string, strategy_label: string, instrument: string,
  contract: nString, side: string, intent: string, quantity: nFinite, price: nFinite, at: nTimestamp, simulated: boolean, mode: string,
});
export const parseFills = shape({ total: count, limit: count, offset: count, items: array(fillGuard) }) as Guard<FillsPage>;

export const tradeGuard = shape({
  trade_id: string, position_id: string, engine: string, strategy: string, version: string, instrument: string,
  contract: nString, direction: nString, mode: string, entry_at: nTimestamp, entry_price: nFinite, exit_at: nTimestamp,
  exit_price: nFinite, holding_seconds: nFinite, lots: nFinite, quantity: nFinite, gross_pnl: nFinite, charges: nFinite,
  net_pnl: nFinite, entry_reason: nString, exit_reason: nString, entry_score: nFinite, cycle_id: nString, market_regime: nString,
  return_on_defined_risk_pct: nFinite,
});
export const parseTrades = shape({
  totals: shape({ trades: count, gross_pnl: nFinite, charges: nFinite, net_pnl: nFinite }), total: count, limit: count,
  offset: count, items: array(tradeGuard),
}) as Guard<TradesPage>;

const pnl = shape({
  realized_gross: nFinite, realized_net: nFinite, unrealized_gross: nFinite, incurred_charges: nFinite,
  incurred_charges_detail: nullable(charges), estimated_exit_charges: nFinite, total_net_pnl: nFinite, net_liquidation_pnl: nFinite,
  partial: boolean, excluded_open_positions: count, excluded_unpriced_closed: count, excluded_unpriced_exposure: nullable(count), note: nString,
});
export const parseSummary = shape({
  as_of: string, window: shape({ from: nString, to: nString, timezone: string }), mode: string, currency: string, pnl,
  counts: shape({
    open_positions: count, unresolved_positions: count, executed_trades: count, closed_trades: count, winning_trades: count,
    pending_orders: count, rejected_orders: count, orders_in_window: count,
  }),
  charge_model: shape({ brokerage_per_order: nFinite, status: record(string) }),
  attention: array(shape({ position_id: string, engine_id: string, label: string, contract: string, flags: stringList })),
}) as Guard<Summary>;

const perfFields = {
  trades: count, wins: nFinite, losses: nFinite, win_rate: nFinite, gross_pnl: nFinite, net_pnl: nFinite, charges: nFinite,
  gross_profit: nFinite, gross_loss: nFinite, expectancy_net: nFinite, avg_win: nFinite, avg_loss: nFinite, profit_factor: nFinite,
  largest_win: nFinite, largest_loss: nFinite, max_drawdown: nFinite, current_drawdown: nFinite, max_consecutive_wins: nFinite,
  max_consecutive_losses: nFinite, avg_holding_seconds: nFinite, first_exit: nTimestamp, last_exit: nTimestamp,
  avg_defined_risk: nFinite, net_to_max_drawdown: nFinite,
};
const perfStats = shape<PerfStats>(perfFields);
const bucket = shape({ bucket: string, trades: count, net_pnl: nFinite, win_rate: nFinite });
export const parsePerformance = shape({
  as_of: string, window: shape({ from: nString, to: nString }), mode: string, overall: perfStats,
  excluded_trades_without_exit_price: count,
  daily: array(shape({ date: string, trades: count, gross: nFinite, net: nFinite, cum_net: nFinite })),
  monthly: array(shape({ month: string, net_pnl: nFinite })),
  by_strategy: array(shape({ ...perfFields, engine_id: string, label: string, instrument: string })),
  by_direction: array(shape({ ...perfFields, direction: string })),
  exit_reason_contribution: array(bucket), entry_score_vs_outcome: array(bucket), time_of_day: array(bucket),
  holding_duration: array(bucket),
  excursions: shape({ trades_with_data: count, avg_mae: nFinite, avg_mfe: nFinite }),
  cost_sensitivity_net_pnl: record(nFinite), opportunity_rejections: array(shape({ reason: string, count })),
  unavailable: record(string), comparison_note: nString,
}) as Guard<Performance>;

const riskEngine = shape({
  engine_id: string, label: string, instrument: string, open_positions: count, open_position_risk: nFinite,
  daily_realized_net: nFinite, daily_realized_loss: nFinite, daily_loss_limit: nFinite, remaining_daily_loss_budget: nFinite,
  max_risk_per_trade: nFinite, entries_today: nFinite, max_entries_per_day: nFinite, limits_source: nString,
  stop_coverage: shape({ with_stop: count, open: count }),
});
export const parseRisk = shape({
  as_of: string, mode: string, engines: array(riskEngine),
  exposure: shape({
    defined_risk_total: nFinite, futures_long_notional: nFinite, futures_short_notional: nFinite, notional_note: nString,
    margin_utilization: nFinite, capital_allocation: nFinite, capital_note: nString,
  }),
  concentration: array(shape({ engine_id: string, share_of_open_risk: nFinite })),
  unresolved_exposure: array(shape({ position_id: string, label: string, contract: string, flags: stringList, defined_max_loss: nFinite })),
  limitations: stringList,
}) as Guard<Risk>;

export const parseEquity = shape({
  date: string, available: boolean,
  points: array(shape({ t: string, realized_gross: nFinite, realized_net: nFinite, unrealized_gross: nFinite, combined_net: nFinite, open_positions: nFinite, partial: boolean })),
  markers: array(shape({ t: string, type: string, engine_id: nString, position_id: nString, direction: nString, price: nFinite })),
  source: nString, note: nString,
}) as Guard<Equity>;

const cardShape = shape({
  engine_id: string, label: string, family: string, instrument: string, version: string, mode: string, retired: boolean,
  operational_state: string, state_reason: nString, last_update: nTimestamp,
  today: shape({ trades: count, open_positions: count, realized_net: nFinite, unrealized_gross: nFinite, total_net: nFinite, partial: boolean }),
  unresolved: count,
  dimensions: shape({
    enablement: dict, session: dict, feed: dict, readiness: dict, signal: dict, position: dict, risk: dict, contract: dict,
    calibration: nString,
  }),
});
export const cardGuard: Guard<StrategyCard> = (value, path) => {
  const card = cardShape(value, path) as Omit<StrategyCard, "dimensions_raw">;
  return { ...card, dimensions_raw: (value as Dict).dimensions };
};
export const parseStrategies = shape({ as_of: string, items: array(cardGuard) }) as Guard<StrategiesList>;
const configGuard = shape({
  available: boolean, source: nString, note: nString, policy_hash: nString, from_trade: nString, decided_at: nString,
  config_id: nString, persisted_config_hash: nString, matches_running_worker: nBool, calibration_status: nString,
  effective: dict, parameters: dict, exit_policy: dict, entry_policy: dict, opportunity_cycle_policy: dict, error: nString,
}) as Guard<StrategyConfig>;
export const parseStrategyDetail: Guard<StrategyDetail> = (value, path) =>
  ({ ...cardGuard(value, path), config: configGuard((value as Dict).config, `${path}.config`), performance: parsePerformance((value as Dict).performance, `${path}.performance`) });

export const parseAlerts = shape({
  as_of: string, critical: count, warning: count,
  items: array(shape({ engine_id: nString, engine: nString, position_id: nString, severity: string, code: string, message: string, since: nString, action: nString })),
}) as Guard<Alerts>;
export const parseHeader = shape({
  as_of: string, now_ist: string, timezone: string, nse: shape({ status: string }),
  mcx: shape({ status: string, window: nString, next_open: nString }),
  broker_connectivity: shape({ state: string, basis: string }),
  data_freshness: shape({ fresh_feeds: count, stale_feeds: count, unavailable_feeds: nullable(count), market_closed_feeds: nullable(count), feeds: count, inactive_feeds: nullable(count), stale: stringList }),
  critical_alerts: count, warning_alerts: count, default_mode: string, live_execution: string,
}) as Guard<Header>;
export const parseSources = shape({
  as_of: string, sources: array(shape({ source: string, status: string, records: count, note: nString })),
  charge_model: record(string), brokerage_per_order: nFinite, definitions: nString,
}) as Guard<Sources>;

export const parsePositionDetail = shape({
  position: positionGuard, orders: array(orderGuard), fills: array(fillGuard),
  timeline: array(shape({ sequence: nFinite, type: string, at: nTimestamp, reason: nString, state: nString })),
  marks: array(shape({ t: string, price: nFinite, unrealized: nFinite })), mark_count: count,
  entry_evidence: shape({ score: nFinite, score_band: nString, score_components: nullable(record(finite)), cycle_id: nString, rearm_reason: nString, entry_reason: nString }),
  pnl_calculation: shape({
    formula: string, quantity: nFinite, realized_gross: nFinite, engine_recorded_gross: nFinite, charges: positionCharges,
    realized_net: nFinite, engine_recorded_net: nFinite, charge_status: record(string),
  }),
}) as Guard<PositionDetail>;

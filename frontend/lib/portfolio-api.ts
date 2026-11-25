import { API_BASE } from "@/lib/http";
import { queryString, type ParamValue } from "@/lib/filters";
import {
  parseAlerts, parseEquity, parseFills, parseHeader, parseOrders, parsePerformance, parsePositionDetail, parsePositions,
  parseRisk, parseSources, parseStrategies, parseStrategyDetail, parseSummary, parseTrades,
} from "@/lib/portfolio-guards";

/** URL builders for the read-only portfolio API. The URL is the cache key of the shared poll store. */
export type Params = Record<string, ParamValue>;
export const portfolioUrl = (path: string, params: Params = {}) => `${API_BASE}/api/portfolio/${path}${queryString(params)}`;

export const urls = {
  header: () => portfolioUrl("header"),
  alerts: () => portfolioUrl("alerts"),
  sources: () => portfolioUrl("sources"),
  summary: (p: Params) => portfolioUrl("summary", p),
  positions: (p: Params) => portfolioUrl("positions", p),
  position: (id: string) => portfolioUrl(`positions/${encodeURIComponent(id)}`),
  orders: (p: Params) => portfolioUrl("orders", p),
  fills: (p: Params) => portfolioUrl("fills", p),
  trades: (p: Params) => portfolioUrl("trades", p),
  tradesCsv: (p: Params) => portfolioUrl("trades.csv", p),
  performance: (p: Params) => portfolioUrl("performance", p),
  risk: (p: Params) => portfolioUrl("risk", p),
  equity: (p: Params) => portfolioUrl("equity", p),
  strategies: () => portfolioUrl("strategies"),
  strategy: (id: string) => portfolioUrl(`strategies/${encodeURIComponent(id)}`),
  enginesStatus: () => `${API_BASE}/api/engines/status`,
};
const at = <T,>(guard: (value: unknown, path: string) => T) => (json: unknown) => guard(json, "");
/** Module-level (stable) root parsers for useResource. */
export const parse = {
  header: at(parseHeader), alerts: at(parseAlerts), sources: at(parseSources), summary: at(parseSummary),
  positions: at(parsePositions), position: at(parsePositionDetail), orders: at(parseOrders), fills: at(parseFills),
  trades: at(parseTrades), performance: at(parsePerformance), risk: at(parseRisk), equity: at(parseEquity),
  strategies: at(parseStrategies), strategy: at(parseStrategyDetail),
};

import { API_BASE, fetchJson } from "@/lib/http";
import { array, nFinite, nString, nTimestamp, nullable, oneOf, shape, string, timestamp, type Guard } from "@/lib/guards";
import { ENGINE_FAMILIES, ENGINE_MODES, ENGINE_STATES, type Engine, type EngineDecision, type EnginePosition, type EngineStatus } from "@/types/engines";

const decision: Guard<EngineDecision> = shape<EngineDecision>({
  at: nTimestamp, status: nString, direction: nString, score: nFinite, primary_blocker: nString,
});
const position: Guard<EnginePosition> = shape<EnginePosition>({
  direction: string, contract: nString, quantity: nFinite, entry_price: nFinite, mark_price: nFinite, mark_at: nTimestamp, unrealized_pnl: nFinite,
});
/** Internal links only: an engine may not point the dashboard at another origin. */
const internalHref: Guard<string | null> = (value, path) => {
  const href = nString(value, path);
  return href && /^\/(?!\/)[A-Za-z0-9/_\-?=&.#]*$/.test(href) ? href : null;
};
export const engineGuard: Guard<Engine> = shape<Engine>({
  id: string, label: string, family: oneOf(ENGINE_FAMILIES), version: nString, instrument: nString,
  mode: nullable(oneOf(ENGINE_MODES)), state: oneOf(ENGINE_STATES), state_reason: nString, last_update: nTimestamp,
  last_decision: nullable(decision), active_position: nullable(position),
  trades_today: nFinite, realized_pnl_today: nFinite, unrealized_pnl: nFinite, detail_href: internalHref,
});
export const parseEngineStatus: Guard<EngineStatus> = shape<EngineStatus>({
  generated_at: timestamp, timezone: oneOf(["Asia/Kolkata"] as const), engines: array(engineGuard),
});
export const enginesApi = {
  status: async (signal?: AbortSignal): Promise<EngineStatus> =>
    parseEngineStatus(await fetchJson(`${API_BASE}/api/engines/status`, { signal }), ""),
};

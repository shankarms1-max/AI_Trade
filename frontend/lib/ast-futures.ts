import type { AstEventPage, AstPerformance, AstStatus, AstTradePage } from "@/types/ast-futures";
import { fetchJson } from "@/lib/http";

/** Same-origin, server-side bridge only. The browser never learns database, API token or broker credentials. */
const BASE = "/monitor-data/ast-direct/api/ast-direct";
/** Kept for existing imports; failures are typed RequestErrors (503 SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED -> "not_configured"). */
export { RequestError as AstApiError } from "@/lib/http";
const read = <T,>(path: string, signal?: AbortSignal): Promise<T> =>
  fetchJson(`${BASE}/${path}`, { signal, credentials: "same-origin" }) as Promise<T>;
export const astApi = {
  status: (signal?: AbortSignal) => read<AstStatus>("status", signal),
  trades: (date?: string, limit = 100, signal?: AbortSignal) => read<AstTradePage>(`trades?limit=${limit}${date ? `&date=${encodeURIComponent(date)}` : ""}`, signal),
  events: (limit = 25, signal?: AbortSignal) => read<AstEventPage>(`events?scope=lifecycle&limit=${limit}`, signal),
  performance: (signal?: AbortSignal) => read<AstPerformance>("performance", signal),
};

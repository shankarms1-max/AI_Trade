import type { AstEventPage, AstPage, AstPerformance, AstStatus, AstTrade } from "@/types/ast-futures";

/** Same-origin, server-side bridge only. The browser never learns database, API token or broker credentials. */
const BASE = "/monitor-data/ast-direct/api/ast-direct";
export class AstApiError extends Error { constructor(public status: number) { super(`AST read failed (${status})`); } }
async function read<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}/${path}`, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" });
  if (!response.ok) throw new AstApiError(response.status);
  return response.json() as Promise<T>;
}
export const astApi = {
  status: () => read<AstStatus>("status"),
  trades: (date?: string, limit = 100) => read<AstPage<AstTrade>>(`trades?limit=${limit}${date ? `&date=${encodeURIComponent(date)}` : ""}`),
  events: (limit = 25) => read<AstEventPage>(`events?scope=lifecycle&limit=${limit}`),
  performance: () => read<AstPerformance>("performance"),
};

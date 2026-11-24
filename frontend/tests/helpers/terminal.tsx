import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";
import { FiltersProvider } from "@/components/filters-context";
import { pollStore } from "@/lib/poll-store";
import fixture from "../fixtures/portfolio-synthetic.json";

export { nav, navigationMock, resetNav } from "./nav-mock";

export const fx = fixture as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

type Route = (url: URL) => unknown | Response | Promise<unknown | Response>;
/**
 * Stub global fetch with fixture payloads by pathname (suffix after /api/portfolio/ or /api/...).
 * Anything not routed returns 404. Records every requested URL.
 */
export function stubApi(routes: Record<string, Route | unknown>, calls: string[] = []) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    calls.push(url.pathname + url.search);
    const key = decodeURIComponent(url.pathname).replace(/^\/api\/portfolio\//, "").replace(/^\/api\//, "api/");
    const handler = routes[key];
    if (handler === undefined) return new Response(JSON.stringify({ detail: "NOT_ROUTED" }), { status: 404 });
    const value = typeof handler === "function" ? await (handler as Route)(url) : handler;
    if (value instanceof Response) return value;
    return new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}
/** Default fixture routes for every portfolio endpoint. */
export const fixtureRoutes = (): Record<string, unknown> => ({
  header: fx.header, alerts: fx.alerts, sources: fx.sources, summary: fx.summary, risk: fx.risk, equity: fx.equity,
  strategies: fx.strategies, performance: fx.performance, orders: fx.orders, fills: fx.fills, trades: fx.trades,
  positions: (url: URL) => { const tab = url.searchParams.get("tab") ?? "open"; return fx[`positions_${tab}`] ?? fx.positions_open; },
  "strategies/nifty-acts_v3": fx.strategy_nifty_v3, "strategies/mcx-crudeoilm": fx.strategy_crude,
  "positions/nifty-acts_v3:T4-00000000000000000000": fx.position_detail_expired_open,
  "positions/nifty-acts_v3:T1-00000000000000000000": fx.position_detail_closed_spread,
  "api/engines/status": { generated_at: "2026-11-24T09:30:00+00:00", timezone: "Asia/Kolkata", engines: [] },
});

export function renderWithApp(ui: ReactElement) { return render(<FiltersProvider>{ui}</FiltersProvider>); }
export const resetStore = () => pollStore.reset();

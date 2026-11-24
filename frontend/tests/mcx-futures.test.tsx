import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import McxFuturesPage from "@/app/mcx-futures/page";
import McxInstrumentPage, { dynamicParams, generateStaticParams } from "@/app/mcx-futures/[instrument]/page";
import fixtures from "./fixtures/engines-mcx.json";

/** SYNTHETIC fixture payloads only (tests/fixtures/engines-mcx.json). */
type Route = { status?: number; body: unknown };
let routes: Record<string, Route>;
let requested: string[];
beforeEach(() => {
  requested = [];
  routes = Object.fromEntries(Object.entries(fixtures.fixtures).filter(([key]) => key.startsWith("/api/mcx-futures/")).map(([key, body]) => [key, { body }]));
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const parsed = new URL(url, "http://localhost");
    requested.push(`${parsed.pathname}${parsed.search}`);
    expect(init?.method).toBeUndefined();
    const route = routes[parsed.pathname];
    if (!route) return new Response("{}", { status: 404, headers: { "content-type": "application/json" } });
    return new Response(JSON.stringify(route.body), { status: route.status ?? 200, headers: { "content-type": "application/json" } });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const notConfigured = { status: 503, body: { status: "UNAVAILABLE", code: "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED" } };

describe("/mcx-futures", () => {
  it("shows collector-only execution separately from live feed state", async () => {
    const status = fixtures.fixtures["/api/mcx-futures/status"];
    routes["/api/mcx-futures/status"] = { body: { ...status, instruments: [
      { ...status.instruments[0], execution_state: "COLLECT_ONLY",
        execution_reason: "MCX_FUTURES_TRADING_ENABLED_FALSE", open_position_count: 0,
        unresolved_exposure_count: 0 }, status.instruments[1]] } };
    render(<McxFuturesPage/>);
    const crude = await screen.findByRole("region", { name: "CRUDEOILM paper engine" });
    expect(within(crude).getByText("RUNNING")).toBeInTheDocument();
    expect(within(crude).getByText("COLLECT ONLY")).toBeInTheDocument();
    expect(within(crude).getByText(/Paper execution: MCX_FUTURES_TRADING_ENABLED_FALSE/)).toBeInTheDocument();
  });

  it("shows both instruments side by side, PAPER labelled, with the unvalidated calibration stated", async () => {
    render(<McxFuturesPage/>);
    const crude = await screen.findByRole("region", { name: "CRUDEOILM paper engine" });
    const gas = screen.getByRole("region", { name: "NATGASMINI paper engine" });
    expect(screen.getByText("PAPER ONLY · NO BROKER ORDERS")).toBeInTheDocument();
    expect(screen.getByText("Calibration: PRE_REGISTERED_UNVALIDATED")).toBeInTheDocument();
    expect(screen.getByLabelText("Calibration status")).toHaveTextContent("No historical validation exists yet");
    expect(within(crude).getByText("SYNTHCRUDEOILM26NOVFUT")).toBeInTheDocument();
    expect(within(crude).getByText("RUNNING")).toBeInTheDocument();
    expect(within(crude).getByText("5,412.00 / 5,414.00")).toBeInTheDocument();
    expect(within(crude).getByText("ENTER")).toBeInTheDocument();
    expect(within(crude).getByText("ARMED")).toBeInTheDocument();
    const bars = within(crude).getByRole("list", { name: "Score breakdown" });
    expect(within(bars).getAllByRole("listitem")).toHaveLength(4);
    expect(within(bars).getByText("-5.00 / 5.00")).toHaveClass("negative");
    expect(within(crude).getByText("Unrealized (MTM)").nextSibling).toHaveTextContent("₹150.00");
    expect(within(crude).getByText(/Marked 23 Oct 2026 · 11:59:57 IST/)).toBeInTheDocument();
    expect(within(crude).getByText("Realized today (closed)").nextSibling).toHaveTextContent("−₹230.00");
    expect(within(crude).getByRole("meter", { name: "Entries used" })).toHaveAttribute("aria-valuenow", "50");
    expect(within(crude).getByRole("meter", { name: "Daily loss limit used" })).toHaveAttribute("aria-valuenow", "12");
    expect(within(gas).getByText("DATA STALE")).toBeInTheDocument();
    expect(within(gas).getByText("Contract not resolved")).toBeInTheDocument();
    expect(within(gas).getByText("No decision recorded yet")).toBeInTheDocument();
    expect(within(gas).getByText("No open paper position")).toBeInTheDocument();
    expect(within(gas).getByText("Bid / Ask").nextSibling).toHaveTextContent("— / —");
    expect(within(crude).getByRole("link", { name: "CRUDEOILM" })).toHaveAttribute("href", "/mcx-futures/CRUDEOILM");
    expect(screen.queryByRole("button", { name: /buy|sell|enter|exit|kill|start|stop/i })).not.toBeInTheDocument();
  });

  it("renders the not-configured (503) state without fabricated values", async () => {
    routes["/api/mcx-futures/status"] = notConfigured;
    render(<McxFuturesPage/>);
    expect(await screen.findByText("MCX FUTURES NOT CONFIGURED")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /paper engine/ })).not.toBeInTheDocument();
    expect(screen.getByText("Calibration: —")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/₹/);
  });

  it("shows a typed HTTP error with retry when the API fails", async () => {
    routes["/api/mcx-futures/status"] = { status: 500, body: { detail: "INTERNAL" } };
    render(<McxFuturesPage/>);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("MCX FUTURES STATUS UNAVAILABLE");
    expect(alert).toHaveTextContent("HTTP 500");
    expect(within(alert).getByRole("button", { name: "Retry now" })).toBeInTheDocument();
  });

  it("reports an instrument missing from the payload instead of inventing it", async () => {
    const status = fixtures.fixtures["/api/mcx-futures/status"];
    routes["/api/mcx-futures/status"] = { body: { ...status, instruments: [status.instruments[0]] } };
    render(<McxFuturesPage/>);
    expect(await screen.findByText("NATGASMINI is not reported by the engine")).toBeInTheDocument();
  });
});

describe("/mcx-futures/[instrument]", () => {
  it("only serves CRUDEOILM and NATGASMINI; anything else is notFound()", async () => {
    expect(dynamicParams).toBe(false);
    expect(generateStaticParams()).toEqual([{ instrument: "CRUDEOILM" }, { instrument: "NATGASMINI" }]);
    for (const instrument of ["GOLDM", "crudeoilm", "NATGASMINI2"]) {
      const error = await McxInstrumentPage({ params: Promise.resolve({ instrument }) }).catch(e => e);
      expect(String((error as { digest?: string }).digest)).toContain("404");
    }
  });

  it("renders decision timeline grouped by IST date, the trade blotter and performance for the deep link", async () => {
    render(await McxInstrumentPage({ params: Promise.resolve({ instrument: "NATGASMINI" }) }));
    expect(screen.getByRole("heading", { name: "Natural Gas Mini" })).toBeInTheDocument();
    await screen.findByRole("region", { name: "NATGASMINI paper engine" });
    // 2026-10-22T18:45Z and 06:29Z on the 23rd are both 23 Oct in IST.
    expect(await screen.findByRole("heading", { name: "23 Oct 2026" })).toBeInTheDocument();
    expect(screen.getByText("FEED_STALE")).toBeInTheDocument();
    const blotter = await screen.findByRole("table", { name: "NATGASMINI paper trades" });
    // 04:00Z on the 23rd -> 23 Oct; 18:00Z on the 22nd is 23:30 IST -> still 22 Oct.
    expect(within(blotter).getByText(/23 Oct 2026 · 1 record$/)).toBeInTheDocument();
    expect(within(blotter).getByText(/22 Oct 2026 · 1 record$/)).toBeInTheDocument();
    const rejected = within(blotter).getByText("SYNTH-T-1").closest("tr") as HTMLElement;
    expect(within(rejected).getByText("SPREAD_TOO_WIDE")).toBeInTheDocument();
    expect(within(rejected).queryByText(/₹0\.00/)).not.toBeInTheDocument();
    const closed = within(blotter).getByText("SYNTH-T-2").closest("tr") as HTMLElement;
    expect(within(closed).getByText("−₹242.50")).toHaveClass("negative");
    expect(screen.getByText("All records loaded")).toBeInTheDocument();
    const perf = await screen.findByLabelText("NATGASMINI performance");
    expect(within(perf).getByText("Win rate").nextSibling).toHaveTextContent("0.0%");
    expect(within(perf).getByText("Profit factor").nextSibling).toHaveTextContent("—");
    expect(within(perf).getByText("Realized (closed)").nextSibling).toHaveTextContent("−₹242.50");
    expect(within(perf).getByText("Unrealized (open, marked)").nextSibling).toHaveTextContent("—");
    expect(requested).toEqual(expect.arrayContaining(["/api/mcx-futures/decisions?instrument=NATGASMINI&limit=50",
      "/api/mcx-futures/trades?instrument=NATGASMINI&limit=50", "/api/mcx-futures/performance?instrument=NATGASMINI"]));
  });

  it("shows empty states when arrays are empty", async () => {
    routes["/api/mcx-futures/decisions"] = { body: { items: [] } };
    routes["/api/mcx-futures/trades"] = { body: { items: [], next_before: null } };
    routes["/api/mcx-futures/performance"] = { body: { by_instrument: {}, overall: fixtures.fixtures["/api/mcx-futures/performance"].overall } };
    render(await McxInstrumentPage({ params: Promise.resolve({ instrument: "CRUDEOILM" }) }));
    expect(await screen.findByText("No decisions recorded yet")).toBeInTheDocument();
    expect(await screen.findByText("No paper trades recorded yet")).toBeInTheDocument();
    expect(await screen.findByText("No performance reported for CRUDEOILM")).toBeInTheDocument();
  });

  it("stops dependent sections when the engine is not configured", async () => {
    routes["/api/mcx-futures/status"] = notConfigured;
    render(await McxInstrumentPage({ params: Promise.resolve({ instrument: "CRUDEOILM" }) }));
    expect(await screen.findByText("MCX FUTURES NOT CONFIGURED")).toBeInTheDocument();
    expect(screen.queryByText("DECISION TIMELINE")).not.toBeInTheDocument();
    expect(screen.queryByText("PAPER TRADE BLOTTER")).not.toBeInTheDocument();
  });
});

describe("commodity futures state visibility", () => {
  it.each(["RUNNING", "MARKET_CLOSED", "DATA_STALE", "IDLE", "DISABLED", "NOT_CONFIGURED"] as const)("shows %s without guessing a trade", async state => {
    const source = fixtures.fixtures["/api/mcx-futures/status"];
    routes["/api/mcx-futures/status"] = { body: { ...source, instruments: [{ ...source.instruments[0], state, state_reason: state + "_REASON" }] } };
    render(<McxFuturesPage/>);
    const row = await screen.findByRole("region", { name: "CRUDEOILM paper engine" });
    expect(within(row).getByText(state.replaceAll("_", " "))).toBeInTheDocument();
    expect(within(row).getByText(state + "_REASON")).toBeInTheDocument();
  });
  it("shows warm-up counts from status", async () => {
    const source = fixtures.fixtures["/api/mcx-futures/status"];
    routes["/api/mcx-futures/status"] = { body: { ...source, instruments: [{ ...source.instruments[0], warmup: {
      ready: false, bars_available: 11, bars_required: 44, timeframe_minutes: 3, blocking: ["FEATURE_BARS_WARMING_UP"] } }] } };
    render(<McxFuturesPage/>);
    const row = await screen.findByRole("region", { name: "CRUDEOILM paper engine" });
    expect(within(row).getByText("11 / 44 completed 3-minute bars")).toBeInTheDocument();
    expect(within(row).getByText("WARMING UP")).toBeInTheDocument();
  });
});

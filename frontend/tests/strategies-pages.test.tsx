import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Strategies, StrategyDetail } from "@/components/pages/strategies";
import { fixtureRoutes, fx, nav, renderWithApp, resetNav, resetStore, stubApi } from "./helpers/terminal";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => { resetNav("/strategies"); resetStore(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const crudeWithMcxDims = () => {
  const d = structuredClone(fx.strategy_crude);
  d.operational_state = "RUNNING"; d.state_reason = null;
  d.dimensions.enablement = { paper_execution: "ENABLED", live_execution: "NONE (no live execution path exists)" };
  d.dimensions.session = { status: "OPEN", window: "09:00-23:55 IST", last_entry_time: "2026-11-24T23:25:00+05:30", forced_exit_time: "2026-11-24T23:45:00+05:30", next_open: null };
  d.dimensions.feed = { stale: false, last_quote_at: "2026-11-24T18:59:45+05:30", age_seconds: 4, bid: 5972, ask: 5974, ltp: 5973, spread_ticks: 2 };
  d.dimensions.readiness = { bars_available: 30, bars_required: 54, blocking: ["atr_percentile", "ema_slow"], ready: false, timeframe_minutes: 3 };
  d.dimensions.signal = { status: "NO_ENTRY", direction: "LONG", score: 52, components: { trend: 25, momentum: 10, structure: 0 }, component_max: { trend: 25, momentum: 20, structure: 20 }, vetoes: ["POSITION_OR_RESERVATION_ACTIVE"], cycle_id: "eb8a2695c82b4415", cycle_state: "CONSUMED", rearm_reason: "PULLBACK_RESUMPTION", unavailable_inputs: { execution: ["quote_age"], trend: [] }, warm: false, market_state: "LONG_TREND" };
  return d;
};

describe("Strategies list", () => {
  it("is a compact table with a row per strategy linking to /strategies/[engineId]", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Strategies/>);
    const table = await screen.findByRole("table", { name: "Strategy performance" });
    const links = within(table).getAllByRole("link").map(a => a.getAttribute("href"));
    expect(links).toEqual(expect.arrayContaining(["/strategies/nifty-acts_v3", "/strategies/mcx-crudeoilm"]));
    expect(within(table).getAllByRole("row")).toHaveLength(1 + fx.strategies.items.length);
  });
  it("applies the global strategy filter and shows an empty state when nothing matches", async () => {
    resetNav("/strategies", "instrument=ZZZ");
    stubApi(fixtureRoutes());
    renderWithApp(<Strategies/>);
    expect(await screen.findByText("No strategy matches the filters")).toBeInTheDocument();
  });
  it("error and disconnected states", async () => {
    stubApi({ ...fixtureRoutes(), strategies: () => new Response("{}", { status: 500 }) });
    renderWithApp(<Strategies/>);
    expect(await screen.findByText(/Strategies UNAVAILABLE/)).toBeInTheDocument();
    cleanup(); resetStore();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("x")));
    renderWithApp(<Strategies/>);
    expect(await screen.findByText(/DISCONNECTED · Strategies/)).toBeInTheDocument();
  });
});

describe("Strategy detail", () => {
  it("has the eight tabs", async () => {
    stubApi({ ...fixtureRoutes() });
    renderWithApp(<StrategyDetail engineId="nifty-acts_v3"/>);
    const tabs = await screen.findByRole("tablist", { name: "Strategy sections" });
    expect(within(tabs).getAllByRole("tab").map(t => t.textContent)).toEqual(["Overview", "Positions", "Orders", "Trades", "Signals", "Performance", "Configuration", "Diagnostics"]);
  });

  it("Overview keeps the six status dimensions separate, with the partial-labelled P&L and the latest blocker", async () => {
    stubApi({ ...fixtureRoutes(), "strategies/mcx-crudeoilm": crudeWithMcxDims() });
    renderWithApp(<StrategyDetail engineId="mcx-crudeoilm"/>);
    for (const h of ["1 · Enablement", "2 · Market session", "3 · Feed freshness", "4 · Feature readiness / warm-up", "5 · Signal state", "6 · Position state"]) {
      expect(await screen.findByRole("region", { name: h })).toBeInTheDocument();
    }
    const warm = screen.getByRole("region", { name: "4 · Feature readiness / warm-up" });
    expect(within(warm).getByText("WARMING UP")).toBeInTheDocument();
    expect(within(warm).getByRole("progressbar", { name: /Bars available versus required/ })).toHaveAttribute("aria-valuenow", "30");
    expect(within(warm).getByText("30 / 54 bars (3 min timeframe)")).toBeInTheDocument();
    expect(within(warm).getByText("atr percentile")).toBeInTheDocument(); // blocking feature
    expect(within(screen.getByRole("region", { name: "1 · Enablement" })).getByText("ENABLED")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "3 · Feed freshness" })).getByText("FRESH")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Latest blocker" })).toHaveTextContent(/POSITION OR RESERVATION ACTIVE/);
    expect(screen.getByRole("group", { name: "Remaining risk budget" })).toBeInTheDocument();
    const net = screen.getByRole("group", { name: "Today net P&L" });
    expect(net).toHaveTextContent("+₹237.79");
  });

  it("separates a disabled strategy from stale feed and insufficient history (different dimensions, different badges)", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<StrategyDetail engineId="mcx-crudeoilm"/>);
    const enable = await screen.findByRole("region", { name: "1 · Enablement" });
    expect(within(enable).getByText("DISABLED")).toBeInTheDocument();        // operational state
    expect(within(enable).getByText("UNKNOWN")).toBeInTheDocument();         // paper execution flag
    expect(within(screen.getByRole("region", { name: "3 · Feed freshness" })).getByText("STALE")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "4 · Feature readiness / warm-up" })).getByText(/No warm-up feature set is recorded/)).toBeInTheDocument();
  });

  it("Signals tab: score vs floor, components with maxima, vetoes, cycle and unavailable inputs", async () => {
    resetNav("/strategies/mcx-crudeoilm", "view=signals");
    const d = crudeWithMcxDims();
    stubApi({ ...fixtureRoutes(), "strategies/mcx-crudeoilm": d });
    renderWithApp(<StrategyDetail engineId="mcx-crudeoilm"/>);
    const score = await screen.findByRole("group", { name: "Latest score" });
    expect(score).toHaveTextContent("52.0");
    const floor = screen.getByRole("group", { name: "Entry floor" });
    expect(floor).toHaveTextContent("60");
    expect(floor).toHaveTextContent("entry_score_floor (deployed config)");
    const bar = screen.getByRole("progressbar", { name: "momentum 10 of 20" });
    expect(bar).toHaveAttribute("aria-valuemax", "20");
    expect(screen.getByText("POSITION OR RESERVATION ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("eb8a2695c82b4415")).toBeInTheDocument();
    expect(screen.getByText("PULLBACK RESUMPTION")).toBeInTheDocument();
    expect(screen.getByText(/quote_age/)).toBeInTheDocument();
  });

  it("Signals tab for NIFTY: floor comes from the stored policy; components without a maximum say so", async () => {
    resetNav("/strategies/nifty-acts_v3", "view=signals");
    const d = structuredClone(fx.strategy_nifty_v3);
    d.dimensions.signal.components = { momentum: 20, structure: 14 };
    stubApi({ ...fixtureRoutes(), "strategies/nifty-acts_v3": d });
    renderWithApp(<StrategyDetail engineId="nifty-acts_v3"/>);
    const floor = await screen.findByRole("group", { name: "Entry floor" });
    expect(floor).toHaveTextContent("80");
    expect(floor).toHaveTextContent("signal_min_score (stored policy)");
    expect(screen.getAllByText("no maximum recorded").length).toBe(2);
  });

  it("Configuration tab shows source, policy hash and the effective parameters; unrecorded values are dashes", async () => {
    resetNav("/strategies/nifty-acts_v3", "view=configuration");
    stubApi(fixtureRoutes());
    renderWithApp(<StrategyDetail engineId="nifty-acts_v3"/>);
    expect(await screen.findByText("synthetic-policy-hash")).toBeInTheDocument();
    expect(screen.getByText(/policy stored on the latest trade/)).toBeInTheDocument();
    expect(screen.getByText("T3-00000000000000000000")).toBeInTheDocument();
    const eff = screen.getByRole("region", { name: /Effective parameters/ });
    expect(within(eff).getByText("signal min score").nextElementSibling).toHaveTextContent("80");
    expect(within(eff).getByText("min credit").nextElementSibling).toHaveTextContent("—"); // null: not recorded, never 0
    expect(screen.getByText(/DIAGNOSTICS_ONLY/)).toBeInTheDocument(); // the config's own note, not an invented rule
  });

  it("MCX configuration shows the deployed config, hash match status and calibration", async () => {
    resetNav("/strategies/mcx-crudeoilm", "view=configuration");
    stubApi(fixtureRoutes());
    renderWithApp(<StrategyDetail engineId="mcx-crudeoilm"/>);
    expect(await screen.findByText("NO MATCH")).toBeInTheDocument();
    expect(screen.getByText("PRE REGISTERED UNVALIDATED")).toBeInTheDocument();
    expect(screen.getByText("none persisted")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Deployed parameters" })).toBeInTheDocument();
  });

  it("Performance tab uses the embedded performance block; Diagnostics keeps raw JSON collapsed", async () => {
    resetNav("/strategies/nifty-acts_v3", "view=performance");
    stubApi(fixtureRoutes());
    renderWithApp(<StrategyDetail engineId="nifty-acts_v3"/>);
    expect(await screen.findByRole("group", { name: "Cumulative net P&L" })).toHaveTextContent("−₹611.02");
    cleanup(); resetStore();
    resetNav("/strategies/nifty-acts_v3", "view=diagnostics");
    renderWithApp(<StrategyDetail engineId="nifty-acts_v3"/>);
    const summary = await screen.findByText("dimensions (raw)");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(summary.closest("details")!.querySelector("pre")!.textContent).toContain("OBSERVED_ACTIVE");
  });

  it("embeds the strategy's Positions / Orders / Trades pinned to the engine", async () => {
    resetNav("/strategies/nifty-acts_v3", "view=orders");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<StrategyDetail engineId="nifty-acts_v3"/>);
    await screen.findByRole("table", { name: "Orders" });
    expect(calls.filter(c => c.includes("/orders")).every(c => c.includes("engine=nifty-acts_v3"))).toBe(true);
  });

  it("changing tab writes ?view= and keeps the global filters", async () => {
    resetNav("/strategies/nifty-acts_v3", "engine=x&range=7d");
    stubApi(fixtureRoutes());
    renderWithApp(<StrategyDetail engineId="nifty-acts_v3"/>);
    fireEvent.click(await screen.findByRole("tab", { name: "Signals" }));
    expect(nav.replaces.at(-1)).toContain("view=signals");
    expect(nav.replaces.at(-1)).toContain("range=7d");
  });

  it("an unknown engine id is a 404 (notFound)", async () => {
    stubApi({ ...fixtureRoutes(), "strategies/bogus": () => new Response('{"detail":"STRATEGY_NOT_FOUND"}', { status: 404 }) });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const thrown: unknown[] = [];
    const { ErrorBoundaryProbe } = await import("./helpers/boundary");
    renderWithApp(<ErrorBoundaryProbe onError={e => thrown.push(e)}><StrategyDetail engineId="bogus"/></ErrorBoundaryProbe>);
    await screen.findByText("boundary caught");
    expect(String(thrown[0])).toMatch(/NEXT_NOT_FOUND/);
    errors.mockRestore();
  });
});

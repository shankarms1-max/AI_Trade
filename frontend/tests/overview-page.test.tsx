import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GlobalHeader } from "@/components/global-header";
import { FiltersProvider } from "@/components/filters-context";
import { pollStore } from "@/lib/poll-store";
import { urls } from "@/lib/portfolio-api";
import { Overview } from "@/components/pages/overview";
import { fixtureRoutes, fx, nav, renderWithApp, resetNav, resetStore, stubApi } from "./helpers/terminal";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => { resetNav("/"); resetStore(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const tile = (name: string) => screen.getByRole("group", { name });

describe("Overview", () => {
  it("answers 'what is my P&L / what is open / what is wrong' from the API's numbers", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    // P&L: primary tiles straight from /summary (the browser sums nothing)
    expect(await within(await screen.findByRole("group", { name: "Total net P&L" })).findByText("−₹146.62")).toBeInTheDocument();
    expect(within(tile("Realized net")).getByText("−₹108.96")).toBeInTheDocument();
    expect(within(tile("Realized gross")).getByText("−₹60.00")).toBeInTheDocument();
    expect(within(tile("Incurred charges")).getByText("₹56.62")).toBeInTheDocument();
    expect(within(tile("Est. exit charges")).getByText("₹8.32")).toBeInTheDocument(); // shown separately from incurred
    expect(within(tile("Executed trades")).getByText("6")).toBeInTheDocument();
    expect(within(tile("Rejected orders")).getByText("2")).toBeInTheDocument();
    expect(within(tile("Pending orders")).getByText("0")).toBeInTheDocument();
    // what is wrong: critical banner + open position row flagged
    const banner = (await screen.findAllByRole("alert")).find(a => /EXPIRED_POSITION_NOT_SETTLED/.test(a.textContent ?? ""))!;
    expect(banner).toHaveAttribute("data-tone", "crit");
    const table = await screen.findByRole("table", { name: "Open positions" });
    const flagged = within(table).getAllByRole("row").find(r => r.getAttribute("data-row-key") === "nifty-acts_v3:T4-00000000000000000000")!;
    expect(flagged).toHaveClass("is-critical");
    // unresolved/attention rows come first
    expect(within(table).getAllByRole("row")[1]).toBe(flagged);
  });

  it("labels partial totals next to the total (pnl.partial, excluded_open_positions, note)", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    const total = await screen.findByRole("group", { name: "Total net P&L" });
    expect(within(total).getByText(/PARTIAL · excludes 1 open position/)).toHaveClass("warn");
    const banner = screen.getByRole("status", { name: "Partial totals" });
    expect(banner).toHaveTextContent(fx.summary.pnl.note);
    expect(within(tile("Unrealized")).getByText(/1 open position excluded/)).toBeInTheDocument();
  });

  it("does not print an unrealized zero when nothing could be priced; shows a dash with the reason", async () => {
    const s = structuredClone(fx.summary);
    s.pnl.unrealized_gross = 0; s.pnl.excluded_open_positions = s.counts.open_positions;
    stubApi({ ...fixtureRoutes(), summary: s });
    renderWithApp(<Overview/>);
    const unrealized = await screen.findByRole("group", { name: "Unrealized" });
    expect(within(unrealized).getByText("—")).toBeInTheDocument();
    expect(within(unrealized).getByText("no executable marks")).toBeInTheDocument();
  });

  it("risk utilisation is UNAVAILABLE with the reason when no capital is configured; budget is per engine", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    const util = await screen.findByRole("group", { name: "Risk utilisation" });
    expect(within(util).getByText("Unavailable")).toBeInTheDocument();
    expect(within(util).getByText(/No paper capital is configured/)).toBeInTheDocument();
    const budget = screen.getByRole("group", { name: "Remaining daily risk budget" });
    expect(budget).toHaveTextContent(/per engine/);
  });

  it("draws the intraday chart only from real points, else shows the note", async () => {
    stubApi({ ...fixtureRoutes(), equity: { date: "2026-11-24", available: false, points: [], markers: [], source: "event-sourced", note: "No recorded marks, entries or exits for this date." } });
    renderWithApp(<Overview/>);
    expect(await screen.findByText("No recorded marks, entries or exits for this date.")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /Intraday P&L/ })).toBeNull();
    cleanup(); resetStore();
    stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    expect(await screen.findByRole("img", { name: /Intraday P&L/ })).toBeInTheDocument();
  });

  it("lists strategy performance with separate dimension badges and links to the strategy page", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    const table = await screen.findByRole("table", { name: "Strategy performance" });
    const row = within(table).getAllByRole("row").find(r => /NIFTY ACTS V3/.test(r.textContent ?? ""))!;
    expect(within(row).getByRole("link", { name: /NIFTY ACTS V3/ })).toHaveAttribute("href", "/strategies/nifty-acts_v3");
    for (const label of ["Execution", "Session", "Feed", "Warm-up", "Signal", "Position"]) expect(within(row).getByText(label)).toBeInTheDocument();
    expect(within(row).getByText("PARTIAL")).toBeInTheDocument();
    expect(within(row).getByText(/MAX OPEN POSITIONS REACHED/i)).toBeInTheDocument(); // latest blocker
    const disabled = within(table).getAllByRole("row").find(r => /CRUDEOILM/.test(r.textContent ?? ""))!;
    expect(within(disabled).getByText("DISABLED")).toBeInTheDocument();           // operational state
    expect(within(disabled).getByText("Execution").closest(".tm-badge")).toHaveTextContent("UNKNOWN"); // enablement is a different dimension from state
  });

  it("opens the order lifecycle drawer link from a recent order", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    const table = await screen.findByRole("table", { name: "Recent orders" });
    fireEvent.click(within(table).getAllByRole("button", { name: /Open order/ })[0]);
    expect(nav.pushes.at(-1)).toMatch(/order=.*&position=|position=.*&order=/);
  });

  it("shows loading, error with retry, and disconnected states", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithApp(<Overview/>);
    expect((await screen.findAllByRole("status")).length).toBeGreaterThan(0);
    cleanup(); resetStore();
    stubApi({ ...fixtureRoutes(), summary: () => new Response("{}", { status: 503 }) });
    renderWithApp(<Overview/>);
    expect(await screen.findByText(/Summary UNAVAILABLE/)).toBeInTheDocument();
    cleanup(); resetStore();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    renderWithApp(<Overview/>);
    expect((await screen.findAllByText(/DISCONNECTED · Summary/)).length).toBe(1);
  });

  it("LIVE mode is an explicit empty state, never PAPER numbers", async () => {
    resetNav("/", "mode=LIVE");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    expect(await screen.findByText(/LIVE: no live engines/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Total net P&L" })).toBeNull();
    expect(calls.filter(c => c.includes("/summary") || c.includes("/equity") || c.includes("/risk"))).toHaveLength(0);
  });

  it("requests summary with the IST session date and PAPER mode", async () => {
    resetNav("/", "range=7d&engine=nifty-acts_v3");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<Overview/>);
    await screen.findByRole("group", { name: "Total net P&L" });
    const q = new URL(calls.find(c => c.includes("/summary"))!, "http://x").searchParams;
    expect(q.get("mode")).toBe("PAPER"); expect(q.get("engine")).toBe("nifty-acts_v3");
    expect(q.get("from")).toMatch(/^\d{4}-\d{2}-\d{2}$/); expect(q.get("to")).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});

describe("Global header", () => {
  it("shows IST time, NSE/MCX sessions, inferred broker connectivity, freshness, critical count and PAPER mode", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<GlobalHeader/>);
    expect(screen.getByRole("timer", { name: /Asia\/Kolkata/ })).toHaveTextContent(/\d\d:\d\d:\d\d\s*IST/);
    expect(await screen.findByText("NSE")).toBeInTheDocument();
    expect(screen.getByText("MCX")).toBeInTheDocument();
    const broker = screen.getByText("Broker (inferred)").closest(".tm-badge")!;
    expect(broker).toHaveAttribute("title", expect.stringContaining("Inferred: inferred from fresh market observations"));
    expect(screen.getByText(/0 fresh · 0 stale/)).toBeInTheDocument();
    expect(await screen.findByText("1 critical")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Mode" })).toHaveValue("PAPER");
  });

  it("lists stale feeds by name", async () => {
    const h = structuredClone(fx.header);
    h.data_freshness = { fresh_feeds: 1, stale_feeds: 2, feeds: 3, stale: ["NIFTY ACTS V3", "ACTS V3 CRUDEOILM"] }; h.broker_connectivity.state = "DISCONNECTED_INFERRED";
    stubApi({ ...fixtureRoutes(), header: h });
    renderWithApp(<GlobalHeader/>);
    expect(await screen.findByText(/stale: NIFTY ACTS V3, ACTS V3 CRUDEOILM/)).toBeInTheDocument();
    expect(screen.getByText("Disconnected").closest(".tm-badge")).toHaveAttribute("data-tone", "neg");
  });

  it("counts unavailable feeds as degraded and shows PARTIAL connectivity when only some in-session feeds are fresh", async () => {
    const h = structuredClone(fx.header);
    h.data_freshness = { fresh_feeds: 1, stale_feeds: 0, unavailable_feeds: 2, market_closed_feeds: 0, feeds: 3, stale: ["ACTS V3 CRUDEOILM", "ACTS V3 NATGASMINI"] };
    h.broker_connectivity.state = "PARTIAL_INFERRED";
    stubApi({ ...fixtureRoutes(), header: h });
    renderWithApp(<GlobalHeader/>);
    expect(await screen.findByText(/1 fresh · 2 stale\/unavail\./)).toBeInTheDocument();
    expect(screen.getByText(/stale: ACTS V3 CRUDEOILM, ACTS V3 NATGASMINI/)).toBeInTheDocument();
    expect(screen.getByText("Partial").closest(".tm-badge")).toHaveAttribute("data-tone", "warn");
  });

  it("opens a compact alerts popover with code, engine, message, since and action", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<GlobalHeader/>);
    fireEvent.click(await screen.findByRole("button", { name: /critical/ }));
    const pop = screen.getByRole("dialog", { name: "Alerts" });
    expect(within(pop).getByText("EXPIRED_POSITION_NOT_SETTLED")).toBeInTheDocument();
    expect(within(pop).getByText("NIFTY ACTS V3")).toBeInTheDocument();
    expect(within(pop).getByText(/since 23 Nov 2026/)).toBeInTheDocument();
    expect(within(pop).getByText(/Action: Do not edit the journal/)).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Alerts" })).toBeNull();
  });

  it("selecting LIVE writes mode=LIVE to the URL; instrument filters are merged into the same query", async () => {
    stubApi(fixtureRoutes());
    const view = renderWithApp(<GlobalHeader/>);
    fireEvent.change(await screen.findByRole("combobox", { name: "Mode" }), { target: { value: "LIVE" } });
    expect(nav.replaces.at(-1)).toContain("mode=LIVE");
    view.rerender(<FiltersProvider><GlobalHeader/></FiltersProvider>);
    expect(await screen.findByText("NO LIVE ENGINES")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Instrument" }), { target: { value: "NIFTY" } });
    expect(nav.replaces.at(-1)).toContain("instrument=NIFTY");
    expect(nav.replaces.at(-1)).toContain("mode=LIVE");
  });

  it("date range selection is held in the URL query", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<GlobalHeader/>);
    fireEvent.click(await screen.findByRole("button", { name: "30D" }));
    expect(nav.replaces.at(-1)).toContain("range=30d");
  });

  it("keeps the last good header data and shows STALE when a refresh fails", async () => {
    let fail = false;
    stubApi({ ...fixtureRoutes(), header: () => fail ? new Response("{}", { status: 500 }) : fx.header });
    renderWithApp(<GlobalHeader/>);
    await screen.findByText("NSE");
    fail = true;
    await act(async () => { await pollStore.refresh(urls.header()); });
    expect(screen.getByText("NSE")).toBeInTheDocument();     // previous data stays on screen
    expect(await screen.findByText("Header")).toBeInTheDocument(); // STALE badge label
    expect(screen.getByText(/since \d\d:\d\d:\d\d IST/)).toBeInTheDocument();
  });
});

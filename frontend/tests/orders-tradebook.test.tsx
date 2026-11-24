import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Orders, ordersQuery } from "@/components/pages/orders";
import { Tradebook, tradeFilterParams, tradesCsvHref, tradesQuery } from "@/components/pages/tradebook";
import { parseFilters } from "@/lib/filters";
import { fixtureRoutes, fx, nav, renderWithApp, resetNav, resetStore, stubApi } from "./helpers/terminal";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => { resetNav("/orders"); resetStore(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Orders page", () => {
  it("lists simulated paper orders: broker id n/a (simulated), status and rejection reason", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Orders/>);
    const table = await screen.findByRole("table", { name: "Orders" });
    expect(within(table).getAllByText("n/a (simulated)").length).toBe(fx.orders.items.length);
    expect(within(table).getAllByText("simulated").length).toBeGreaterThan(0);
    for (const header of ["Order id", "Broker id", "Position", "Strategy", "Version", "Contract", "Side", "Intent", "Type", "Req. qty", "Filled", "Remaining", "Req. price", "Avg fill", "Trigger", "Submitted", "Last update", "Status", "Rejection reason"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const rejected = within(table).getAllByRole("row").find(r => /REJECTED/.test(r.textContent ?? ""))!;
    expect(rejected).toHaveClass("is-warn");
    expect(rejected).toHaveTextContent(/PENDING ACTS V2 DIRECTION REVERSED/);
  });

  it("shows status_counts chips and filters by status/intent through API params", async () => {
    resetNav("/orders", "status=REJECTED&intent=ENTRY&engine=nifty-acts_v3&offset=50");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<Orders/>);
    const chips = await screen.findByRole("group", { name: "Status counts" });
    expect(within(chips).getByRole("button", { name: /FILLED/ })).toHaveTextContent("19");
    expect(within(chips).getByRole("button", { name: /REJECTED/ })).toHaveAttribute("aria-pressed", "true");
    const list = new URL(calls.find(c => c.includes("status=REJECTED") && c.includes("limit=50"))!, "http://x").searchParams;
    expect(Object.fromEntries(list)).toMatchObject({ status: "REJECTED", intent: "ENTRY", engine: "nifty-acts_v3", offset: "50", limit: "50", mode: "PAPER" });
    // chips come from a request WITHOUT the status filter so they never collapse to the selected status
    const counts = calls.find(c => c.includes("limit=1") && c.includes("/orders"))!;
    expect(counts).not.toContain("status=");
    expect(ordersQuery(parseFilters(new URLSearchParams("")), null, null, 0)).toMatchObject({ status: null, intent: null, limit: 50, offset: 0 });
  });

  it("clicking a status chip writes status to the URL and resets pagination", async () => {
    resetNav("/orders", "offset=50");
    stubApi(fixtureRoutes());
    renderWithApp(<Orders/>);
    fireEvent.click(await screen.findByRole("button", { name: /EXPIRED/ }));
    expect(nav.replaces.at(-1)).toContain("status=EXPIRED");
    expect(nav.replaces.at(-1)).not.toContain("offset");
  });

  it("opens the lifecycle drawer from the order id and links to the position", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Orders/>);
    const table = await screen.findByRole("table", { name: "Orders" });
    fireEvent.click(within(table).getAllByRole("button", { name: /Open order/ })[0]);
    expect(nav.pushes.at(-1)).toContain("order=");
    fireEvent.click(within(table).getAllByRole("button", { name: /Open position/ })[0]);
    expect(nav.pushes.at(-1)).toContain("position=");
  });

  it("empty, error and disconnected states", async () => {
    stubApi({ ...fixtureRoutes(), orders: { status_counts: {}, note: null, total: 0, limit: 50, offset: 0, items: [] } });
    renderWithApp(<Orders/>);
    expect(await screen.findByText("No orders match these filters")).toBeInTheDocument();
    cleanup(); resetStore();
    stubApi({ ...fixtureRoutes(), orders: () => new Response("{}", { status: 500 }) });
    renderWithApp(<Orders/>);
    expect(await screen.findByText(/Orders UNAVAILABLE/)).toBeInTheDocument();
    cleanup(); resetStore();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("x")));
    renderWithApp(<Orders/>);
    expect(await screen.findByText(/DISCONNECTED · Orders/)).toBeInTheDocument();
  });
});

describe("Tradebook page", () => {
  beforeEach(() => resetNav("/tradebook"));

  it("shows the /trades columns, API totals row and unknown values as dashes", async () => {
    const t = structuredClone(fx.trades);
    t.items[0].exit_price = null; t.items[0].net_pnl = null; t.items[0].gross_pnl = null;
    stubApi({ ...fixtureRoutes(), trades: t });
    renderWithApp(<Tradebook/>);
    const table = await screen.findByRole("table", { name: "Trades" });
    for (const header of ["Trade", "Strategy", "Version", "Instrument", "Contract", "Dir", "Mode", "Entry time", "Entry", "Exit time", "Exit", "Held", "Lots", "Qty", "Gross", "Charges", "Net", "Entry reason", "Exit reason", "Entry score"]) {
      expect(within(table).getAllByRole("columnheader").some(h => h.textContent?.startsWith(header))).toBe(true);
    }
    const footer = table.querySelector("tfoot")!;
    expect(footer).toHaveTextContent("Totals · all 4 filtered trades");
    expect(footer).toHaveTextContent("−₹60.00"); // totals.gross_pnl straight from the API
    expect(footer).toHaveTextContent("−₹108.96");
    expect(within(table).getAllByText("—").length).toBeGreaterThan(0);
    expect(within(table).queryByText("₹0.00")).toBeNull();
  });

  it("sorting is an API parameter (sort/desc), not a local reorder", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Tradebook/>);
    fireEvent.click(await screen.findByRole("button", { name: "Sort by Net" }));
    expect(nav.replaces.at(-1)).toContain("sort=net_pnl");
    resetNav("/tradebook", "sort=net_pnl");
    expect(tradesQuery(parseFilters(new URLSearchParams("")), { result: null, exit_reason: null, direction: null }, "net_pnl", false, 0)).toMatchObject({ sort: "net_pnl", desc: false, limit: 50, offset: 0 });
  });

  it("filters are API params: date range, instrument, strategy, version, direction, result, exit reason, mode", async () => {
    resetNav("/tradebook", "range=custom&from=2026-11-01&to=2026-11-24&engine=mcx-crudeoilm&instrument=CRUDEOILM&version=ACTS_V3_MCX_FUTURES&direction=LONG&result=win&exit_reason=MAX_HOLDING_TIME&sort=entry_at&desc=false&offset=50");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<Tradebook/>);
    await screen.findByRole("table", { name: "Trades" });
    const q = Object.fromEntries(new URL(calls.find(c => c.includes("/trades") && !c.includes(".csv"))!, "http://x").searchParams);
    expect(q).toEqual({ mode: "PAPER", engine: "mcx-crudeoilm", instrument: "CRUDEOILM", version: "ACTS_V3_MCX_FUTURES", from: "2026-11-01", to: "2026-11-24", direction: "LONG", result: "win", exit_reason: "MAX_HOLDING_TIME", sort: "entry_at", desc: "false", limit: "50", offset: "50" });
  });

  it("the CSV export is a plain link to /api/portfolio/trades.csv with exactly the active filters", async () => {
    resetNav("/tradebook", "range=custom&from=2026-11-01&to=2026-11-24&engine=nifty-acts_v3&instrument=NIFTY&version=ACTS_V3&direction=BULL&result=loss&exit_reason=SPREAD_STOP&sort=net_pnl&offset=50");
    stubApi(fixtureRoutes());
    renderWithApp(<Tradebook/>);
    const link = await screen.findByRole("link", { name: "Export CSV" });
    const url = new URL(link.getAttribute("href")!, "http://x");
    expect(url.pathname).toBe("/api/portfolio/trades.csv");
    expect(link.tagName).toBe("A");
    const csv = Object.fromEntries(url.searchParams);
    const filters = parseFilters(new URLSearchParams(nav.search));
    const active = tradeFilterParams(filters, { result: "loss", exit_reason: "SPREAD_STOP", direction: "BULL" });
    // every active filter is in the link ...
    for (const [k, v] of Object.entries(active)) if (v !== null && v !== "") expect(csv[k]).toBe(String(v));
    // ... and nothing else (no sort / paging: the file is the whole filtered book)
    expect(Object.keys(csv).sort()).toEqual(Object.entries(active).filter(([, v]) => v !== null && v !== "").map(([k]) => k).sort());
    // the table request uses the same filter set
    const table = new URLSearchParams(tradesQuery(filters, { result: "loss", exit_reason: "SPREAD_STOP", direction: "BULL" }, "net_pnl", true, 50) as Record<string, string>);
    for (const [k, v] of Object.entries(csv)) expect(table.get(k)).toBe(v);
    expect(tradesCsvHref(filters, { result: null, exit_reason: null, direction: null })).toContain("/api/portfolio/trades.csv?");
  });

  it("drills down into the position drawer", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Tradebook/>);
    const table = await screen.findByRole("table", { name: "Trades" });
    fireEvent.click(within(table).getAllByRole("button", { name: /Open trade/ })[0]);
    expect(nav.pushes.at(-1)).toContain("position=");
  });

  it("empty, error and disconnected states; LIVE shows no live engines", async () => {
    stubApi({ ...fixtureRoutes(), trades: { totals: { trades: 0, gross_pnl: 0, charges: 0, net_pnl: 0 }, total: 0, limit: 50, offset: 0, items: [] } });
    renderWithApp(<Tradebook/>);
    expect(await screen.findByText("No closed trades match these filters")).toBeInTheDocument();
    cleanup(); resetStore();
    stubApi({ ...fixtureRoutes(), trades: () => new Response("{}", { status: 500 }) });
    renderWithApp(<Tradebook/>);
    expect(await screen.findByText(/Tradebook UNAVAILABLE/)).toBeInTheDocument();
    cleanup(); resetStore();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("x")));
    renderWithApp(<Tradebook/>);
    expect(await screen.findByText(/DISCONNECTED · Tradebook/)).toBeInTheDocument();
    cleanup(); resetStore();
    resetNav("/tradebook", "mode=LIVE");
    stubApi(fixtureRoutes());
    renderWithApp(<Tradebook/>);
    expect(await screen.findByText(/LIVE: no live engines/)).toBeInTheDocument();
  });
});

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Positions, positionsQuery } from "@/components/pages/positions";
import { parseFilters } from "@/lib/filters";
import { fixtureRoutes, fx, nav, renderWithApp, resetNav, resetStore, stubApi } from "./helpers/terminal";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => { resetNav("/positions"); resetStore(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Positions page", () => {
  it("renders the open positions and flags the expired-not-settled position as critical", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Positions/>);
    const table = await screen.findByRole("table", { name: "open positions" });
    const rows = within(table).getAllByRole("row");
    const expired = rows.find(r => r.getAttribute("data-row-key") === "nifty-acts_v3:T4-00000000000000000000")!;
    expect(expired).toHaveClass("is-critical");
    expect(within(expired).getByText(/EXPIRED NOT SETTLED/i)).toBeInTheDocument();
    expect(within(expired).getByText(/PAST FORCED EXIT/i)).toBeInTheDocument();
    // critical banner from /alerts is shown too
    expect((await screen.findAllByRole("alert")).some(a => /CRITICAL/.test(a.textContent ?? ""))).toBe(true);
    // unknown mark / unrealized renders a dash with its reason, never zero
    expect(within(expired).getAllByText(/EXPIRED CONTRACT NOT SETTLED/).length).toBeGreaterThan(0);
    expect(within(expired).queryByText("₹0.00")).toBeNull();
  });

  it("shows tab counts from tab_counts and a red count for unresolved", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Positions/>);
    const tabs = await screen.findByRole("tablist", { name: "Position tabs" });
    await waitFor(() => expect(within(tabs).getByRole("tab", { name: /Open/ })).toHaveTextContent("3"));
    expect(within(tabs).getByRole("tab", { name: /Closed/ })).toHaveTextContent("5");
    expect(within(tabs).getByRole("tab", { name: /Unresolved/ })).toHaveTextContent("2");
  });

  it("switching tab and filters goes to the API as query params (server pagination)", () => {
    const f = parseFilters(new URLSearchParams("engine=nifty-acts_v3&instrument=NIFTY&version=ACTS_V3&range=custom&from=2026-11-01&to=2026-11-24"));
    expect(positionsQuery(f, "closed", "BULL", 50)).toEqual({ mode: "PAPER", engine: "nifty-acts_v3", instrument: "NIFTY", version: "ACTS_V3", from: "2026-11-01", to: "2026-11-24", tab: "closed", direction: "BULL", limit: 50, offset: 50 });
  });

  it("requests exactly the URL filters, tab and offset", async () => {
    resetNav("/positions", "tab=closed&engine=nifty-acts_v3&direction=BULL&offset=50&range=7d");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<Positions/>);
    await screen.findByRole("table", { name: /closed positions/ });
    const list = calls.find(c => c.includes("tab=closed"))!;
    const q = new URL(list, "http://x").searchParams;
    expect(q.get("engine")).toBe("nifty-acts_v3");
    expect(q.get("direction")).toBe("BULL");
    expect(q.get("offset")).toBe("50");
    expect(q.get("limit")).toBe("50");
    expect(q.get("mode")).toBe("PAPER");
    expect(q.get("from")).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("changing the tab updates the URL and resets pagination", async () => {
    resetNav("/positions", "offset=50");
    stubApi(fixtureRoutes());
    renderWithApp(<Positions/>);
    fireEvent.click(await screen.findByRole("tab", { name: /Closed/ }));
    expect(nav.replaces.at(-1)).toContain("tab=closed");
    expect(nav.replaces.at(-1)).not.toContain("offset");
  });

  it("opens the detail drawer deep link on row click", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Positions/>);
    const table = await screen.findByRole("table", { name: "open positions" });
    fireEvent.click(within(table).getAllByRole("button", { name: /Open position/ })[0]);
    expect(nav.pushes.at(-1)).toMatch(/position=nifty-acts_v3%3AT4/);
  });

  it("expands multi-leg rows into SHORT / HEDGE legs", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Positions/>);
    const table = await screen.findByRole("table", { name: "open positions" });
    fireEvent.click(within(table).getAllByRole("button", { name: /Expand legs/ })[0]);
    const legs = await screen.findByRole("table", { name: /Legs of/ });
    expect(within(legs).getByText("SHORT")).toBeInTheDocument();
    expect(within(legs).getByText("HEDGE")).toBeInTheDocument();
  });

  it("shows an empty state, an error state with retry, and a disconnected state", async () => {
    stubApi({ ...fixtureRoutes(), positions: { tab: "open", tab_counts: { open: 0, unresolved: 0, closed: 0 }, total: 0, limit: 50, offset: 0, items: [] } });
    renderWithApp(<Positions/>);
    expect(await screen.findByText("No open positions")).toBeInTheDocument();
    cleanup(); resetStore();
    stubApi({ ...fixtureRoutes(), positions: () => new Response("{}", { status: 500 }) });
    renderWithApp(<Positions/>);
    expect(await screen.findByText(/Positions UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    cleanup(); resetStore();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    renderWithApp(<Positions/>);
    expect(await screen.findByText(/DISCONNECTED/)).toBeInTheDocument();
  });

  it("LIVE mode shows no live engines and never requests positions", async () => {
    resetNav("/positions", "mode=LIVE");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<Positions/>);
    expect(await screen.findByText(/no live engines/i)).toBeInTheDocument();
    expect(calls.filter(c => c.includes("/positions"))).toHaveLength(0);
  });

  it("fixture sanity: the synthetic fixture contains the expired open position", () => {
    expect(fx.positions_open.items.some((p: { flags: string[] }) => p.flags.includes("EXPIRED_NOT_SETTLED"))).toBe(true);
  });
});

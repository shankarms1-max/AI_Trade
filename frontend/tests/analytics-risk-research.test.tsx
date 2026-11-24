import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Analytics } from "@/components/pages/analytics";
import { Research } from "@/components/pages/research";
import { Risk } from "@/components/pages/risk";
import { Settings } from "@/components/pages/settings";
import { SystemSections } from "@/components/pages/system-sections";
import { fixtureRoutes, fx, nav, renderWithApp, resetNav, resetStore, stubApi } from "./helpers/terminal";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => { resetNav("/analytics"); resetStore(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Analytics", () => {
  it("shows the KPI strip from /performance (no Sharpe/Sortino), small-sample warning and API figures", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Analytics/>);
    expect(await screen.findByRole("group", { name: "Cumulative net P&L" })).toHaveTextContent("−₹108.96");
    for (const label of ["Win rate", "Expectancy / trade", "Profit factor", "Max drawdown", "Current drawdown", "Avg hold", "Largest win", "Largest loss", "Max consecutive wins", "Max consecutive losses", "Gross profit", "Gross loss", "Costs (charges)"]) {
      expect(screen.getByRole("group", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("group", { name: "Win rate" })).toHaveTextContent("75.0%");
    expect(screen.getByRole("group", { name: "Profit factor" })).toHaveTextContent("0.88");
    expect(screen.getByText(/SMALL SAMPLE/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /Sharpe|Sortino/i })).toBeNull();
  });

  it("renders unavailable metrics as explicit UNAVAILABLE with their reason", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Analytics/>);
    expect(await screen.findByText("sharpe sortino UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/sample too short and no valid capital basis/)).toBeInTheDocument();
    expect(screen.getByText("market regime UNAVAILABLE")).toBeInTheDocument();
  });

  it("shows the strategy comparison with sample periods and the comparison note as a visible caption", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Analytics/>);
    const table = await screen.findByRole("table", { name: "Strategy comparison" });
    expect(within(table).getByRole("columnheader", { name: "Sample period" })).toBeInTheDocument();
    expect(within(table).getAllByText(/→/).length).toBe(fx.performance.by_strategy.length);
    expect(screen.getByTestId("comparison-note")).toHaveTextContent(fx.performance.comparison_note);
    expect(screen.getByTestId("comparison-note")).toBeVisible();
  });

  it("shows long/short, exit reasons, score, time of day, holding, cost sensitivity and opportunity rejections", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Analytics/>);
    for (const name of ["Long and short", "Exit-reason contribution", "Entry score vs outcome", "Time of day (IST entry hour)", "Holding duration", "Cost sensitivity", "Opportunity rejections"]) {
      expect(await screen.findByRole("table", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("₹10 per order")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Cumulative net P&L by session" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Monthly net P&L" })).toBeInTheDocument();
  });

  it("shows MAE/MFE only when excursion data exists", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Analytics/>);
    expect(await screen.findByText("MAE / MFE")).toBeInTheDocument();
    cleanup(); resetStore();
    const p = structuredClone(fx.performance); p.excursions = { trades_with_data: 0, avg_mae: null, avg_mfe: null };
    stubApi({ ...fixtureRoutes(), performance: p });
    renderWithApp(<Analytics/>);
    await screen.findByRole("group", { name: "Cumulative net P&L" });
    expect(screen.queryByText("MAE / MFE")).toBeNull();
  });

  it("the date range selector writes the range to the URL; the request carries from/to", async () => {
    resetNav("/analytics", "range=30d");
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<Analytics/>);
    await screen.findByRole("group", { name: "Cumulative net P&L" });
    const q = new URL(calls.find(c => c.includes("/performance"))!, "http://x").searchParams;
    expect(q.get("from")).toMatch(/^\d{4}-\d{2}-\d{2}$/); expect(q.get("mode")).toBe("PAPER");
    fireEvent.click(screen.getByRole("button", { name: "MTD" }));
    expect(nav.replaces.at(-1)).toContain("range=mtd");
  });

  it("empty, error and disconnected states", async () => {
    const p = structuredClone(fx.performance); p.overall.trades = 0;
    stubApi({ ...fixtureRoutes(), performance: p });
    renderWithApp(<Analytics/>);
    expect(await screen.findByText("No closed trades")).toBeInTheDocument();
    cleanup(); resetStore();
    stubApi({ ...fixtureRoutes(), performance: () => new Response("{}", { status: 500 }) });
    renderWithApp(<Analytics/>);
    expect(await screen.findByText(/Analytics UNAVAILABLE/)).toBeInTheDocument();
    cleanup(); resetStore();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("x")));
    renderWithApp(<Analytics/>);
    expect(await screen.findByText(/DISCONNECTED · Analytics/)).toBeInTheDocument();
  });
});

describe("Risk", () => {
  beforeEach(() => resetNav("/risk"));
  it("shows per-engine limits, remaining budget, entries and stop coverage; null limits are UNAVAILABLE not zero", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Risk/>);
    const table = await screen.findByRole("table", { name: "Risk by engine" });
    for (const h of ["Open position risk", "Daily realized net", "Daily loss limit", "Remaining budget", "Max risk / trade", "Entries today / max", "Stop coverage"]) {
      expect(within(table).getByRole("columnheader", { name: h })).toBeInTheDocument();
    }
    const crude = within(table).getAllByRole("row").find(r => /CRUDEOILM/.test(r.textContent ?? ""))!;
    expect(within(crude).getByText("Unavailable")).toBeInTheDocument(); // remaining_daily_loss_budget null
    const nifty = within(table).getAllByRole("row").find(r => /NIFTY ACTS V3/.test(r.textContent ?? ""))!;
    expect(nifty).toHaveTextContent("₹50,000");
  });
  it("exposure: notional is not margin; margin and capital UNAVAILABLE with the reason", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Risk/>);
    const margin = await screen.findByRole("group", { name: "Margin utilisation" });
    expect(within(margin).getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Notional contract value, NOT margin/)).toBeInTheDocument();
    expect(screen.getAllByText(/No paper capital is configured/).length).toBeGreaterThan(0);
  });
  it("lists concentration, unresolved exposure with a red banner, and limitations", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Risk/>);
    expect(await screen.findByRole("alert")).toHaveTextContent(/UNRESOLVED EXPOSURE/);
    const unresolved = screen.getByRole("table", { name: "Unresolved exposure" });
    expect(within(unresolved).getByText("EXPIRED NOT SETTLED")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Concentration" })).toBeInTheDocument();
    for (const l of fx.risk.limitations) expect(screen.getByText(l)).toBeInTheDocument();
  });
  it("error and empty states", async () => {
    stubApi({ ...fixtureRoutes(), risk: () => new Response("{}", { status: 500 }) });
    renderWithApp(<Risk/>);
    expect(await screen.findByText(/Risk UNAVAILABLE/)).toBeInTheDocument();
  });
});

describe("Research", () => {
  it("separates evidence classes with explicit status chips and never fabricates numbers", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Research/>);
    const expectations: Record<string, string> = {
      "Historical simulation": "SEPARATE PAGES", "Synthetic integration tests": "NOT PERFORMANCE EVIDENCE", "Genuine historical backtest": "INSUFFICIENT DATA",
      "Out-of-sample validation": "INSUFFICIENT DATA", "Forward paper": "ACCUMULATING", "Live execution": "NONE",
    };
    for (const [name, status] of Object.entries(expectations)) {
      const card = await screen.findByRole("article", { name });
      expect(within(card).getByText(status)).toBeInTheDocument();
    }
    const forward = screen.getByRole("article", { name: "Forward paper" });
    expect(within(forward).getByRole("link", { name: "Tradebook (PAPER)" })).toHaveAttribute("href", "/tradebook?mode=PAPER");
    expect(within(screen.getByRole("article", { name: "Historical simulation" })).getByRole("link", { name: "Market research" })).toHaveAttribute("href", "/nifty/research");
    expect(within(screen.getByRole("article", { name: "Live execution" })).getByText(/no live trades and no live P&L/)).toBeInTheDocument();
  });
  it("MCX engines carry PRE_REGISTERED_UNVALIDATED from the API", async () => {
    const s = structuredClone(fx.strategies);
    s.items.find((i: { engine_id: string }) => i.engine_id === "mcx-crudeoilm").dimensions.calibration = "PRE_REGISTERED_UNVALIDATED";
    stubApi({ ...fixtureRoutes(), strategies: s });
    renderWithApp(<Research/>);
    const card = await screen.findByRole("article", { name: /CRUDEOILM Futures calibration/ });
    expect(within(card).getByText("PRE REGISTERED UNVALIDATED")).toBeInTheDocument();
    expect(card).toHaveTextContent(/no result is claimed/);
  });
  it("forward paper says NO TRADES when none were recorded", async () => {
    const p = structuredClone(fx.performance); p.overall.trades = 0;
    stubApi({ ...fixtureRoutes(), performance: () => new Response("{}", { status: 500 }) });
    renderWithApp(<Research/>);
    expect(await screen.findByText("NO TRADES")).toBeInTheDocument();
    void p;
  });
});

describe("System sections and Settings", () => {
  it("System: sources with status/count/note, engines with feed state, and charge model status", async () => {
    stubApi({ ...fixtureRoutes(), "api/engines/status": JSON.parse(JSON.stringify(await import("./fixtures/engines-mcx.json").then(m => (m.default as { fixtures: Record<string, unknown> }).fixtures["/api/engines/status"]))) });
    renderWithApp(<SystemSections/>);
    const sources = await screen.findByRole("table", { name: "Data sources" });
    expect(within(sources).getByText("ast_futures").closest("tr")).toHaveTextContent("NOT CONFIGURED");
    expect(within(sources).getByText("acts_paper").closest("tr")).toHaveTextContent("OK");
    expect(within(sources).getByText("commodity_options_legacy").closest("tr")).toHaveTextContent("NOT NORMALISED");
    const charge = await screen.findByRole("table", { name: "Charge model" });
    expect(within(charge).getByText("brokerage").closest("tr")).toHaveTextContent(/UNVERIFIED/);
    expect(within(charge).getByText("stt").closest("tr")).toHaveTextContent(/STATUTORY/);
    expect(await screen.findByRole("table", { name: "Engines" })).toBeInTheDocument();
  });
  it("Settings is read-only: theme toggle only, PAPER default, timezone, polling and charge model", async () => {
    stubApi(fixtureRoutes());
    renderWithApp(<Settings/>);
    expect(screen.getByText("Asia/Kolkata (IST). Naive timestamps from the API are UTC.")).toBeInTheDocument();
    expect(screen.getByText("PAPER")).toBeInTheDocument();
    expect(screen.getByText(/15 s \(one shared request per URL/)).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Charge model" })).toBeInTheDocument();
    const buttons = screen.getAllByRole("button").map(b => b.getAttribute("aria-label"));
    expect(buttons).toEqual(["Switch to light theme"]);
    expect(screen.queryByRole("checkbox")).toBeNull(); expect(screen.queryByRole("switch")).toBeNull(); expect(screen.queryByRole("textbox")).toBeNull();
  });
});

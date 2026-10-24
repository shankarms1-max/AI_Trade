import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CommodityPaper } from "@/components/commodity-paper";
import { api } from "@/lib/api";
import type { PaperSummary } from "@/types/commodity-paper";

vi.mock("@/lib/api", () => ({ api: { commodityPaperSummary: vi.fn(), commodityPaperPage: vi.fn() } }));
const summary = (): PaperSummary => ({ broker_orders_enabled: false, instruments: [{
  instrument_id: "CRUDEOILM", status: "DISABLED", paper_enabled: false, market_open: null,
  pending_entries: 0, open_positions: 0, realized_gross: 0, realized_net: null,
  margin_status: "MARGIN_UNVERIFIED", latest: { decision: { opportunity_score: 0, score_band: "NO_TRADE", blockers: null,
    score_components: null, candidate_rejections: null }, candidate: null, risk_blocks: null, observed_at: "invalid" },
}, { instrument_id: "NATGASMINI", status: "POLICY_UNCONFIGURED", latest: null }] });
beforeEach(() => {
  vi.mocked(api.commodityPaperSummary).mockReset().mockResolvedValue(summary());
  vi.mocked(api.commodityPaperPage).mockReset().mockResolvedValue({ items: [], has_more: false });
});
afterEach(cleanup);

describe("commodity paper evidence only", () => {
  it("distinguishes zero from missing and never infers enabled paper or worker liveness", async () => {
    render(<CommodityPaper/>);
    expect(await screen.findByText(/Paper entry activation: DISABLED/)).toBeInTheDocument();
    expect(screen.getByText(/Market session: UNAVAILABLE. Worker process state: UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText("MARGIN_UNVERIFIED")).toBeInTheDocument();
    const gross = screen.getByText("Cumulative realized gross").closest("div")!;
    expect(within(gross).getByText("₹0.00")).toBeInTheDocument();
    const net = screen.getByText("Realized net (configured costs only)").closest("div")!;
    expect(within(net).getByText("N/A")).toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
    expect(screen.queryByText("Invalid Date")).not.toBeInTheDocument();
    expect(screen.getByText(/descriptive research, not a profitability claim/)).toBeInTheDocument();
  });
  it("handles null collections and unavailable instrument state", async () => {
    vi.mocked(api.commodityPaperSummary).mockResolvedValue({ broker_orders_enabled: false, instruments: null });
    vi.mocked(api.commodityPaperPage).mockResolvedValue({ items: null });
    render(<CommodityPaper/>);
    expect(await screen.findByText("PAPER STATE UNAVAILABLE")).toBeInTheDocument();
    expect(await screen.findByText("NO PAPER RECORDS")).toBeInTheDocument();
  });
  it("resets pagination independently for each instrument and record kind", async () => {
    vi.mocked(api.commodityPaperPage).mockImplementation(async (_instrument, _kind, offset) => ({ items: [], has_more: offset === 0 }));
    render(<CommodityPaper/>);
    await waitFor(() => expect(screen.getByRole("button", { name: "Next paper page" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Next paper page" }));
    await waitFor(() => expect(api.commodityPaperPage).toHaveBeenLastCalledWith("CRUDEOILM", "positions", 50));
    fireEvent.change(screen.getByLabelText("Paper instrument"), { target: { value: "NATGASMINI" } });
    await waitFor(() => expect(api.commodityPaperPage).toHaveBeenLastCalledWith("NATGASMINI", "positions", 0));
    fireEvent.change(screen.getByLabelText("Paper records"), { target: { value: "events" } });
    await waitFor(() => expect(api.commodityPaperPage).toHaveBeenLastCalledWith("NATGASMINI", "events", 0));
    expect(screen.getByText(/Paper entry activation: UNAVAILABLE/)).toBeInTheDocument();
  });
  it("renders exact trade history and keeps gross distinct from unknown net", async () => {
    vi.mocked(api.commodityPaperPage).mockResolvedValue({ items: [{ state: "CLOSED", entry_credit: 10, exit_debit: 6,
      gross_realized_pnl: 40, net_realized_pnl: null, exit_reason: "PROFIT_CAPTURE", margin_status: "MARGIN_UNVERIFIED",
      candidate: { short_leg: { trading_symbol: "TEST_SHORT_PE" }, long_leg: { trading_symbol: "TEST_HEDGE_PE" } } }] });
    render(<CommodityPaper/>);
    const table = await screen.findByRole("table", { name: "Commodity paper lifecycle ledger" });
    expect(within(table).getByText(/TEST_SHORT_PE \/ TEST_HEDGE_PE \(historical spread\)/)).toBeInTheDocument();
    expect(within(table).getByText("₹40.00 / N/A")).toBeInTheDocument();
    expect(within(table).getByText("10.00 / 6.00")).toBeInTheDocument();
    expect(within(table).getByText("CLOSED")).toBeInTheDocument();
  });
  it("does not mislabel entry fills as exit debit", async () => {
    vi.mocked(api.commodityPaperPage).mockResolvedValue({ items: [{ kind: "ENTRY", execution: { value: 12.5, atomic: true } }] });
    render(<CommodityPaper/>);
    expect(await screen.findByText("12.50 / N/A")).toBeInTheDocument();
  });
  it("shows a single short, underlying units, unbounded loss and unknown capital honestly", async () => {
    const data = summary();
    Object.assign(data.instruments![0], { reserved_stress_risk: 3000, expiry_exit_overdue: 1,
      latest: { candidate: { strategy_type: "SHORT_PUT", contract_quantity: 10, executable_credit: 90,
        stress_loss_estimate: 1500, construction_evidence: { margin: { status: "MARGIN_UNVERIFIED", required_margin: null } } } } });
    vi.mocked(api.commodityPaperSummary).mockResolvedValue(data);
    vi.mocked(api.commodityPaperPage).mockResolvedValue({ items: [{ state: "OPEN", action: "SELL", contract_quantity: 10,
      candidate: { strategy_type: "SHORT_PUT", short_leg: { trading_symbol: "TEST_SINGLE_PE" }, contract_quantity: 10 } }] });
    render(<CommodityPaper/>);
    expect(await screen.findByText(/stops and stress scenarios are not guaranteed maximum losses/)).toBeInTheDocument();
    expect(await screen.findByText(/TEST_SINGLE_PE \/ SHORT_PUT/)).toBeInTheDocument();
    expect(screen.getByText("SELL / units 10")).toBeInTheDocument();
    const margin = screen.getByText("Single-instrument broker margin estimate").closest("div")!;
    expect(within(margin).getByText("N/A")).toBeInTheDocument();
    expect(screen.queryByText("Verified margin saved")).not.toBeInTheDocument();
  });
  it("isolates paper summary failure from its journal", async () => {
    vi.mocked(api.commodityPaperSummary).mockRejectedValue(new Error("private backend details"));
    render(<CommodityPaper/>);
    expect(await screen.findByText("COMMODITY PAPER STATUS UNAVAILABLE")).toBeInTheDocument();
    expect(await screen.findByText("NO PAPER RECORDS")).toBeInTheDocument();
    expect(screen.queryByText(/private backend details/)).not.toBeInTheDocument();
  });
  it("shows historical decision scores and risk rejections without inventing orders", async () => {
    vi.mocked(api.commodityPaperPage).mockResolvedValue({ items: [{ event_type: "PAPER_OBSERVATION", sequence: 1,
      latest: { risk_blocks: ["GLOBAL_DEFINED_RISK_LIMIT"], decision: { status: "BLOCKED", direction: "BULL", opportunity_score: 70 } } }] });
    render(<CommodityPaper/>);
    const table = await screen.findByRole("table", { name: "Commodity paper lifecycle ledger" });
    expect(within(table).getByText("BLOCKED / BULL / 70.00 legacy")).toBeInTheDocument();
    expect(within(table).getByText("GLOBAL_DEFINED_RISK_LIMIT")).toBeInTheDocument();
    expect(within(table).queryByText("FILLED")).not.toBeInTheDocument();
  });
  it("shows healthy disabled/configuration states instead of a transport failure", async () => {
    const data = summary();
    Object.assign(data.instruments![0], { latest: null, readiness: "PAPER_DISABLED", session_configured: false,
      configuration_blockers: ["SESSION_UNCONFIGURED", "POLICY_UNCONFIGURED", "GLOBAL_RISK_UNCONFIGURED"],
      realized_gross_today: null, realized_net_today: null, trades_today: 0 });
    vi.mocked(api.commodityPaperSummary).mockResolvedValue(data);
    vi.mocked(api.commodityPaperPage).mockResolvedValue({ items: [] });
    render(<CommodityPaper/>);
    expect(await screen.findByText("PAPER DISABLED")).toBeInTheDocument();
    expect(screen.getByText("NO OBSERVATIONS YET")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("POLICY UNCONFIGURED");
    expect(screen.queryByText("COMMODITY PAPER STATUS UNAVAILABLE")).not.toBeInTheDocument();
    expect(screen.queryByText("COMMODITY PAPER LEDGER UNAVAILABLE")).not.toBeInTheDocument();
    expect(screen.getByText("No captured contract")).toBeInTheDocument();
  });
  it("shows the backend's outside-session readiness without changing paper activation", async () => {
    const data = summary();
    Object.assign(data.instruments![0], { paper_enabled: true, readiness: "OUTSIDE_SESSION", market_open: false });
    vi.mocked(api.commodityPaperSummary).mockResolvedValue(data);
    render(<CommodityPaper/>);
    expect(await screen.findByText("OUTSIDE SESSION")).toBeInTheDocument();
    expect(screen.getByText(/Paper entry activation: ENABLED/)).toBeInTheDocument();
  });
  it("separates context direction from genuine native execution qualification", async () => {
    const data = summary();
    data.instruments![0].latest = { decision: { direction: "BEAR", market_state: "STRONG_BEAR",
      directional_score: 66.2, directional_score_max: 100, directional_components: { momentum: 28, alpha: 8 },
      entry_qualification: { version: "COMMODITY_SINGLE_SHORT_ENTRY_V1", status: "ELIGIBLE",
        evidence: { option_token: "TEST_CE", option_type: "CE", strike: 6000, bid: 80, ask: 81,
          otm_distance_points: 50, observed_strike_step: 25, bid_quantity_units: 10, ask_quantity_units: 20,
          open_interest: 900, volume: 400, premium_to_stress_loss_estimate: 0.8 },
        checks: { volume: { value: 400, minimum: null, status: "DIAGNOSTIC_ONLY" } } },
      candidate_evaluations: [{ option_token: "TEST_CE", strike: 6000, status: "SELECTED" },
        { option_token: "STALE_CE", strike: 6025, status: "REJECTED", reason: "STALE_OR_NONCAUSAL_QUOTE" }] } };
    vi.mocked(api.commodityPaperSummary).mockResolvedValue(data);
    render(<CommodityPaper/>);
    expect(await screen.findByText("Directional state")).toBeInTheDocument();
    expect(screen.getByText("Commodity entry qualification")).toBeInTheDocument();
    expect(screen.getByText(/TEST_CE · CE 6,000.00/)).toBeInTheDocument();
    expect(screen.getByText(/STALE_CE · 6,025.00 · REJECTED · STALE_OR_NONCAUSAL_QUOTE/)).toBeInTheDocument();
    expect(screen.getByText(/400.00 · DIAGNOSTIC_ONLY · minimum N\/A/)).toBeInTheDocument();
    expect(screen.queryByText(/ACTS V2 score/)).not.toBeInTheDocument();
    expect(screen.queryByText("Opportunity score")).not.toBeInTheDocument();
  });
  it("renders frozen trade qualification separately from the latest decision", async () => {
    vi.mocked(api.commodityPaperPage).mockResolvedValue({ items: [{ state: "OPEN", decision: {
      direction: "BULL", directional_score: 61, entry_qualification: { version: "COMMODITY_SINGLE_SHORT_ENTRY_V1",
        status: "ELIGIBLE", evidence: { option_token: "FROZEN_PE", option_type: "PE", strike: 4900, bid: 90, ask: 91 } } } }] });
    render(<CommodityPaper/>);
    const table = await screen.findByRole("table", { name: "Commodity paper lifecycle ledger" });
    expect(within(table).getByText("Frozen entry qualification")).toBeInTheDocument();
    expect(within(table).getByText(/FROZEN_PE · PE 4,900.00/)).toBeInTheDocument();
    expect(within(table).getByText("90.00 / 91.00")).toBeInTheDocument();
    expect(within(table).getAllByText("N/A / N/A").length).toBeGreaterThan(0);
  });

});

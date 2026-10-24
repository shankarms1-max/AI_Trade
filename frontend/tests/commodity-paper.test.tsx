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
    expect(within(table).getByText("BLOCKED / BULL / 70.00")).toBeInTheDocument();
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
});

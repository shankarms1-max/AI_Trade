import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CommodityPage from "@/app/commodity-acts/page";
import { CommodityJournal, CommodityOverview } from "@/components/commodity-acts";
import { api } from "@/lib/api";
import type { CommodityDecision, CommodityDecisionPage, CommoditySummary } from "@/types/commodity-acts";

vi.mock("@/lib/api", () => ({ api: { commodityActsSummary: vi.fn(), commodityActsDecisions: vi.fn() } }));

function decision(): CommodityDecision {
  return { instrument_id: "CRUDEOILM", readiness: "ALPHA_WARMING_UP", snapshot_id: 1,
    stage: "SCORE_TRANSFERABILITY", research_scope: "ACTS directional/scoring research only. Entry qualification is disabled until commodity execution geometry is validated.",
    data_provenance: { option_future_link: "SAME_MASTER_UNDERLYING_NEAREST_EXPIRIES_LINK_NOT_CONFIRMED" },
    decision_kind: "SHADOW_DECISION", observed_at: "2026-10-20T10:00:00+05:30", decided_at: "2026-10-20T10:00:01+05:30",
    reference_price: 9453, future_price: 9453, reference_basis: "SELECTED_MCX_FUTURE", future_instrument_id: "CRUDEOILM19NOV26FUT",
    future_expiry: "2026-11-19", option_expiry: "2026-11-17", lot_size: 10, atm: 9450, session_id: "MCX_CRUDEOILM_2026-10-20",
    maximum_score_without_execution: 86, unavailable_components: ["EXECUTION_SPREAD", "EXECUTION_DEPTH", "EXECUTION_CREDIT"],
    alpha: { validity_state: "PARTIAL", alpha_1_direction: "BULLISH", alpha_2_direction: "INSUFFICIENT_DATA" },
    acts: { opportunity_score: 58.6, score_band: "TRADEABLE", status: "NO_SETUP", direction: "BULL", market_state: "STRONG_BULL",
      score_components: { momentum: 25, structure: 15, alpha: 8.6, positioning: 10, participation: 0, execution: 0 },
      blockers: ["NO_EXECUTABLE_CANDIDATE"] },
    geometry: { modal_strike_step: 50, complete_pairs: 21, contracts: 42, bid_ask_coverage: .5, depth_coverage: 0,
      oi_coverage: 1, volume_coverage: 1, depth_unit: "UNKNOWN", strikes: [9400, 9450, 9500], adjacent_gaps: [50, 50],
      credit_geometry_summary: [{ observed_steps: "1", comparisons: 3, min_observed_credit: -2, max_observed_credit: 4 }] } };
}
function summary(): CommoditySummary {
  return { as_of: "2026-10-20T10:00:01+05:30", version: "COMMODITY_ACTS_PAPER_V1", mode: "PAPER",
    stage: "SCORE_TRANSFERABILITY", research_scope: "ACTS directional/scoring research only. Entry qualification is disabled until commodity execution geometry is validated.",
    broker_orders_enabled: false, spread_execution_enabled: false, pnl: null,
    instruments: [{ instrument_id: "CRUDEOILM", display_name: "CRUDEOILM", configured_enabled: false,
      session_configured: true, worker_state: "PROCESS_STATE_UNAVAILABLE", observation_count: 12, decision_count: 12,
      readiness: "ALPHA_WARMING_UP", latest: decision() },
    { instrument_id: "NATGASMINI", display_name: "NATGASMINI", configured_enabled: false, session_configured: false,
      worker_state: "PROCESS_STATE_UNAVAILABLE", observation_count: 0, decision_count: 0, readiness: "NOT_CAPTURED", latest: null }] };
}
const page = (items: CommodityDecision[] = []): CommodityDecisionPage => ({ instrument_id: "CRUDEOILM", limit: 50, offset: 0, has_more: false, items });

beforeEach(() => {
  vi.mocked(api.commodityActsSummary).mockReset().mockResolvedValue(summary());
  vi.mocked(api.commodityActsDecisions).mockReset().mockResolvedValue(page([decision()]));
});
afterEach(cleanup);

describe("isolated commodity ACTS research", () => {
  it("labels score transferability and keeps research readiness distinct from execution validation", async () => {
    const ready = summary(); ready.instruments[0].readiness = "READY_FOR_ACTS_RESEARCH";
    vi.mocked(api.commodityActsSummary).mockResolvedValue(ready);
    render(<CommodityPage/>);
    expect(screen.getByText(/Stage: SCORE_TRANSFERABILITY. ACTS directional\/scoring research only/)).toBeInTheDocument();
    expect(screen.getByText(/Entry qualification is disabled until commodity execution geometry is validated/)).toBeInTheDocument();
    expect(await screen.findByText("READY FOR ACTS RESEARCH")).toBeInTheDocument();
    expect(screen.getAllByText(/Readiness is for scoring\/research only/)).toHaveLength(2);
    expect(screen.getAllByText("NOT SELECTED")).toHaveLength(2);
    expect(screen.getAllByText("UNKNOWN (unconfirmed)")).toHaveLength(2);
    expect(screen.getByText(/UNCONFIRMED: SAME_MASTER_UNDERLYING_NEAREST_EXPIRIES_LINK_NOT_CONFIRMED/)).toBeInTheDocument();
    expect(screen.getAllByText("UNCONFIRMED")).toHaveLength(2);
    expect(screen.getAllByText(/Research score classification only, not a trade signal or paper entry/)).not.toHaveLength(0);
    expect(screen.getAllByText(/ACTS event time:.*Observed at:.*Processed at:/)).not.toHaveLength(0);
  });

  it("labels paper research only, no broker orders and no P&L, independently of NIFTY", async () => {
    render(<CommodityPage/>);
    expect(screen.getByText(/RESEARCH \/ PAPER ONLY · NO BROKER ORDERS · NO P&L YET/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "CRUDEOILM" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "NATGASMINI" })).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Paper trade journal" })).not.toBeInTheDocument();
    expect(screen.queryByText(/₹/)).not.toBeInTheDocument();
    expect(screen.getByText(/VERIFIED MCX SESSION SETTINGS REQUIRED/)).toBeInTheDocument();
  });

  it("shows original score out of 100, unavailable execution and observed geometry", async () => {
    render(<CommodityOverview/>);
    await screen.findByRole("heading", { name: "CRUDEOILM" });
    expect(screen.getByText("58.60 / 100 · TRADEABLE")).toBeInTheDocument();
    expect(screen.getByText("STRONG_BULL / BULL")).toBeInTheDocument();
    expect(screen.getByText("50.0% / 0.0%")).toBeInTheDocument();
    expect(screen.getByText(/Original 100-point weights · no rescaling/)).toBeInTheDocument();
    expect(screen.getByText(/EXECUTION_SPREAD, EXECUTION_DEPTH, EXECUTION_CREDIT/)).toBeInTheDocument();
    expect(screen.getByText(/Depth unit: UNKNOWN/)).toBeInTheDocument();
    expect(screen.getByText("MCX_CRUDEOILM_2026-10-20")).toBeInTheDocument();
    expect(screen.getByText("CRUDEOILM19NOV26FUT / 2026-11-19")).toBeInTheDocument();
  });

  it("never presents warmup as an error or infers a running worker from captures", async () => {
    render(<CommodityOverview/>);
    expect(await screen.findByText("ALPHA WARMING UP")).toBeInTheDocument();
    expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
    expect(screen.queryByText("COMMODITY RESEARCH UNAVAILABLE")).not.toBeInTheDocument();
    expect(screen.getByText("12 / 12")).toBeInTheDocument();
  });

  it("keeps commodity decision filters separate and resets pagination", async () => {
    vi.mocked(api.commodityActsDecisions).mockImplementation(async (instrument, offset = 0) => ({ ...page(), instrument_id: instrument, offset, has_more: offset === 0 }));
    render(<CommodityJournal/>);
    await waitFor(() => expect(api.commodityActsDecisions).toHaveBeenLastCalledWith("CRUDEOILM", 0));
    await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(api.commodityActsDecisions).toHaveBeenLastCalledWith("CRUDEOILM", 50));
    fireEvent.change(screen.getByLabelText("Instrument"), { target: { value: "NATGASMINI" } });
    await waitFor(() => expect(api.commodityActsDecisions).toHaveBeenLastCalledWith("NATGASMINI", 0));
    expect(await screen.findByText("NO CAPTURED COMMODITY DECISIONS")).toBeInTheDocument();
  });

  it("journals an ENTER label only as shadow research without trade/P&L controls", async () => {
    const row = decision(); row.acts.status = "ENTER";
    vi.mocked(api.commodityActsDecisions).mockResolvedValue(page([row]));
    render(<CommodityJournal/>);
    const table = await screen.findByRole("table", { name: "Commodity shadow decision journal" });
    expect(within(table).getByText("SHADOW_DECISION · ENTER")).toBeInTheDocument();
    expect(within(table).queryByText(/filled|realized P&L/i)).not.toBeInTheDocument();
  });

  it("leaves the journal available when overview data is unavailable", async () => {
    vi.mocked(api.commodityActsSummary).mockRejectedValue(new Error("unavailable"));
    render(<CommodityPage/>);
    expect(await screen.findByText("COMMODITY RESEARCH UNAVAILABLE")).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Commodity shadow decision journal" })).toBeInTheDocument();
  });
});

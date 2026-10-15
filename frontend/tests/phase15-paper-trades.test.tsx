import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Phase15Journal, Phase15Positions } from "@/components/phase15-paper-trades";
import Phase15Page from "@/app/phase15/page";
import { api } from "@/lib/api";
import type { Phase15PaperTrade, Phase15TradePage } from "@/types";

vi.mock("@/lib/api", () => ({ api: { phase15Summary: vi.fn(), phase15Positions: vi.fn(), phase15Journal: vi.fn() } }));

function trade(overrides: Partial<Phase15PaperTrade> = {}): Phase15PaperTrade {
  return { trade_id: "acts-stored-1", strategy_id: "ACTS_V2", display_name: "ACTS V2", state: "OPEN", direction: "BULL",
    decision_timestamp: "2026-10-14T10:00:00+05:30", entry_timestamp: "2026-10-14T10:00:15+05:30", exit_timestamp: null,
    short_strike: 24900, hedge_strike: 24500, spread_width: 400, lot_size: 50, lots: 1,
    entry_credit: 32, current_debit: 28, exit_debit: null, realized_gross_pnl: null, current_unrealized_gross_pnl: 200,
    mfe: 5, mae: -2, best_gross_points: 5, holding_seconds: 180,
    entry_reason: "DIRECTIONAL_CONTINUATION", exit_reason: null, exit_policy_version: "MIRROR_EXIT_V1",
    policy_hash: "stored-policy-hash", policy_allowed_widths: [400], rejection_reason: null, unresolved_reason: null,
    latest_mark_timestamp: "2026-10-15T10:03:00+05:30", mark_available: true, mark_unavailable_reason: null,
    accounting_status: "GROSS_ONLY", opportunity_score: 58.6, score_band: "TRADEABLE", candidate_rejections: { STALE_QUOTE: 2 },
    mirror_diagnostics: [{ name: "opposite_full_entry_signal_first_at", observed_at: "2026-10-15T10:03:00+05:30", opportunity_score: 58.6, score_band: "TRADEABLE" }],
    entry_quotes: { short: { side: "bid", fill_price: 40, bid: 40, ask: 41, bid_quantity: 100, ask_quantity: 150, depth_unit: "UNKNOWN", trading_symbol: "STORED_NIFTY", source_market_timestamp: "2026-10-14T10:00:15+05:30" } },
    exit_quotes: {}, latest_mark_quotes: {}, false_breakout_first_at: null, ...overrides };
}
function page(items: Phase15PaperTrade[] = [], overrides: Partial<Phase15TradePage> = {}): Phase15TradePage {
  return { mode: "PAPER", as_of: "2026-10-15T10:03:00+05:30", date: "2026-10-15", date_basis: "DECISION_OBSERVATION_IST",
    accounting_status: "GROSS_ONLY", items, limit: 100, offset: 0, has_more: false, ...overrides };
}

beforeEach(() => {
  vi.mocked(api.phase15Positions).mockReset().mockResolvedValue(page());
  vi.mocked(api.phase15Journal).mockReset().mockResolvedValue(page());
  vi.mocked(api.phase15Summary).mockReset().mockResolvedValue(null as never);
});
afterEach(cleanup);

describe("Phase 15 positions and journal", () => {
  it("keeps overview, positions and journal independently visible", async () => {
    render(<Phase15Page/>);
    expect(screen.getByRole("heading", { name: "OVERVIEW" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "OPEN POSITIONS" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "TRADE JOURNAL" })).toBeInTheDocument();
    expect(await screen.findByText("NO OPEN PAPER POSITIONS")).toBeInTheDocument();
    expect(await screen.findByText("NO PAPER TRADES MATCH THESE FILTERS")).toBeInTheDocument();
    expect(screen.getByText("PHASE 15 MONITOR UNAVAILABLE")).toBeInTheDocument();
  });

  it("shows a prior-day open position immediately, with exact stored width and research evidence", async () => {
    vi.mocked(api.phase15Positions).mockResolvedValue(page([trade()]));
    render(<Phase15Positions/>);
    const card = await screen.findByRole("article", { name: "ACTS V2 position acts-stored-1" });
    expect(within(card).getByText("Width 400 pts")).toBeInTheDocument();
    expect(within(card).getByText("Short 24,900 / Hedge 24,500")).toBeInTheDocument();
    expect(within(card).getByText("₹200.00")).toBeInTheDocument();
    expect(within(card).getAllByText("MIRROR_EXIT_V1").length).toBeGreaterThan(0);
    fireEvent.click(within(card).getByText(/Research details/));
    expect(within(card).getByText("stored-policy-hash")).toBeInTheDocument();
    expect(within(card).getByText("STALE_QUOTE: 2")).toBeInTheDocument();
    expect(within(card).getByText("Opposite full entry signal first at")).toBeInTheDocument();
    expect(within(card).getByText(/STORED_NIFTY/)).toBeInTheDocument();
    expect(within(card).getByText("UNKNOWN")).toBeInTheDocument();
    expect(within(card).getAllByText(/14 Oct 2026/).length).toBeGreaterThan(0);
  });

  it("shows N/A rather than fabricated MTM when the current mark is unavailable", async () => {
    // Even an inconsistent/cached response must not display a stale stored value.
    vi.mocked(api.phase15Positions).mockResolvedValue(page([trade({
      mark_available: false, mark_unavailable_reason: "STALE_QUOTE" })]));
    render(<Phase15Positions/>);
    const card = await screen.findByRole("article");
    expect(within(card).getAllByText(/MARK UNAVAILABLE/).length).toBeGreaterThan(0);
    const label = within(card).getByText("Unrealized gross MTM");
    expect(label.parentElement).toHaveTextContent("N/A");
    expect(within(card).queryByText("₹200.00")).not.toBeInTheDocument();
  });

  it("renders closed/open/rejected and preserves historical widths and TIME_STOP", async () => {
    vi.mocked(api.phase15Journal).mockResolvedValue(page([
      trade(), trade({ trade_id: "closed-historical", state: "CLOSED", spread_width: 100, policy_allowed_widths: [100],
        exit_reason: "TIME_STOP", exit_policy_version: null, realized_gross_pnl: 300, exit_debit: 26 }),
      trade({ trade_id: "rejected", state: "ENTRY_REJECTED", rejection_reason: "STALE_QUOTE", entry_timestamp: null }),
    ]));
    render(<Phase15Journal/>);
    const table = await screen.findByRole("table", { name: "Paper trade journal" });
    for (const state of ["OPEN", "CLOSED", "ENTRY REJECTED"]) expect(within(table).getByText(state)).toBeInTheDocument();
    expect(within(table).getAllByText("400 pts")).toHaveLength(2);
    expect(within(table).getByText("100 pts")).toBeInTheDocument();
    expect(within(table).getAllByText("TIME_STOP").length).toBeGreaterThan(0);
    expect(within(table).getByText("₹300.00")).toBeInTheDocument();
    expect(screen.getByText(/GROSS_ONLY · Net costs unavailable/)).toBeInTheDocument();
  });

  it("defaults to Today and sends date/strategy/state filters to the bounded API", async () => {
    render(<Phase15Journal/>);
    await waitFor(() => expect(api.phase15Journal).toHaveBeenCalled());
    expect(api.phase15Journal).toHaveBeenLastCalledWith(expect.objectContaining({ date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/), strategy: undefined, state: undefined, offset: 0 }));
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText("Strategy"), { target: { value: "ORB_30" } });
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "CLOSED" } });
    await waitFor(() => expect(api.phase15Journal).toHaveBeenLastCalledWith({ date: undefined, strategy: "ORB_30", state: "CLOSED", offset: 0 }));
  });

  it("paginates and resets to the first page when a filter changes", async () => {
    vi.mocked(api.phase15Journal).mockImplementation(async filters => page([trade()], { has_more: !filters?.offset, offset: filters?.offset ?? 0 }));
    render(<Phase15Journal/>);
    const next = await screen.findByRole("button", { name: "Next" });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await waitFor(() => expect(api.phase15Journal).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 100 })));
    fireEvent.change(screen.getByLabelText("Strategy"), { target: { value: "ACTS_V2" } });
    await waitFor(() => expect(api.phase15Journal).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0, strategy: "ACTS_V2" })));
  });

  it("does not let a late old-filter response replace the current journal", async () => {
    let resolveOld!: (value: Phase15TradePage) => void;
    vi.mocked(api.phase15Journal).mockImplementation(filters => filters?.strategy === "ORB_15" ?
      Promise.resolve(page([trade({ trade_id: "selected", display_name: "ORB 15" })])) : new Promise(resolve => { resolveOld = resolve; }));
    render(<Phase15Journal/>);
    await waitFor(() => expect(api.phase15Journal).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Strategy"), { target: { value: "ORB_15" } });
    expect(await screen.findByText("selected")).toBeInTheDocument();
    resolveOld(page([trade({ trade_id: "stale-response" })]));
    await waitFor(() => expect(screen.queryByText("stale-response")).not.toBeInTheDocument());
  });

  it("keeps the journal usable when positions are unavailable", async () => {
    vi.mocked(api.phase15Positions).mockRejectedValue(new Error("unavailable"));
    vi.mocked(api.phase15Journal).mockResolvedValue(page([trade({ trade_id: "journal-only" })]));
    render(<><Phase15Positions/><Phase15Journal/></>);
    expect(await screen.findByText("OPEN PAPER POSITIONS UNAVAILABLE")).toBeInTheDocument();
    expect(await screen.findByText("journal-only")).toBeInTheDocument();
  });
});

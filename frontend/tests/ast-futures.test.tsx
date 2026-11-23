import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AstFuturesPage from "@/app/ast-futures/page";
import { Navigation } from "@/components/navigation";
import { astApi } from "@/lib/ast-futures";
import type { AstPerformance, AstStatus, AstTrade } from "@/types/ast-futures";

vi.mock("@/lib/ast-futures", () => ({ astApi: { status: vi.fn(), trades: vi.fn(), events: vi.fn(), performance: vi.fn() } }));
vi.mock("@/lib/api", () => ({ api: { health: vi.fn().mockResolvedValue({ status: "HEALTHY" }) } }));
vi.mock("next/navigation", () => ({ usePathname: () => "/ast-futures" }));

const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
const fresh = () => new Date(Date.now() - 5000).toISOString();

function status(overrides: Partial<AstStatus> = {}): AstStatus {
  return {
    strategy_id: "AST_FUTURES_V1_DIRECT", strategy_version: "AST_FUTURES_V1_DIRECT", mode: "PAPER", live_trading: false,
    worker: "RUNNING", worker_enabled: true, heartbeat: { status: null }, heartbeat_age_seconds: 12, kill_switch: false,
    policy_hash: "aa56748d9c75d63340ab", fixed_parameters: { atr_period: 22, multiplier: 3.9, regime: "NONE", optimizer: "DISABLED" },
    policy_configuration: { history_sessions: 15, lot_size: 65, lots: 1, opening_range_ist: "09:15:00-09:30:00",
      session_exit_time_ist: "15:20:00", friction_points_per_round_trip: null },
    trading_date: today, in_session: false, futures_instrument: null, last_observation_at: null, last_completed_bar: null,
    opening_range: null, ast: null, status: null, blockers: null, fill_blocker: null, position: null, position_detail: null,
    mark: null, position_mark_gross_inr: null, trades_today: [], today_gross_points: 0, cumulative_closed_trades: 0,
    cumulative_gross_points: 0, cumulative_net_research_points: null, forced_exit_status: "SCHEDULED_15:20_IST",
    journal_sequence: 0, journal_head: "GENESIS", journal_verification: "VALID", fill_basis: "FUTURES_LTP_PAPER_V1",
    fill_basis_note: "Observed NIFTY futures LTP used as a paper fill proxy.", ...overrides,
  };
}
function watching(overrides: Partial<AstStatus> = {}): AstStatus {
  return status({ in_session: true, last_observation_at: fresh(), status: "WATCH", blockers: ["ORB_NOT_BROKEN"],
    futures_instrument: { instrument_id: "NIFTY26NOVFUT", expiry: "2026-11-26", observed_lot_size: 65, ltp: 26012.5, timing_ok: true, observation_blocker: null },
    opening_range: { high: 26040, low: 25980, range_start: `${today}T09:15:00+05:30`, range_end: `${today}T09:30:00+05:30` },
    ast: { atr_period: 22, multiplier: 3.9, direction: "BULL", value: 25931.4, atr: 18.2, transition: `${today}T09:50:00+05:30` },
    last_completed_bar: { start: `${today}T10:05:00+05:30`, end: `${today}T10:10:00+05:30`, open: 26000, high: 26020, low: 25995,
      close: 26010, valid: true, reason: null, future_instrument_id: "NIFTY26NOVFUT", future_expiry: "2026-11-26" }, ...overrides });
}
function trade(overrides: Partial<AstTrade> = {}): AstTrade {
  return { trade_id: "trade-1", trading_date: today, state: "OPEN", side: "LONG", future_instrument_id: "NIFTY26NOVFUT",
    future_expiry: "2026-11-26", lot_size: 65, quantity: 65, signal_timestamp: `${today}T10:15:00+05:30`,
    entry_timestamp: `${today}T10:15:10+05:30`, entry_price: 26050, exit_timestamp: null, exit_price: null, exit_reason: null,
    gross_points: null, gross_inr: null, net_research_points: null, rejection_reason: null, unresolved_reason: null,
    fill_basis: "FUTURES_LTP_PAPER_V1", ...overrides };
}
function open(side: "LONG" | "SHORT", points: number, state = "OPEN", extra: Partial<AstTrade> = {}): Partial<AstStatus> {
  const position = trade({ side, state, ...extra });
  return { status: state, blockers: ["MAX_OPEN_POSITIONS_REACHED"], position, position_detail: position,
    mark: { trade_id: "trade-1", at: fresh(), price: 26050 + (side === "LONG" ? points : -points), points, mfe_points: 30, mae_points: -12 },
    position_mark_gross_inr: points * 65, trades_today: [position] };
}
const performance = (overrides: Partial<AstPerformance> = {}): AstPerformance => ({
  label: "FORWARD_PAPER_PERFORMANCE", basis: "CLOSED_PAPER_TRADES_FUTURES_LTP_PAPER_V1", drawdown_basis: "CLOSED_TRADE_EQUITY_FROM_ZERO_NOT_MARK_TO_MARKET",
  closed_trades: 0, wins: 0, losses: 0, flat: 0, win_rate: null, gross_points: 0, gross_inr: null, net_research_points: null,
  average_points: null, best_trade: null, worst_trade: null, daily: [], current_peak_points: 0, current_drawdown_points: 0,
  max_realized_drawdown_points: 0, excluded_unresolved_exposures: 0, ...overrides });

beforeEach(() => {
  vi.mocked(astApi.status).mockReset().mockResolvedValue(status());
  vi.mocked(astApi.trades).mockReset().mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0, has_more: false });
  vi.mocked(astApi.events).mockReset().mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0, has_more: false,
    scope: "lifecycle", journal_sequence: 0, journal_head: "GENESIS" });
  vi.mocked(astApi.performance).mockReset().mockResolvedValue(performance());
});
afterEach(cleanup);

describe("AST Futures paper monitor", () => {
  it("shows identity, paper-only disclosure and the frozen 22 / 3.9 policy before the market opens", async () => {
    render(<AstFuturesPage/>);
    expect(screen.getByRole("heading", { name: "AST Futures" })).toBeInTheDocument();
    expect(screen.getByText("NIFTY FUTURES · AST 22 / 3.9")).toBeInTheDocument();
    expect(screen.getByText("PAPER ONLY · NO BROKER ORDERS")).toBeInTheDocument();
    expect(screen.getAllByText(/FUTURES_LTP_PAPER_V1/).length).toBeGreaterThan(0);
    expect(await screen.findByText("Waiting for first market observation")).toBeInTheDocument();
    expect(screen.getByText("No open paper position")).toBeInTheDocument();
    const strategy = screen.getByText("FROZEN STRATEGY · AST_FUTURES_V1_DIRECT").closest("section") as HTMLElement;
    for (const [label, value] of [["ATR", "22"], ["Multiplier", "3.9"], ["Regime", "NONE"], ["Optimizer", "DISABLED"],
      ["Opening range", "09:15–09:30 IST"], ["Forced exit", "15:20 IST"], ["History sessions", "15"]]) {
      expect(within(strategy).getByText(label).nextSibling).toHaveTextContent(value);
    }
    const strip = screen.getByLabelText("AST status");
    for (const text of ["RUNNING", "MARKET CLOSED", "NO OBSERVATION", "FLAT", "VALID", "SCHEDULED 15:20 IST"]) {
      expect(within(strip).getByText(text)).toBeInTheDocument();
    }
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(await screen.findByText("No paper trades today")).toBeInTheDocument();
    expect(await screen.findByText(/cumulative daily chart appears/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/18,031|3,461|18031|3461/);
    expect(screen.queryByRole("button", { name: /kill|acknowledge|enter|exit|buy|sell/i })).not.toBeInTheDocument();
  });

  it("shows the frozen opening range, AST state and humanized WATCH blockers with raw evidence", async () => {
    vi.mocked(astApi.status).mockResolvedValue(watching());
    render(<AstFuturesPage/>);
    expect(await screen.findByText("NIFTY26NOVFUT")).toBeInTheDocument();
    expect(screen.getByText("26,012.50")).toBeInTheDocument();
    expect(screen.getByText(/Observed futures LTP/)).toBeInTheDocument();
    expect(screen.getByText("26,040.00")).toBeInTheDocument();
    expect(screen.getByText("60.00 pts")).toBeInTheDocument();
    expect(screen.getByText("INSIDE RANGE")).toBeInTheDocument();
    expect(screen.getByText("Price inside the opening range")).toBeInTheDocument();
    expect(screen.getByText("ORB_NOT_BROKEN")).toBeInTheDocument();
    expect(within(screen.getByLabelText("AST status")).getByText("MARKET OPEN")).toBeInTheDocument();
    expect(screen.getAllByText("BULL").length).toBeGreaterThan(0);
  });

  it("makes an open LONG paper position the most prominent card", async () => {
    vi.mocked(astApi.status).mockResolvedValue(watching(open("LONG", 22.5)));
    render(<AstFuturesPage/>);
    const card = await screen.findByLabelText("Current paper position");
    expect(within(card).getByText("LONG")).toBeInTheDocument();
    expect(within(card).getByText("+22.50")).toBeInTheDocument();
    expect(within(card).getByText("₹1,462.50")).toBeInTheDocument();
    expect(within(card).getByText("+30.00 / -12.00 pts")).toBeInTheDocument();
    expect(within(card).getByText(/LTP proxy/)).toBeInTheDocument();
    expect(card.compareDocumentPosition(screen.getByLabelText("NIFTY futures instrument")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(screen.getByLabelText("AST status")).getByText("LONG OPEN")).toBeInTheDocument();
  });

  it("renders an open SHORT with an adverse mark and a pending AST flip exit", async () => {
    vi.mocked(astApi.status).mockResolvedValue(watching(open("SHORT", -8, "PENDING_PAPER_EXIT", { pending_exit_reason: "AST_FLIP" })));
    render(<AstFuturesPage/>);
    const card = await screen.findByLabelText("Current paper position");
    expect(within(card).getByText("SHORT")).toBeInTheDocument();
    expect(within(card).getByText("-8.00")).toHaveClass("negative");
    expect(within(card).getByText("AST flip")).toBeInTheDocument();
  });

  it("raises a critical warning for unresolved exposure without inventing an exit", async () => {
    vi.mocked(astApi.status).mockResolvedValue(watching(open("LONG", 0, "UNRESOLVED_EXPOSURE", { unresolved_reason: "HELD_CONTRACT_UNAVAILABLE" })));
    render(<AstFuturesPage/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("UNRESOLVED PAPER EXPOSURE — HELD_CONTRACT_UNAVAILABLE");
    expect(screen.getByLabelText("Current paper position")).toHaveClass("is-critical");
    expect(screen.getByText(/No exit or P&L is assumed/)).toBeInTheDocument();
  });

  it("warns strongly when the worker is stale or stopped", async () => {
    vi.mocked(astApi.status).mockResolvedValue(status({ worker: "STOPPED", heartbeat_age_seconds: 400 }));
    render(<AstFuturesPage/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("WORKER STOPPED — heartbeat is 400s old");
    expect(within(screen.getByLabelText("AST status")).getByText("UNHEALTHY")).toBeInTheDocument();
  });

  it("renders today's paper ledger and forward paper performance separately", async () => {
    const closed = trade({ state: "CLOSED", exit_timestamp: `${today}T11:00:00+05:30`, exit_price: 26090, exit_reason: "AST_FLIP",
      gross_points: 40, gross_inr: 2600 });
    vi.mocked(astApi.status).mockResolvedValue(watching({ trades_today: [closed], today_gross_points: 40 }));
    vi.mocked(astApi.trades).mockResolvedValue({ items: [closed], total: 1, limit: 100, offset: 0, has_more: false });
    vi.mocked(astApi.performance).mockResolvedValue(performance({ closed_trades: 3, wins: 2, losses: 1, win_rate: 2 / 3, gross_points: 55,
      gross_inr: 3575, average_points: 55 / 3, max_realized_drawdown_points: 15, best_trade: { trade_id: "a", trading_date: today, side: "LONG", gross_points: 40 },
      worst_trade: { trade_id: "b", trading_date: today, side: "SHORT", gross_points: -15 },
      daily: [{ date: "2026-11-20", trades: 2, points: 15, cumulative_points: 15 }, { date: today, trades: 1, points: 40, cumulative_points: 55 }] }));
    render(<AstFuturesPage/>);
    const ledger = await screen.findByRole("table", { name: "Today's paper trades" });
    const row = within(ledger).getAllByRole("row")[1];
    expect(within(row).getByText("NIFTY26NOVFUT")).toBeInTheDocument();
    expect(within(row).getByText("+40.00")).toHaveClass("positive");
    expect(within(row).getByText("₹2,600.00")).toBeInTheDocument();
    expect(within(row).getByText("AST flip")).toBeInTheDocument();
    expect(vi.mocked(astApi.trades)).toHaveBeenCalledWith(today);
    expect(await screen.findByText("66.7%")).toBeInTheDocument();
    expect(screen.getByText(/Not a backtest/)).toBeInTheDocument();
    expect(screen.getByLabelText("Cumulative forward paper points by day")).toBeInTheDocument();
    expect(screen.getByText("15.00 pts")).toBeInTheDocument();
  });

  it("keeps independent sections working when the status API is unavailable", async () => {
    vi.mocked(astApi.status).mockRejectedValue(new Error("down"));
    render(<AstFuturesPage/>);
    expect(await screen.findByText(/AST STATUS UNAVAILABLE/)).toBeInTheDocument();
    expect(await screen.findByText("FORWARD PAPER PERFORMANCE")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("JOURNAL · AUDIT EVIDENCE")).toBeInTheDocument());
    expect(screen.getByText("PAPER ONLY · NO BROKER ORDERS")).toBeInTheDocument();
  });

  it("shows journal evidence as a read-only audit table", async () => {
    vi.mocked(astApi.status).mockResolvedValue(status({ journal_sequence: 9, journal_head: "abcdef0123456789" }));
    vi.mocked(astApi.events).mockResolvedValue({ items: [{ sequence: 9, event_type: "ENTRY", timestamp: `${today}T10:15:10+05:30`, snapshot_id: 5,
      trade_id: "trade-1-long-id", event_hash: "abcdef0123456789", previous_hash: "x", summary: { state: "OPEN", entry_price: 26050 } }],
      total: 1, limit: 25, offset: 0, has_more: false, scope: "lifecycle", journal_sequence: 9, journal_head: "abcdef0123456789" });
    render(<AstFuturesPage/>);
    const table = await screen.findByRole("table", { name: "Lifecycle journal events" });
    expect(within(table).getByText("state: OPEN · entry_price: 26050")).toBeInTheDocument();
    expect(screen.getByText("abcdef0123456789")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("navigation", () => {
  it("lists AST Futures with the NIFTY strategy and research destinations", () => {
    render(<Navigation/>);
    const links = screen.getAllByRole("link", { name: "AST Futures" });
    expect(links.length).toBeGreaterThan(0);
    expect(links[0]).toHaveAttribute("href", "/ast-futures");
    expect(links[0]).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("AST Futures", { selector: "strong" })).toBeInTheDocument();
  });
});

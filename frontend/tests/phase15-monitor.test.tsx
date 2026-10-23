import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Phase15Monitor, blockerLabel } from "@/components/phase15-monitor";
import { api } from "@/lib/api";
import type { Phase15Strategy, Phase15Summary, Phase15Trade } from "@/types";

vi.mock("@/lib/api", () => ({ api: { phase15Summary: vi.fn() } }));

const ids = ["ACTS_V2", "ORB_15", "ORB_30", "ORB_60", "EMA_PULLBACK", "SUPERTREND_RSI_VOLUME"];
const names = ["ACTS V2", "ORB 15", "ORB 30", "ORB 60", "EMA Pullback", "Supertrend + RSI + Volume"];
const openTrade: Phase15Trade = {
  trade_id: "paper-1", state: "OPEN", direction: "BULL",
  decision_timestamp: "2026-10-15T04:00:00+00:00", entry_timestamp: "2026-10-15T04:03:00+00:00", exit_timestamp: null,
  short_strike: 25000, hedge_strike: 24600, entry_credit: 42.5,
  current_debit: 45, exit_debit: null, realized_gross_pnl: null, current_unrealized_gross_pnl: -187.5,
  mfe: 5, mae: -2.5, holding_seconds: 183, entry_reason: "STRUCTURE_CONFIRMED", exit_reason: null,
  false_breakout: false,
};
function fixture(): Phase15Summary {
  const strategies: Phase15Strategy[] = ids.map((strategy_id, index) => ({
    strategy_id, display_name: names[index], trades_today: index === 0 ? 1 : 0,
    wins_today: 0, losses_today: 0, entry_rejections_today: index === 1 ? 1 : 0,
    latest_rejection_reason: index === 1 ? "MAX_TRADES_PER_DAY_REACHED" : null,
    realized_gross_pnl_today: index === 2 ? 250 : 0,
    unrealized_gross_pnl: index === 0 ? -187.5 : 0, open_position_count: index === 0 ? 1 : 0,
    unresolved_exposure_count: 0, expired_unpriced_count: 0,
    latest_status: index === 0 ? "OPEN" : "NO_SETUP",
    latest_blocker: index === 1 ? "ORB_REENTRY_DISABLED_OR_SAME_BREAKOUT" : null,
    latest_timestamp: "2026-10-15T04:03:00+00:00", accounting_status: "GROSS_ONLY",
    today_trades: index === 0 ? [openTrade] : [],
  }));
  return {
    mode: "PAPER", as_of: "2026-10-15T04:06:00+00:00", accounting_status: "GROSS_ONLY",
    totals: { realized_gross_pnl_today: 250, unrealized_gross_pnl: -187.5,
      combined_gross_pnl: 62.5, open_positions: 1, unresolved_exposures: 0, expired_unpriced_count: 0, trades_today: 1 }, strategies,
  };
}

beforeEach(() => vi.mocked(api.phase15Summary).mockReset());
afterEach(cleanup);

describe("Phase 15 monitor", () => {
  it("filters workspace strategies without changing strategy evidence", async () => {
    const data = fixture();
    vi.mocked(api.phase15Summary).mockResolvedValue(data);
    render(<Phase15Monitor workspace/>);
    const table = await screen.findByRole("table", { name: "Phase 15 strategies" });
    fireEvent.click(screen.getByRole("button", { name: "ORB 30" }));
    expect(within(table).getByRole("button", { name: "Expand ORB 30" })).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: "Expand ACTS V2" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "All strategies" }));
    expect(within(table).getByRole("button", { name: "Expand ACTS V2" })).toBeInTheDocument();
    expect(data.strategies).toHaveLength(6);
    expect(data.totals.combined_gross_pnl).toBe(62.5);
  });
  it("renders six strategies, gross totals, and a negative live mark", async () => {
    vi.mocked(api.phase15Summary).mockResolvedValue(fixture());
    render(<Phase15Monitor/>);
    const table = await screen.findByRole("table", { name: "Phase 15 strategies" });
    for (const name of names) expect(within(table).getByRole("button", { name: `Expand ${name}` })).toBeInTheDocument();
    expect(screen.getAllByText("₹250.00")).toHaveLength(2);
    expect(screen.getAllByText("−₹187.50")).toHaveLength(2);
    expect(screen.getByText("GROSS ONLY")).toBeInTheDocument();
    expect(screen.getByText("Net transaction-cost accounting is not configured.")).toBeInTheDocument();
    expect(screen.queryByText(/net profit/i)).not.toBeInTheDocument();
  });

  it("expands an open trade and keeps rejected decisions separate", async () => {
    vi.mocked(api.phase15Summary).mockResolvedValue(fixture());
    render(<Phase15Monitor/>);
    fireEvent.click(await screen.findByRole("button", { name: "Expand ACTS V2" }));
    const details = screen.getByRole("table", { name: "ACTS V2 today trades" });
    expect(within(details).getByText("25,000 / 24,600")).toBeInTheDocument();
    expect(within(details).getByText("42.50")).toBeInTheDocument();
    expect(within(details).getByText("OPEN")).toBeInTheDocument();
    expect(within(details).getByText("Structure confirmed")).toBeInTheDocument();
    expect(screen.getByText("Entry rejections today: 0")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand ORB 15" }));
    expect(screen.getByText(/Entry rejections today: 1/)).toBeInTheDocument();
    expect(screen.getByText("NO ENTERED TRADES TODAY")).toBeInTheDocument();
    expect(screen.getByText("Re-entry disabled / same breakout")).toBeInTheDocument();
  });

  it("separates unresolved and expired unpriced records from open and realized P&L", async () => {
    const data = fixture();
    data.totals.unresolved_exposures = 2;
    data.totals.expired_unpriced_count = 3;
    data.strategies[0].unresolved_exposure_count = 2;
    data.strategies[0].expired_unpriced_count = 3;
    data.strategies[0].today_trades = [{ ...openTrade, state: "CLOSED", current_debit: null,
      current_unrealized_gross_pnl: null, expiry_terminalization: true,
      terminal_status: "CONTRACT EXPIRED / EXIT PRICE UNAVAILABLE", exit_reason: "CONTRACT_EXPIRED_UNPRICED" }];
    vi.mocked(api.phase15Summary).mockResolvedValue(data);
    render(<Phase15Monitor/>);
    fireEvent.click(await screen.findByRole("button", { name: "Expand ACTS V2" }));
    expect(screen.getByText("Unresolved Exposures")).toBeInTheDocument();
    expect(screen.getAllByText("Expired Unpriced")).toHaveLength(2);
    const details = screen.getByRole("table", { name: "ACTS V2 today trades" });
    expect(within(details).getByText("CONTRACT EXPIRED / EXIT PRICE UNAVAILABLE")).toBeInTheDocument();
    expect(within(details).queryByText(/₹0/)).not.toBeInTheDocument();
  });

  it("treats ordinary blockers as labels, not errors", () => {
    expect(blockerLabel("MAX_TRADES_PER_DAY_REACHED")).toBe("Max trades per day reached");
    expect(blockerLabel("VOLUME_CONFIRMATION_REQUIRED")).toBe("Volume confirmation required");
    expect(blockerLabel("BAR_ALREADY_EVALUATED")).toBe("Bar already evaluated");
    expect(blockerLabel("ORB_REENTRY_DISABLED_OR_SAME_BREAKOUT")).toBe("Re-entry disabled / same breakout");
  });

  it.each([
    ["OPPOSITE_ACTS_V2_ENTRY_SIGNAL", "Opposite acts v2 entry signal"],
    ["SUSTAINED_OPPOSITE_ACTS_TRADEABLE_THESIS", "Sustained opposite acts tradeable thesis"],
  ])("shows the stored exit policy and preserves the actual reason %s", async (reason, label) => {
    const data = fixture();
    data.strategies[0].today_trades = [{ ...openTrade, state: "CLOSED",
      exit_policy_version: "MIRROR_EXIT_V1",
      exit_reason: reason }];
    vi.mocked(api.phase15Summary).mockResolvedValue(data);
    render(<Phase15Monitor/>);
    fireEvent.click(await screen.findByRole("button", { name: "Expand ACTS V2" }));
    const details = screen.getByRole("table", { name: "ACTS V2 today trades" });
    expect(within(details).getByText("MIRROR_EXIT_V1")).toBeInTheDocument();
    expect(within(details).getByText(label)).toHaveAttribute("title", reason);
  });

  it("shows N/A for one unavailable strategy while preserving the other five", async () => {
    const data = fixture();
    data.strategies[4] = { ...data.strategies[4], trades_today: null,
      wins_today: null, losses_today: null, entry_rejections_today: null,
      open_position_count: null, realized_gross_pnl_today: null, unrealized_gross_pnl: null,
      latest_status: null, latest_blocker: null };
    data.totals.combined_gross_pnl = null;
    vi.mocked(api.phase15Summary).mockResolvedValue(data);
    render(<Phase15Monitor/>);
    const table = await screen.findByRole("table", { name: "Phase 15 strategies" });
    for (const name of names) expect(within(table).getByRole("button", { name: `Expand ${name}` })).toBeInTheDocument();
    const emaRow = within(table).getByRole("button", { name: "Expand EMA Pullback" }).closest("tr");
    expect(emaRow).toHaveTextContent("N/A");
  });

  it("shows unavailable without inferring paper activity when the response is absent", async () => {
    vi.mocked(api.phase15Summary).mockResolvedValue(null as unknown as Phase15Summary);
    render(<Phase15Monitor/>);
    expect(await screen.findByText("PHASE 15 MONITOR UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.queryByText("GROSS ONLY")).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Phase 15 strategies" })).not.toBeInTheDocument();
  });
});

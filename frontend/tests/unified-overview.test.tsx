import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CrossMarketOverview } from "@/components/cross-market-overview";
import { completeSum, observationState } from "@/lib/monitoring";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: { phase15Summary: vi.fn(), commodityPaperSummary: vi.fn(), health: vi.fn() } }));
afterEach(cleanup);
describe("unified monitoring evidence", () => {
  it("never turns missing or nonfinite metrics into zero", () => {
    expect(completeSum([1, null, 2])).toBeNull();
    expect(completeSum([1, NaN])).toBeNull();
    expect(completeSum([])).toBeNull();
    expect(completeSum([1, 0, 2])).toBe(3);
  });
  it("distinguishes stale, missing and future observations", () => {
    expect(observationState(null)).toBe("UNAVAILABLE");
    expect(observationState("2026-10-23T00:00:00Z", Date.parse("2026-10-23T00:05:00Z"))).toBe("STALE");
    expect(observationState("2026-10-23T00:05:00Z", Date.parse("2026-10-23T00:00:00Z"))).toBe("FUTURE_OBSERVATION");
  });
  it("retains NIFTY when Commodity fails without fabricating combined metrics", async () => {
    vi.mocked(api.phase15Summary).mockResolvedValue({ totals: { open_positions: 2, trades_today: 3 }, strategies: [
      {strategy_id:"ACTS_V3",display_name:"ACTS V3",open_position_count:2,trades_today:3},
      {strategy_id:"ACTS_V2",display_name:"ACTS V2",open_position_count:0,trades_today:0}] } as never);
    vi.mocked(api.commodityPaperSummary).mockRejectedValue(new Error("private error"));
    vi.mocked(api.health).mockResolvedValue({ status: "HEALTHY", components: {} } as never);
    render(<CrossMarketOverview/>);
    await waitFor(() => expect(screen.getByText(/ACTS versions: 2/)).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "NIFTY" })).toBeInTheDocument();
    expect(screen.getByText(/Open: 2/)).toBeInTheDocument();
    expect(screen.queryByText("private error")).not.toBeInTheDocument();
    expect(screen.getByText(/Partial or missing records/)).toBeInTheDocument();
  });
});

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OrbRange } from "@/components/phase15-orb-evidence";
import type { Phase15OrbRange } from "@/types";

afterEach(cleanup);

function evidence(overrides: Partial<Phase15OrbRange> = {}): Phase15OrbRange {
  return { range_minutes: 15, range_start: "2026-10-23T09:15:00+05:30", range_end: "2026-10-23T09:30:00+05:30",
    opening_high: 25684.40, opening_low: 25621.15, current_future_price: 25701.80, complete: true,
    breakout_direction: "BULL", breakout_timestamp: "2026-10-23T09:32:00+05:30", breakout_distance_bps: 6.7745,
    confirmation_seconds: 45, reason: null, status: "PENDING_PAPER_ENTRY",
    decision_reason: "CONFIRMED_OPENING_RANGE_BREAKOUT", ...overrides };
}

describe("stored NIFTY futures ORB evidence", () => {
  it.each([[15, "09:30"], [30, "09:45"], [60, "10:15"]])("displays the exact stored ORB %i clock window", (minutes, end) => {
    render(<OrbRange title={`ORB ${minutes}`} evidence={evidence({ range_minutes: Number(minutes),
      range_end: `2026-10-23T${end}:00+05:30` })}/>);
    const card = screen.getByRole("article", { name: `ORB ${minutes}` });
    expect(within(card).getByText(`09:15–${end} IST`)).toBeInTheDocument();
    expect(within(card).getByText("25,684.40")).toBeInTheDocument();
    expect(within(card).getByText("25,621.15")).toBeInTheDocument();
    expect(within(card).getByText(/NIFTY FUTURES/)).toBeInTheDocument();
  });

  it.each([
    [25701.80, true, "ABOVE HIGH"], [25600, true, "BELOW LOW"],
    [25650, true, "INSIDE RANGE"], [25684.40, true, "INSIDE RANGE"],
    [25621.15, true, "INSIDE RANGE"], [25701.80, false, "RANGE FORMING"],
    [null, true, "N/A"],
  ])("reports price %s complete %s as %s", (price, complete, status) => {
    render(<OrbRange title="ORB 15" evidence={evidence({ current_future_price: price, complete })}/>);
    expect(screen.getByText("Status").parentElement).toHaveTextContent(status);
  });

  it("shows signed point distance and stored breakout confirmation without inferring an entry", () => {
    render(<OrbRange title="ORB 15" evidence={evidence({ status: "ENTRY_REJECTED", blocker: "MAX_OPEN_POSITIONS_REACHED" })}/>);
    expect(screen.getByText(/↑ \+17.40 pts/)).toBeInTheDocument();
    expect(screen.getByText("BREAKOUT CONFIRMED")).toBeInTheDocument();
    expect(screen.getByText("45 sec")).toBeInTheDocument();
    expect(screen.getByText(/MAX_OPEN_POSITIONS_REACHED/)).toBeInTheDocument();
    expect(screen.getByText("09:32:00 IST")).toBeInTheDocument();
  });

  it("shows the stored watch state", () => {
    render(<OrbRange title="ORB 15" evidence={evidence({ status: "WATCH", decision_reason: null, confirmation_seconds: 15 })}/>);
    expect(screen.getByText("BREAKOUT WATCH")).toBeInTheDocument();
    expect(screen.queryByText("BREAKOUT CONFIRMED")).not.toBeInTheDocument();
    expect(screen.getByText("15 sec")).toBeInTheDocument();
  });

  it("does not borrow a current range when rendering frozen historical trade evidence", () => {
    const { rerender } = render(<><OrbRange title="Latest ORB 15" evidence={evidence()}/>
      <OrbRange title="Frozen entry ORB range" frozen evidence={evidence({ opening_high: 25000, opening_low: 24900 })}/></>);
    rerender(<><OrbRange title="Latest ORB 15" evidence={evidence({ opening_high: 99999, opening_low: 88888 })}/>
      <OrbRange title="Frozen entry ORB range" frozen evidence={evidence({ opening_high: 25000, opening_low: 24900 })}/></>);
    const frozen = screen.getByRole("article", { name: "Frozen entry ORB range" });
    expect(within(frozen).getByText("25,000.00")).toBeInTheDocument();
    expect(within(frozen).getByText("24,900.00")).toBeInTheDocument();
    expect(within(frozen).queryByText("99,999.00")).not.toBeInTheDocument();
    expect(within(frozen).queryByText("Current future")).not.toBeInTheDocument();
  });

  it("keeps unavailable evidence N/A instead of reconstructing a range", () => {
    render(<OrbRange title="ORB 15"/>);
    expect(screen.getByText("High").parentElement).toHaveTextContent("N/A");
    expect(screen.getByText("Low").parentElement).toHaveTextContent("N/A");
    expect(screen.queryByText("RANGE FORMING")).not.toBeInTheDocument();
  });
});

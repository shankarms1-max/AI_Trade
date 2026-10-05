import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CandidateTable } from "@/components/candidate-economics";
import type { StrategyCandidate } from "@/types";

const leg = { strike: 24600, option_type: "PE" as const, trading_symbol: "SHORT", ltp: 50, bid: 49, ask: 51, open_interest: 5000, volume: 500 };
const candidate: StrategyCandidate = {
  candidate_id: "phase14_2_v1:1", strategy_type: "BULL_PUT_SPREAD", short_leg: leg,
  long_leg: { ...leg, strike: 24200 }, spread_width: 400, net_credit: 30, credit_to_width_ratio: .075,
  pricing_basis: "BID_ASK", selection_score: 72, support_or_resistance_reference: 24750, warnings: [],
  strategy_logic_version: "phase14_2_v1", strategy_family: "THETA_CARRY_CREDIT_SPREAD", market_bias: "NEUTRAL",
  directional_strength: "WEAK", survival_score: 80, carry_score: 55, distance_in_expected_move_units: 1.67,
  days_to_expiry: 3, dte_bucket: "LE_3_DTE", max_loss_per_lot: 18500, theta_gamma_balance_state: "BALANCED",
  expected_move_source: "VIX", expected_move_points: 240, gamma_risk_state: "MODERATE", cost_estimate_complete: false,
  estimated_cost: 1, net_credit_after_cost: 29, survival_components: { distance: .8 },
};

describe("Phase 14.2 candidate economics", () => {
  it("shows economics with expandable components", () => {
    render(<CandidateTable candidates={[candidate]} />);
    expect(screen.getByText(/THETA CARRY CREDIT SPREAD/)).toBeInTheDocument();
    expect(screen.getByText("80 / 55")).toBeInTheDocument();
    expect(screen.getByText("1.67×")).toBeInTheDocument();
    expect(screen.getByText("400")).toBeInTheDocument();
    expect(screen.getByText("BALANCED")).toBeInTheDocument();
    expect(screen.getByText("Score components and penalties")).toBeInTheDocument();
    expect(screen.getByText(/incomplete costs/)).toBeInTheDocument();
  });
  it("keeps legacy candidates unclassified", () => {
    const legacy = { ...candidate, strategy_logic_version: undefined, strategy_family: undefined };
    render(<CandidateTable candidates={[legacy]} />);
    expect(screen.queryByText("Survival / Carry")).not.toBeInTheDocument();
    expect(screen.queryByText(/THETA CARRY CREDIT SPREAD/)).not.toBeInTheDocument();
  });
});

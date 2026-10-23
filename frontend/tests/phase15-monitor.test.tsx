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

describe("Phase 15 responsive strategy monitor",()=>{
 it("shows all six strategies and preserves supplied evidence without another read",()=>{
  const data=fixture();const before=structuredClone(data);render(<Phase15Monitor summary={data} workspace compact/>);
  for(const name of names)expect(screen.getByRole("button",{name:`Expand ${name}`})).toBeInTheDocument();
  expect(api.phase15Summary).not.toHaveBeenCalled();expect(data).toEqual(before);
  expect(screen.queryByRole("table",{name:"Phase 15 strategies"})).not.toBeInTheDocument();
 });
 it("filters the strategy workspace without mutating counts",()=>{
  const data=fixture();render(<Phase15Monitor summary={data} workspace/>);
  fireEvent.change(screen.getByLabelText("Strategy selection"),{target:{value:"ORB_30"}});
  expect(screen.getByRole("button",{name:"Expand ORB 30"})).toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"Expand ACTS V2"})).not.toBeInTheDocument();expect(data.strategies).toHaveLength(6);
 });
 it("shows gross values and keeps technical execution evidence behind expansion",async()=>{
  vi.mocked(api.phase15Summary).mockResolvedValue(fixture());render(<Phase15Monitor/>);
  fireEvent.click(await screen.findByRole("button",{name:"Expand ACTS V2"}));
  const details=screen.getByRole("table",{name:"ACTS V2 today trades"});
  expect(within(details).getByText("25,000 / 24,600")).toBeInTheDocument();expect(within(details).getByText("42.50")).toBeInTheDocument();
  expect(screen.getAllByText("−₹187.50").length).toBeGreaterThan(0);expect(screen.queryByText(/net profit/i)).not.toBeInTheDocument();
 });
 it.each(["OPPOSITE_ACTS_V2_ENTRY_SIGNAL","SUSTAINED_OPPOSITE_ACTS_TRADEABLE_THESIS"])("preserves frozen policy and exit reason %s",reason=>{
  const data=fixture();data.strategies[0].today_trades=[{...openTrade,state:"CLOSED",exit_policy_version:"MIRROR_EXIT_V1",exit_reason:reason}];
  render(<Phase15Monitor summary={data}/>);fireEvent.click(screen.getByRole("button",{name:"Expand ACTS V2"}));
  expect(screen.getByText("MIRROR_EXIT_V1")).toBeInTheDocument();expect(screen.getByTitle(reason)).toHaveTextContent(blockerLabel(reason));
 });
 it("preserves unpriced expiry as unknown rather than a zero exit",()=>{
  const data=fixture();data.strategies[0].today_trades=[{...openTrade,state:"CLOSED",exit_debit:null,realized_gross_pnl:null,terminal_status:"CONTRACT EXPIRED / EXIT PRICE UNAVAILABLE"}];
  render(<Phase15Monitor summary={data}/>);fireEvent.click(screen.getByRole("button",{name:"Expand ACTS V2"}));
  expect(screen.getByText("CONTRACT EXPIRED / EXIT PRICE UNAVAILABLE")).toBeInTheDocument();
  expect(within(screen.getByRole("table")).queryByText(/₹0/)).not.toBeInTheDocument();
 });
 it("renders stored ACTS, EMA and Supertrend values without synthesizing gates",()=>{
  const data=fixture();data.strategies[0].decision_features={opportunity_score:58.6,score_band:"TRADEABLE",alpha_state:"BEAR"};
  data.strategies[4].decision_features={ema_fast:25701,ema_slow:25750,resumption_strength_bps:-6};
  data.strategies[5].decision_features={supertrend_direction:"BEAR",rsi:34,volume_ratio:1.3};
  render(<Phase15Monitor summary={data}/>);expect(screen.getByText("58.6")).toBeInTheDocument();expect(screen.getByText("25,701.00")).toBeInTheDocument();expect(screen.getByText("34.0")).toBeInTheDocument();
 });
 it("keeps unavailable strategy inputs unknown",()=>{
  const data=fixture();data.strategies[4]={...data.strategies[4],latest_status:null,open_position_count:null,decision_features:undefined};render(<Phase15Monitor summary={data}/>);
  expect(screen.getByRole("article",{name:"EMA Pullback strategy"})).toHaveTextContent("N/A");
 });
 it("shows an unavailable response without inventing paper activity",async()=>{
  vi.mocked(api.phase15Summary).mockResolvedValue(null as never);render(<Phase15Monitor/>);
  expect(await screen.findByText("PHASE 15 MONITOR UNAVAILABLE")).toBeInTheDocument();expect(screen.queryByText("GROSS ONLY")).not.toBeInTheDocument();
 });
 it("exposes all three stored ORB windows and a selected futures panel",()=>{
  const data=fixture();for(const [index,end]of [[1,"09:30"],[2,"09:45"],[3,"10:15"]]as const)data.strategies[index].orb_range={range_minutes:+ids[index].split("_")[1],range_start:"2026-10-23T09:15:00+05:30",range_end:`2026-10-23T${end}:00+05:30`,opening_high:25684.4,opening_low:25621.15,current_future_price:25701.8,complete:true,breakout_direction:"BULL",breakout_timestamp:null,breakout_distance_bps:6.77,confirmation_seconds:45,reason:null};
  render(<Phase15Monitor summary={data} workspace/>);
  for(const [name,end]of [["ORB 15","09:30"],["ORB 30","09:45"],["ORB 60","10:15"]]){fireEvent.click(screen.getByRole("tab",{name:new RegExp(name)}));expect(screen.getByText(`09:15–${end} IST`)).toBeInTheDocument();}
 });
 it("labels ordinary blockers without converting them to failures",()=>{
  expect(blockerLabel("ORB_REENTRY_DISABLED_OR_SAME_BREAKOUT")).toBe("Re-entry disabled / same breakout");expect(blockerLabel("VOLUME_CONFIRMATION_REQUIRED")).toBe("Volume confirmation required");
 });
});

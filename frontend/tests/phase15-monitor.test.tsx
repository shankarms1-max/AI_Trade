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
 it("shows only ACTS from cached mixed evidence without another read",()=>{
  const data=fixture();const before=structuredClone(data);render(<Phase15Monitor summary={data} workspace compact/>);
  expect(screen.getByRole("button",{name:"Expand ACTS V2"})).toBeInTheDocument();
  expect(screen.getAllByRole("article")).toHaveLength(1);
  for(const name of names.slice(1))expect(screen.queryByRole("button",{name:`Expand ${name}`})).not.toBeInTheDocument();
  expect(api.phase15Summary).not.toHaveBeenCalled();expect(data).toEqual(before);
  expect(screen.queryByRole("table",{name:"Phase 15 strategies"})).not.toBeInTheDocument();
 });
 it("filters the strategy workspace without mutating counts",()=>{
  const data=fixture();render(<Phase15Monitor summary={data} workspace/>);
  fireEvent.change(screen.getByLabelText("Strategy selection"),{target:{value:"ACTS_V2"}});
  expect(screen.getByRole("button",{name:"Expand ACTS V2"})).toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"Expand ORB 30"})).not.toBeInTheDocument();expect(data.strategies).toHaveLength(6);
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
 it("shows ACTS facts and excludes retired indicator facts",()=>{
  const data=fixture();data.strategies[0].decision_features={opportunity_score:58.6,score_band:"TRADEABLE",alpha_state:"BEAR"};
  data.strategies[4].decision_features={ema_fast:25701,ema_slow:25750,resumption_strength_bps:-6};
  data.strategies[5].decision_features={supertrend_direction:"BEAR",rsi:34,volume_ratio:1.3};
  render(<Phase15Monitor summary={data}/>);expect(screen.getByText("58.6")).toBeInTheDocument();expect(screen.queryByText("25,701.00")).not.toBeInTheDocument();expect(screen.queryByText("34.0")).not.toBeInTheDocument();
 });
 it("keeps V3 separate and uses ACTS facts rather than Supertrend facts",()=>{
  const data=fixture();data.strategies[0].decision_features={opportunity_score:58.6,score_band:"TRADEABLE",alpha_state:"BEAR"};
  data.strategies.push({...structuredClone(data.strategies[0]),strategy_id:"ACTS_V3",display_name:"ACTS V3",trades_today:2,
   realized_gross_pnl_today:125,decision_features:{opportunity_score:86.2,score_band:"VERY_STRONG",alpha_state:"BULL"},
   today_trades:[{...openTrade,cycle_id:"stored-cycle",cycle_state:"ENTERED",reversal_confirmation_seconds:60}]});
  const before=structuredClone(data);render(<Phase15Monitor summary={data} workspace/>);
  const v2=within(screen.getByRole("article",{name:"ACTS V2 strategy"}));
  const v3=within(screen.getByRole("article",{name:"ACTS V3 strategy"}));
  expect(v2.getByText("58.6")).toBeInTheDocument();expect(v2.queryByText("86.2")).not.toBeInTheDocument();
  expect(v3.getByText("Score")).toBeInTheDocument();expect(v3.getByText("86.2")).toBeInTheDocument();
  expect(v3.getByText("VERY STRONG")).toBeInTheDocument();expect(v3.getByText("Alpha")).toBeInTheDocument();
  expect(v3.queryByText("RSI")).not.toBeInTheDocument();expect(v3.queryByText("Volume ratio")).not.toBeInTheDocument();
  expect(screen.getByRole("heading",{name:"ACTS Strategy Status"})).toBeInTheDocument();expect(data).toEqual(before);
  fireEvent.change(screen.getByLabelText("Strategy selection"),{target:{value:"ACTS_V3"}});
  expect(screen.getByRole("button",{name:"Expand ACTS V3"})).toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"Expand ACTS V2"})).not.toBeInTheDocument();
  expect(screen.getByRole("heading",{name:"ACTS Strategy Status"})).toBeInTheDocument();
 });
 it("orders the public ACTS cards V3 before V2",()=>{
  const data=fixture();data.strategies.push({...data.strategies[0],strategy_id:"ACTS_V3",display_name:"ACTS V3"});
  render(<Phase15Monitor summary={data}/>);
  expect(screen.getAllByRole("article").map(node=>node.getAttribute("aria-label"))).toEqual(["ACTS V3 strategy","ACTS V2 strategy"]);
  expect(screen.getByText(/V3 active · V2 history/)).toBeInTheDocument();
  expect(screen.queryByText(/NIFTY strategies/)).not.toBeInTheDocument();
 });
 it("keeps missing V3 scores unknown",()=>{
  const data=fixture();data.strategies=[{...data.strategies[0],strategy_id:"ACTS_V3",display_name:"ACTS V3",decision_features:undefined}];
  render(<Phase15Monitor summary={data}/>);const facts=within(screen.getByRole("article",{name:"ACTS V3 strategy"}));
  for(const label of ["Score","Band","Alpha"])expect(within(facts.getByText(label)).getByText("N/A")).toBeInTheDocument();
  expect(facts.queryByText("RSI")).not.toBeInTheDocument();
 });
 it("shows an unobserved V3 row alongside V2 with zero counts and no inherited facts",()=>{
  const data=fixture();data.strategies[0].decision_features={opportunity_score:58.6,score_band:"TRADEABLE",alpha_state:"BEAR"};
  data.strategies.push({...data.strategies[0],strategy_id:"ACTS_V3",display_name:"ACTS V3",trades_today:0,
   open_position_count:0,unresolved_exposure_count:0,realized_gross_pnl_today:0,unrealized_gross_pnl:0,
   latest_status:null,latest_timestamp:null,direction:null,latest_blocker:null,today_trades:[],
   decision_features:{opportunity_score:null,score_band:null,alpha_state:null}});
  render(<Phase15Monitor summary={data}/>);const v3=within(screen.getByRole("article",{name:"ACTS V3 strategy"}));
  expect(screen.getByRole("heading",{name:"ACTS Strategy Status"})).toBeInTheDocument();expect(v3.getByText("0 open · 0 trades")).toBeInTheDocument();
  for(const label of ["Score","Band","Alpha"])expect(within(v3.getByText(label)).getByText("N/A")).toBeInTheDocument();
  expect(v3.queryByText("58.6")).not.toBeInTheDocument();expect(v3.queryByText("BEAR")).not.toBeInTheDocument();
  expect(v3.queryByText("RSI")).not.toBeInTheDocument();
 });
 it("keeps unavailable strategy inputs unknown",()=>{
  const data=fixture();data.strategies[0]={...data.strategies[0],latest_status:null,open_position_count:null,decision_features:undefined};render(<Phase15Monitor summary={data}/>);
  expect(screen.getByRole("article",{name:"ACTS V2 strategy"})).toHaveTextContent("N/A");
 });
 it("shows an unavailable response without inventing paper activity",async()=>{
  vi.mocked(api.phase15Summary).mockResolvedValue(null as never);render(<Phase15Monitor/>);
  expect(await screen.findByText("ACTS MONITOR UNAVAILABLE")).toBeInTheDocument();expect(screen.queryByText("GROSS ONLY")).not.toBeInTheDocument();
 });
 it("does not render retired names, ORB workspace or tabs",()=>{
  render(<Phase15Monitor summary={fixture()} workspace/>);
  expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("ORB Range")).not.toBeInTheDocument();
  expect(screen.queryByText(/ORB 15|ORB 30|ORB 60|EMA Pullback|Supertrend/)).not.toBeInTheDocument();
 });
 it("labels ordinary blockers without converting them to failures",()=>{
  expect(blockerLabel("CONSUMED_CYCLE")).toBe("Consumed cycle");expect(blockerLabel("VOLUME_CONFIRMATION_REQUIRED")).toBe("Volume confirmation required");
 });
});

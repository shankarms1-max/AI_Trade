import { describe, expect, it } from "vitest";
import { actsSummary } from "@/lib/acts";
import type { Phase15Strategy, Phase15Summary } from "@/types";

function row(strategy_id:string,realized:number,unrealized:number|null):Phase15Strategy {
  return {strategy_id,display_name:strategy_id,trades_today:1,wins_today:0,losses_today:0,
    entry_rejections_today:0,latest_rejection_reason:null,realized_gross_pnl_today:realized,
    unrealized_gross_pnl:unrealized,open_position_count:1,unresolved_exposure_count:0,expired_unpriced_count:0,
    latest_status:null,latest_blocker:null,latest_timestamp:null,accounting_status:"GROSS_ONLY",today_trades:[]};
}
function data(strategies:Phase15Strategy[]):Phase15Summary {
  return {mode:"PAPER",as_of:"2026-11-19T10:00:00+05:30",accounting_status:"GROSS_ONLY",strategies,
    totals:{realized_gross_pnl_today:999999,unrealized_gross_pnl:999999,combined_gross_pnl:1999998,
      open_positions:90,unresolved_exposures:80,expired_unpriced_count:70,trades_today:60}};
}
describe("ACTS-only client projections",()=>{
 it("uses ACTS rows only, orders V3 first and never mutates source data",()=>{
  const original=data([row("ACTS_V2",120,25),row("ORB_15",999999999,999999999),row("ACTS_V3",-40,50)]);
  const before=structuredClone(original);const result=actsSummary(original)!;
  expect(result.strategies.map(item=>item.strategy_id)).toEqual(["ACTS_V3","ACTS_V2"]);
  expect(result.totals).toEqual({realized_gross_pnl_today:80,unrealized_gross_pnl:75,combined_gross_pnl:155,
    open_positions:2,unresolved_exposures:0,expired_unpriced_count:0,trades_today:2});
  expect(original).toEqual(before);
 });
 it("does not treat a missing ACTS version as a zero-value complete portfolio",()=>{
  const result=actsSummary(data([row("ACTS_V3",120,25),row("ORB_15",999999999,999999999)]))!;
  expect(result.totals.combined_gross_pnl).toBeNull();expect(result.totals.open_positions).toBeNull();
 });
 it("retains unavailable ACTS marks even when retired marks are profitable",()=>{
  const result=actsSummary(data([row("ACTS_V2",120,null),row("ACTS_V3",-40,50),row("ORB_15",999999999,999999999)]))!;
  expect(result.totals.realized_gross_pnl_today).toBe(80);expect(result.totals.unrealized_gross_pnl).toBeNull();
  expect(result.totals.combined_gross_pnl).toBeNull();
 });
 it("leaves unavailable summaries unavailable",()=>{expect(actsSummary(null)).toBeNull();});
});

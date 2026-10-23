"use client";
import { useCallback } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { money,integer,number,timeIST } from "@/lib/format";
import { observationState } from "@/lib/monitoring";
import { usePolling } from "@/hooks/use-polling";
import { Badge,Metric,SectionError } from "@/components/ui";
import { Phase15Monitor } from "@/components/phase15-monitor";
import { Phase15Positions } from "@/components/phase15-paper-trades";
import { SectionBoundary } from "@/components/section-boundary";

export function TradingWorkspace({title="Trading overview",evidence=false}:{title?:string;evidence?:boolean}) {
  const loader=useCallback(async()=>{
    const [summary,health]=await Promise.allSettled([api.phase15Summary(),api.health()]);
    return {summary:summary.status==="fulfilled"?summary.value:null,health:health.status==="fulfilled"?health.value:null};
  },[]);
  const {data,error}=usePolling(loader,20000);
  const summary=data?.summary;
  const totals=summary?.totals;
  const latest=[...(summary?.strategies??[])].filter(s=>s.latest_timestamp).sort((a,b)=>Date.parse(b.latest_timestamp!)-Date.parse(a.latest_timestamp!))[0];
  const future=[...(summary?.strategies??[])].filter(s=>s.orb_range&&s.latest_timestamp).sort((a,b)=>Date.parse(b.latest_timestamp!)-Date.parse(a.latest_timestamp!))[0];
  const freshness=error?"UNAVAILABLE":observationState(latest?.latest_timestamp);
  return <div className="trading-workspace">
    <header className="trading-heading"><div><span className="eyebrow">NIFTY · Observed-book paper</span><h1>{title}</h1></div><Badge value="PAPER"/></header>
    <div className="market-context" aria-label="Market and system status"><span>NIFTY FUT <strong>{number(future?.orb_range?.current_future_price)}</strong></span><Badge value={freshness}/><span>System <strong>{data?.health?.status??"UNAVAILABLE"}</strong></span><span className="context-time">Observed {timeIST(latest?.latest_timestamp)}</span></div>
    <section className={`trading-summary ${!data?"is-loading":""}`} aria-label="Today's NIFTY gross summary">
      <Metric label="Gross P&L" value={money(error?null:totals?.combined_gross_pnl)} detail="NIFTY · INR · gross only"/>
      <div className="pnl-breakdown"><span>Realized <strong>{money(totals?.realized_gross_pnl_today)}</strong></span><span>Unrealized <strong>{money(error?null:totals?.unrealized_gross_pnl)}</strong></span></div>
      <Metric label="Open positions" value={integer(totals?.open_positions)}/><Metric label="Trades today" value={integer(totals?.trades_today)}/><Metric label="Unresolved" value={integer(totals?.unresolved_exposures)} tone={totals?.unresolved_exposures?"negative":""}/>
    </section>
    <nav className="strategy-pulse" aria-label="Six strategy state summary">{(summary?.strategies??[]).map(strategy=><a key={strategy.strategy_id} href={`/nifty#strategy-${strategy.strategy_id}`}><strong>{strategy.strategy_id==="SUPERTREND_RSI_VOLUME"?"Supertrend":strategy.display_name}</strong><span>{strategy.latest_status?.replaceAll("_"," ")??"UNAVAILABLE"}{strategy.open_position_count?` · ${strategy.open_position_count} open`:""}</span></a>)}</nav>
    {!summary&&data&&<SectionError>NIFTY SUMMARY UNAVAILABLE · Positions load independently</SectionError>}
    {freshness!=="FRESH"&&data&&<p className="freshness-note">{freshness.replaceAll("_"," ")} · Amounts are stored marks, not streaming quotes. Worker process state is not inferred.</p>}
    {!!totals?.unresolved_exposures&&<a className="attention-line" href="#open-positions" role="alert">{integer(totals.unresolved_exposures)} unresolved exposure · Exact executable mark required. Review positions.</a>}
    <SectionBoundary label="Open paper positions"><Phase15Positions/></SectionBoundary>
    <SectionBoundary label="NIFTY strategy monitor"><Phase15Monitor pending={!data&&!error} summary={summary??null} workspace={evidence} compact/></SectionBoundary>
    <div className="workspace-links"><Link href="/trades">Recent trades & journal →</Link><Link href="/nifty/research">Market research & diagnostics →</Link></div>
    <p className="accounting-note">Gross only. Unknown marks stay N/A. Paper results are not a profitability claim.</p>
  </div>;
}

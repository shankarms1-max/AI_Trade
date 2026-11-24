"use client";
import { useCallback, type ReactNode } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { money,integer,number,timeIST } from "@/lib/format";
import { observationState } from "@/lib/monitoring";
import { usePolling } from "@/hooks/use-polling";
import { Badge,Metric,SectionError } from "@/components/ui";
import { Phase15Monitor } from "@/components/phase15-monitor";
import { Phase15Positions } from "@/components/phase15-paper-trades";
import { SectionBoundary } from "@/components/section-boundary";
import { actsSummary } from "@/lib/acts";

export function TradingWorkspace({title="ACTS Paper Trading",evidence=false,engineOverview}:{title?:string;evidence?:boolean;engineOverview?:ReactNode}) {
  const loader=useCallback(async()=>{
    const [summary,health,dashboard]=await Promise.allSettled([api.phase15Summary(),api.health(),api.dashboard()]);
    return {summary:summary.status==="fulfilled"?summary.value:null,health:health.status==="fulfilled"?health.value:null,
      dashboard:dashboard.status==="fulfilled"?dashboard.value:null};
  },[]);
  const {data,error}=usePolling(loader,20000);
  const summary=actsSummary(data?.summary);
  const totals=summary?.totals;
  const latest=summary?.strategies.find(s=>s.latest_timestamp);
  const freshness=error?"UNAVAILABLE":observationState(latest?.latest_timestamp);
  const positions = <SectionBoundary label="Open paper positions"><Phase15Positions/></SectionBoundary>;
  const strategies = <SectionBoundary label="ACTS strategy monitor">
    <Phase15Monitor pending={!data&&!error} summary={summary??null} workspace={evidence} compact/>
  </SectionBoundary>;
  return <div className={engineOverview ? "trading-workspace desk-overview" : "trading-workspace"}>
    <header className="trading-heading"><div><span className="eyebrow">NIFTY · Observed-book paper</span><h1>{title}</h1></div><Badge value="PAPER"/></header>
    <div className="market-context" aria-label="Market and system status"><span>NIFTY FUT <strong>{number(data?.dashboard?.snapshot?.nifty_future)}</strong></span><Badge value={freshness}/><span>System <strong>{data?.health?.status??"UNAVAILABLE"}</strong></span><span className="context-time">Observed {timeIST(latest?.latest_timestamp)}</span></div>
    <section className={`trading-summary ${!data?"is-loading":""}`} aria-label="Today's ACTS gross summary">
      <Metric label="Gross P&L" value={money(error?null:totals?.combined_gross_pnl)} detail="ACTS · INR · gross only"/>
      <div className="pnl-breakdown"><span>Realized <strong>{money(totals?.realized_gross_pnl_today)}</strong></span><span>Unrealized <strong>{money(error?null:totals?.unrealized_gross_pnl)}</strong></span></div>
      <Metric label="Open positions" value={integer(totals?.open_positions)}/><Metric label="Trades today" value={integer(totals?.trades_today)}/><Metric label="Unresolved" value={integer(totals?.unresolved_exposures)} tone={totals?.unresolved_exposures?"negative":""}/>
    </section>
    <nav className="strategy-pulse" aria-label="ACTS version state summary">{(summary?.strategies??[]).map(strategy=><a key={strategy.strategy_id} href={`/nifty#strategy-${strategy.strategy_id}`}><strong>{strategy.display_name}</strong><span>{strategy.latest_status?.replaceAll("_"," ")??"UNAVAILABLE"}{strategy.open_position_count?` · ${strategy.open_position_count} open`:""}</span></a>)}</nav>
    {!summary&&data&&<SectionError>ACTS SUMMARY UNAVAILABLE · Positions load independently</SectionError>}
    {freshness!=="FRESH"&&data&&<p className="freshness-note">{freshness.replaceAll("_"," ")} · Amounts are stored marks, not streaming quotes. Worker process state is not inferred.</p>}
    {!!totals?.unresolved_exposures&&<a className="attention-line" href="#open-positions" role="alert">{integer(totals.unresolved_exposures)} unresolved exposure · Exact executable mark required. Review positions.</a>}
    {engineOverview ? <div className="desk-columns">
      <div className="desk-positions">{positions}</div>
      <aside className="desk-engines" aria-label="Engine monitoring">{engineOverview}</aside>
      <div className="desk-strategies">{strategies}</div>
    </div> : <>{positions}{strategies}</>}
    <div className="workspace-links"><Link href="/trades">Recent trades & journal →</Link><Link href="/nifty/research">Market research & diagnostics →</Link></div>
    <p className="accounting-note">Gross only. Unknown marks stay N/A. Paper results are not a profitability claim.</p>
  </div>;
}

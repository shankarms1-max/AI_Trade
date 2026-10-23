"use client";
import { ResponsiveTable } from "@/components/responsive-table";
import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { integer, money, number, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Metric, SectionError } from "@/components/ui";
import type { Phase15Strategy, Phase15Trade, Phase15Summary } from "@/types";
import { OrbRange } from "@/components/phase15-orb-evidence";
import { observationState } from "@/lib/monitoring";

const DISPLAY_NAMES: Record<string, string> = {
  ACTS_V2: "ACTS V2", ORB_15: "ORB 15", ORB_30: "ORB 30", ORB_60: "ORB 60",
  EMA_PULLBACK: "EMA Pullback", SUPERTREND_RSI_VOLUME: "Supertrend + RSI + Volume",
};

export function blockerLabel(value: string | null | undefined): string {
  if (!value) return "N/A";
  if (value === "ORB_REENTRY_DISABLED_OR_SAME_BREAKOUT") return "Re-entry disabled / same breakout";
  const words = value.replaceAll("_", " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function amount(value: number | null | undefined) {
  return <span className={value == null ? "muted" : value < 0 ? "negative" : value > 0 ? "positive" : ""}>{money(value)}</span>;
}

function holding(seconds: number | null) {
  if (seconds == null) return "N/A";
  return seconds < 60 ? `${integer(seconds)}s` : `${integer(Math.floor(seconds / 60))}m ${integer(Math.floor(seconds % 60))}s`;
}

function TradeDetails({ strategy }: { strategy: Phase15Strategy }) {
  return <div className="phase15-details">
    <div className="phase15-rejections">Entry rejections today: {integer(strategy.entry_rejections_today)}
      {strategy.latest_rejection_reason && <> · Latest: {blockerLabel(strategy.latest_rejection_reason)}</>}
    </div>
    {strategy.today_trades.length ? <div className="table-wrap"><ResponsiveTable aria-label={`${strategy.display_name} today trades`}>
      <thead><tr><th>Time</th><th>Direction</th><th>Spread</th><th>Entry Credit</th><th>Exit / Current Debit</th><th>Gross P&amp;L / MTM</th><th>MFE</th><th>MAE</th><th>Holding</th><th>Entry Reason</th><th>Exit Policy</th><th>Exit Reason</th><th>State</th></tr></thead>
      <tbody>{strategy.today_trades.map((trade: Phase15Trade) => <tr key={trade.trade_id}>
        <td>{timeIST(trade.entry_timestamp ?? trade.decision_timestamp)}</td><td>{trade.direction ?? "N/A"}</td>
        <td>{number(trade.short_strike, 0)} / {number(trade.hedge_strike, 0)}</td>
        <td>{number(trade.entry_credit)}</td><td>{number(trade.state === "CLOSED" ? trade.exit_debit : trade.current_debit)}</td>
        <td>{amount(trade.state === "CLOSED" ? trade.realized_gross_pnl : trade.current_unrealized_gross_pnl)}</td>
        <td>{number(trade.mfe)}</td><td>{number(trade.mae)}</td><td>{holding(trade.holding_seconds)}</td>
        <td>{blockerLabel(trade.entry_reason)}{trade.strategy_features && <OrbRange title="Frozen entry ORB range" evidence={trade.strategy_features} frozen/>}</td><td>{trade.exit_policy_version ?? "UNAVAILABLE"}</td>
        <td title={trade.exit_reason ?? undefined}>{trade.terminal_status ?? blockerLabel(trade.exit_reason)}</td>
        <td><Badge value={trade.state}/></td>
      </tr>)}</tbody>
    </ResponsiveTable></div> : <Empty>NO ENTERED TRADES TODAY</Empty>}
  </div>;
}

function StrategyFacts({strategy}:{strategy:Phase15Strategy}) {
  const f=strategy.decision_features;
  if(strategy.strategy_id.startsWith("ORB_"))return <div className="strategy-facts"><span>High <b>{number(strategy.orb_range?.opening_high)}</b></span><span>Low <b>{number(strategy.orb_range?.opening_low)}</b></span><span>Future <b>{number(strategy.orb_range?.current_future_price)}</b></span></div>;
  if(strategy.strategy_id==="ACTS_V2")return <div className="strategy-facts"><span>Score <b>{number(f?.opportunity_score,1)}</b></span><span>Band <b>{f?.score_band?.replaceAll("_"," ")??"N/A"}</b></span><span>Alpha <b>{f?.alpha_state??"N/A"}</b></span></div>;
  if(strategy.strategy_id==="EMA_PULLBACK")return <div className="strategy-facts"><span>EMA fast <b>{number(f?.ema_fast)}</b></span><span>EMA slow <b>{number(f?.ema_slow)}</b></span><span>Resumption <b>{number(f?.resumption_strength_bps)} bps</b></span></div>;
  return <div className="strategy-facts"><span>Trend <b>{f?.supertrend_direction??"N/A"}</b></span><span>RSI <b>{number(f?.rsi,1)}</b></span><span>Volume ratio <b>{number(f?.volume_ratio)}</b></span></div>;
}

export function Phase15Monitor({ workspace=false,compact=false,pending=false,summary }: { workspace?:boolean;compact?:boolean;pending?:boolean;summary?:Phase15Summary|null }={}) {
  const loader=useCallback(()=>api.phase15Summary(),[]);
  const response=usePolling(loader,20000,summary===undefined);
  const data=summary===undefined?response.data:summary;
  const error=!pending&&(summary===undefined?(response.error||(!response.loading&&!response.data)):summary===null);
  const [expanded,setExpanded]=useState<string|null>(null);
  const [selected,setSelected]=useState("ALL");
  const [query,setQuery]=useState("");
  const [orb,setOrb]=useState("ORB_15");
  const visible=(data?.strategies??[]).filter(s=>(selected==="ALL"||selected===s.strategy_id)&&`${s.display_name} ${s.latest_status} ${s.latest_blocker}`.toLowerCase().includes(query.toLowerCase()));
  const range=data?.strategies.find(s=>s.strategy_id===orb);
  return <section className="phase15-monitor" aria-label="Phase 15 paper strategy monitor">
    <header className="section-title"><h2>Strategy status</h2><span>Six NIFTY strategies · <Badge value={pending?"PENDING":data&&!error?"GROSS_ONLY":"UNAVAILABLE"}/></span></header>
    {error?<SectionError>PHASE 15 MONITOR UNAVAILABLE</SectionError>:!data?<div className="strategy-skeleton" aria-busy="true">Loading strategy evidence…</div>:<>
      {!compact&&<div className="phase15-metrics"><Metric label="Realized Gross Today" value={amount(data.totals.realized_gross_pnl_today)}/><Metric label="Live Unrealized Gross" value={amount(data.totals.unrealized_gross_pnl)}/><Metric label="Open Positions" value={integer(data.totals.open_positions)}/><Metric label="Unresolved" value={integer(data.totals.unresolved_exposures)}/></div>}
      {workspace&&<details className="strategy-filters"><summary>Filter strategies</summary><div className="filter-controls"><label>Strategy<select aria-label="Strategy selection" value={selected} onChange={e=>setSelected(e.target.value)}><option value="ALL">All strategies</option>{data.strategies.map(s=><option key={s.strategy_id} value={s.strategy_id}>{s.display_name}</option>)}</select></label><label>Search<input aria-label="Filter strategies" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Signal or blocker"/></label></div></details>}
      <div className="strategy-list" aria-label="NIFTY strategy states">{visible.map(strategy=><article id={`strategy-${strategy.strategy_id}`} className="strategy-row" key={strategy.strategy_id} aria-label={`${strategy.display_name} strategy`}>
        <header><button className="strategy-title" aria-expanded={expanded===strategy.strategy_id} aria-label={`${expanded===strategy.strategy_id?"Collapse":"Expand"} ${strategy.display_name}`} onClick={()=>setExpanded(expanded===strategy.strategy_id?null:strategy.strategy_id)}>{DISPLAY_NAMES[strategy.strategy_id]??strategy.display_name}<span aria-hidden="true">{expanded===strategy.strategy_id?"−":"+"}</span></button><Badge value={strategy.latest_status??"UNAVAILABLE"}/></header>
        <div className="strategy-state"><strong>{strategy.direction??"Direction N/A"}</strong><span>{integer(strategy.open_position_count)} open · {integer(strategy.trades_today)} trades</span>{strategy.open_position_count!==0&&<span>Gross {money(strategy.unrealized_gross_pnl)}</span>}</div>
        {strategy.realized_gross_pnl_today!=null&&strategy.realized_gross_pnl_today!==0&&<span className="strategy-age">Realized {money(strategy.realized_gross_pnl_today)}</span>}<StrategyFacts strategy={strategy}/>
        <p className="strategy-reason">{blockerLabel(strategy.latest_blocker)}</p>
        <small className="strategy-age">{observationState(strategy.latest_timestamp).replaceAll("_"," ")} · {timeIST(strategy.latest_timestamp)}</small>
        {expanded===strategy.strategy_id&&<div className="strategy-expanded"><TradeDetails strategy={strategy}/>{strategy.orb_range&&<OrbRange title={`${strategy.display_name} underlying signal`} evidence={strategy.orb_range} observedAt={strategy.latest_timestamp}/>}</div>}
      </article>)}</div>
      {!visible.length&&<Empty>No strategies match this filter</Empty>}
      {workspace&&<section className="orb-workspace" aria-label="ORB Range"><header className="section-title"><h2>Opening range · NIFTY futures</h2></header><div className="orb-tabs" role="tablist" aria-label="Opening range selection">{data.strategies.filter(s=>s.strategy_id.startsWith("ORB_")).map(s=><button role="tab" id={`tab-${s.strategy_id}`} aria-selected={orb===s.strategy_id} aria-controls={`range-${s.strategy_id}`} key={s.strategy_id} onClick={()=>setOrb(s.strategy_id)}>{s.display_name}<small>{s.latest_status?.replaceAll("_"," ")??"N/A"}</small></button>)}</div><div role="tabpanel" id={`range-${orb}`} aria-labelledby={`tab-${orb}`}><OrbRange title={range?.display_name??orb} evidence={range?.orb_range} observedAt={range?.latest_timestamp}/></div></section>}
    </>}
  </section>;
}

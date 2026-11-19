"use client";
import { ResponsiveTable } from "@/components/responsive-table";
import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { integer, money, number, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Metric, SectionError } from "@/components/ui";
import type { Phase15Strategy, Phase15Trade, Phase15Summary } from "@/types";
import { actsSummary } from "@/lib/acts";
import { observationState } from "@/lib/monitoring";

const DISPLAY_NAMES: Record<string, string> = {
  ACTS_V3: "ACTS V3", ACTS_V2: "ACTS V2",
};

export function blockerLabel(value: string | null | undefined): string {
  if (!value) return "N/A";
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
        <td>{blockerLabel(trade.entry_reason)}</td><td>{trade.exit_policy_version ?? "UNAVAILABLE"}</td>
        <td title={trade.exit_reason ?? undefined}>{trade.terminal_status ?? blockerLabel(trade.exit_reason)}</td>
        <td><Badge value={trade.state}/></td>
      </tr>)}</tbody>
    </ResponsiveTable></div> : <Empty>NO ENTERED TRADES TODAY</Empty>}
  </div>;
}

function StrategyFacts({strategy}:{strategy:Phase15Strategy}) {
  const f=strategy.decision_features;
  return <div className="strategy-facts"><span>Score <b>{number(f?.opportunity_score,1)}</b></span><span>Band <b>{f?.score_band?.replaceAll("_"," ")??"N/A"}</b></span><span>Alpha <b>{f?.alpha_state??"N/A"}</b></span></div>;
}

export function Phase15Monitor({ workspace=false,compact=false,pending=false,summary }: { workspace?:boolean;compact?:boolean;pending?:boolean;summary?:Phase15Summary|null }={}) {
  const loader=useCallback(()=>api.phase15Summary(),[]);
  const response=usePolling(loader,20000,summary===undefined);
  const data=actsSummary(summary===undefined?response.data:summary);
  const error=!pending&&(summary===undefined?(response.error||(!response.loading&&!response.data)):summary===null);
  const [expanded,setExpanded]=useState<string|null>(null);
  const [selected,setSelected]=useState("ALL");
  const [query,setQuery]=useState("");
  const visible=(data?.strategies??[]).filter(s=>(selected==="ALL"||selected===s.strategy_id)&&`${s.display_name} ${s.latest_status} ${s.latest_blocker}`.toLowerCase().includes(query.toLowerCase()));
  return <section className="phase15-monitor" aria-label="ACTS paper strategy monitor">
    <header className="section-title"><h2>ACTS Strategy Status</h2><span>V3 active · V2 history · <Badge value={pending?"PENDING":data&&!error?"GROSS_ONLY":"UNAVAILABLE"}/></span></header>
    {error?<SectionError>ACTS MONITOR UNAVAILABLE</SectionError>:!data?<div className="strategy-skeleton" aria-busy="true">Loading ACTS evidence…</div>:<>
      {!compact&&<div className="phase15-metrics"><Metric label="Realized Gross Today" value={amount(data.totals.realized_gross_pnl_today)}/><Metric label="Live Unrealized Gross" value={amount(data.totals.unrealized_gross_pnl)}/><Metric label="Open Positions" value={integer(data.totals.open_positions)}/><Metric label="Unresolved" value={integer(data.totals.unresolved_exposures)}/></div>}
      {workspace&&<details className="strategy-filters"><summary>Filter strategies</summary><div className="filter-controls"><label>Strategy<select aria-label="Strategy selection" value={selected} onChange={e=>setSelected(e.target.value)}><option value="ALL">All strategies</option>{data.strategies.map(s=><option key={s.strategy_id} value={s.strategy_id}>{s.display_name}</option>)}</select></label><label>Search<input aria-label="Filter strategies" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Signal or blocker"/></label></div></details>}
      <div className="strategy-list" aria-label="NIFTY strategy states">{visible.map(strategy=><article id={`strategy-${strategy.strategy_id}`} className="strategy-row" key={strategy.strategy_id} aria-label={`${strategy.display_name} strategy`}>
        <header><button className="strategy-title" aria-expanded={expanded===strategy.strategy_id} aria-label={`${expanded===strategy.strategy_id?"Collapse":"Expand"} ${strategy.display_name}`} onClick={()=>setExpanded(expanded===strategy.strategy_id?null:strategy.strategy_id)}>{DISPLAY_NAMES[strategy.strategy_id]??strategy.display_name}<span aria-hidden="true">{expanded===strategy.strategy_id?"−":"+"}</span></button><Badge value={strategy.latest_status??"UNAVAILABLE"}/></header>
        <div className="strategy-state"><strong>{strategy.direction??"Direction N/A"}</strong><span>{integer(strategy.open_position_count)} open · {integer(strategy.trades_today)} trades</span>{strategy.open_position_count!==0&&<span>Gross {money(strategy.unrealized_gross_pnl)}</span>}</div>
        {strategy.realized_gross_pnl_today!=null&&strategy.realized_gross_pnl_today!==0&&<span className="strategy-age">Realized {money(strategy.realized_gross_pnl_today)}</span>}<StrategyFacts strategy={strategy}/>
        <p className="strategy-reason">{blockerLabel(strategy.latest_blocker)}</p>
        <small className="strategy-age">{observationState(strategy.latest_timestamp).replaceAll("_"," ")} · {timeIST(strategy.latest_timestamp)}</small>
        {expanded===strategy.strategy_id&&<div className="strategy-expanded"><TradeDetails strategy={strategy}/></div>}
      </article>)}</div>
      {!visible.length&&<Empty>No strategies match this filter</Empty>}
    </>}
  </section>;
}

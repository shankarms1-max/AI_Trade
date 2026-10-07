"use client";
import { useCallback, useMemo, useState } from "react";
import { CandidateTable } from "@/components/candidate-economics";
import { api } from "@/lib/api";
import { ageLabel, ageSeconds, integer, money, number, percent, pretty, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Metric, Panel, SectionError } from "@/components/ui";
import { AlphaHistoryChart, BreakdownChart, EquityChart, OIChart, PnlChart } from "@/components/charts";
import { SessionOverview } from "@/components/session-overview";
import type { AlphaFeature, DashboardData, RiskDecision } from "@/types";

function freshness(data: DashboardData) {
  if (!data.snapshot) return { label: "NO DATA", tone: "muted", age: "N/A" };
  const age = ageSeconds(data.snapshot.timestamp_ist);
  const now = new Date(data.system.server_time_ist);
  const ist = new Intl.DateTimeFormat("en-GB", { timeZone:"Asia/Kolkata", weekday:"short", hour:"2-digit", minute:"2-digit", hour12:false }).formatToParts(now);
  const part=(name:string)=>ist.find(item=>item.type===name)?.value ?? "0";
  const minutes=Number(part("hour"))*60+Number(part("minute"));
  const closed=["Sat","Sun"].includes(part("weekday")) || minutes<558 || minutes>927;
  if (closed) return { label:"MARKET CLOSED", tone:"muted", age:ageLabel(age) };
  if (age<=300) return { label:"HEALTHY", tone:"positive", age:ageLabel(age) };
  if (age<=600) return { label:"DELAYED", tone:"warning", age:ageLabel(age) };
  return { label:"STALE", tone:"negative", age:ageLabel(age) };
}
function RiskTable({ decisions }: { decisions: RiskDecision[] }) { return decisions.length ? <div className="table-wrap"><table><thead><tr><th>Candidate</th><th>Decision</th><th>Max profit / lot</th><th>Max loss / lot</th><th>Reward / risk</th><th>Failed</th><th>Warnings</th></tr></thead><tbody>{decisions.map((item,index)=><tr key={`${item.candidate_reference}-${index}`}><td><details><summary>{item.candidate_reference ?? "No candidate"}</summary><table><thead><tr><th>Check</th><th>Status</th><th>Actual</th><th>Threshold</th><th>Message</th></tr></thead><tbody>{item.checks.map(check=><tr key={check.check_code}><td>{pretty(check.check_code)}</td><td><Badge value={check.status}/></td><td>{String(check.actual_value ?? "N/A")}</td><td>{String(check.threshold ?? "N/A")}</td><td>{check.message}</td></tr>)}</tbody></table></details></td><td><Badge value={item.decision}/></td><td>{money(item.max_profit_per_lot)}</td><td>{money(item.max_loss_per_lot)}</td><td>{number(item.reward_to_risk_ratio)}</td><td>{item.failed_checks.length}</td><td>{item.warnings.length}</td></tr>)}</tbody></table></div> : <Empty>NO RISK APPROVALS</Empty>; }

function AlphaPanel({ alpha }: { alpha: AlphaFeature | null | undefined }) {
  if (!alpha) return <Empty>STATISTICAL ALPHA WARMING UP OR DISABLED</Empty>;
  const details: Array<[string, string]> = [
    ["Session", alpha.session_id ?? "N/A"], ["Hypothesis", alpha.hypothesis_type ?? "N/A"],
    ["Lookback clock", alpha.lookback_clock_mode ?? "N/A"],
    ["Actual horizon", alpha.actual_horizon_seconds == null ? "N/A" : `${alpha.actual_horizon_seconds}s`],
    ["Signed log return", number(alpha.signed_log_return,6)], ["Alpha 1 rank", number(alpha.alpha_1,2)],
    ["Alpha 2 rank", number(alpha.alpha_2,2)], ["Standardized return", number(alpha.standardized_return,4)],
    ["Validity", alpha.validity_state ?? "N/A"], ["Participation", alpha.participation_state ?? "N/A"],
    ["Underlying volatility", number(alpha.underlying_horizon_volatility,6)],
    ["CE / PE activity state", `${alpha.ce_volume_state ?? "N/A"} / ${alpha.pe_volume_state ?? "N/A"}`],
    ["Persistence reset", alpha.confirmation_reset_reason ?? "N/A"],
    ["Legacy Alpha 2 (diagnostic)", number(alpha.legacy_alpha2_raw,4)],
  ];
  return <>
    <div className="kv-grid"><div className="kv"><dt>Alpha 1</dt><dd>{number(alpha.alpha_1,2)} · {pretty(alpha.alpha_1_direction)}</dd></div><div className="kv"><dt>Alpha 2</dt><dd>{number(alpha.alpha_2,2)} · {pretty(alpha.alpha_2_direction)}</dd></div><div className="kv"><dt>Joint alpha</dt><dd>{pretty(alpha.joint_alpha_direction)}</dd></div><div className="kv"><dt>Signal persistence</dt><dd>{alpha.consecutive_confirmation_count} snapshots</dd></div><div className="kv"><dt>Quality</dt><dd>{alpha.evidence_quality}</dd></div><div className="kv"><dt>Status</dt><dd>{pretty(alpha.status)}</dd></div></div>
    {alpha.warnings.length>0&&<p className="warning">{alpha.warnings.map(pretty).join(" · ")}</p>}
    <details><summary>Research diagnostics</summary><dl className="kv-grid">{details.map(([label,value])=><div className="kv" key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl><p className="metric-detail">Signal persistence uses overlapping observations; it is not independent confirmation.</p></details>
  </>;
}

export function Dashboard() {
  const loader=useCallback(()=>api.dashboard(),[]);
  const { data, error, loading }=usePolling(loader);
  const [oiMode,setOiMode]=useState<"OI"|"DOI">("OI");
  const support=data?.features?.support_resistance.potential_support_clusters[0]?.center_strike;
  const resistance=data?.features?.support_resistance.potential_resistance_clusters[0]?.center_strike;
  const health=data?freshness(data):null;
  const lastMark=data?.open_shadow_marks.at(-1);
  const confirmation=data?.risk?.decisions[0]?.checks.find(check=>check.check_code==="CONSECUTIVE_REGIME_CONFIRMATION");
  const positioning=data?.features?.oi_features.contracts.filter(item=>item.positioning_class!=="INSUFFICIENT_DATA").slice(0,12) ?? [];
  const closedPnL=useMemo(()=>data?.shadow_trades.filter(item=>item.status==="CLOSED" && item.realized_pnl_per_lot!=null).map(item=>item.realized_pnl_per_lot as number) ?? [],[data]);
  if (loading && !data) return <main><div className="loading">LOADING RESEARCH TERMINAL</div></main>;
  return <main>
    <div className="page-head"><div><h1>Market Research Dashboard</h1><p>Deterministic NIFTY options research · Asia/Kolkata</p></div>{error && <Badge value="FAILED"/>}</div>
    {!data ? <><SectionError>MARKET DATA UNAVAILABLE</SectionError><div className="grid grid-2" style={{marginTop:12}}><Panel title="Pipeline health"><SectionError>PIPELINE DATA UNAVAILABLE</SectionError></Panel><Panel title="Shadow research"><SectionError>SHADOW DATA UNAVAILABLE</SectionError></Panel></div></> : <>
      <SessionOverview data={data}/>
      <section className="metric-strip" aria-label="Current market status">
        <Metric label="NIFTY Spot" value={number(data.snapshot?.nifty_spot)} detail={data.features?.price_structure_features.spot_change_pct_from_previous_snapshot == null ? "Change N/A" : percent(data.features.price_structure_features.spot_change_pct_from_previous_snapshot)} />
        <Metric label="NIFTY Future" value={number(data.snapshot?.nifty_future)} detail={`Basis ${number(data.features?.futures_features.futures_basis)}`} />
        <Metric label="Futures basis" value={number(data.features?.futures_features.futures_basis)} detail={percent(data.features?.futures_features.futures_basis_pct)} />
        <Metric label="India VIX" value={number(data.snapshot?.india_vix)} detail={data.features?.volatility_features.vix_regime ?? "Bucket N/A"} />
        <Metric label="Current expiry" value={data.snapshot ? new Intl.DateTimeFormat("en-GB",{day:"2-digit",month:"short"}).format(new Date(`${data.snapshot.expiry}T00:00:00+05:30`)) : "N/A"} />
        <Metric label="Last snapshot" value={timeIST(data.snapshot?.timestamp_ist)} detail={`Age ${health?.age}`} />
        <Metric label="Data freshness" value={health?.label ?? "NO DATA"} tone={health?.tone} detail={`${data.system.collector_interval_minutes} min expected`} />
        <Metric label="Pipeline" value={data.pipeline?.status ?? "NO RUN"} tone={data.pipeline?.status === "SUCCESS" ? "positive" : data.pipeline?.status === "FAILED" ? "negative" : "warning"} detail={data.pipeline ? `Snapshot ${data.pipeline.market_snapshot_id}` : undefined} />
      </section>

      <div className="grid grid-3">
        <Panel title="Deterministic market regime">
          {data.regime ? <><div className={`regime-value ${data.regime.regime === "BULLISH" ? "positive" : data.regime.regime === "BEARISH" ? "negative" : data.regime.regime === "RANGE" ? "accent" : "muted"}`}>{pretty(data.regime.regime)}</div><div className="kv-grid"><dl className="kv"><dt>Confidence</dt><dd>{percent(data.regime.confidence)}</dd></dl><dl className="kv"><dt>Evidence</dt><dd>{data.regime.evidence_quality}</dd></dl><dl className="kv"><dt>Confirmed</dt><dd>{confirmation?.status ?? "N/A"}</dd></dl></div>{[["Bull",data.regime.bull_score],["Bear",data.regime.bear_score],["Range",data.regime.range_score]].map(([label,value])=><div className="score-row" key={String(label)}><span>{label}</span><div className="track"><div className="fill" style={{width:`${Math.min(100,Number(value)*10)}%`}}/></div><span>{number(Number(value),1)}</span></div>)}<ul className="reason-list">{[...data.regime.bull_evidence,...data.regime.bear_evidence,...data.regime.range_evidence].slice(0,5).map(item=><li key={item}>{pretty(item)}</li>)}</ul>{data.regime.warnings.length>0&&<p className="warning">{data.regime.warnings.map(pretty).join(" · ")}</p>}</> : <Empty>NO REGIME RESULT</Empty>}
          {data.regime?.market_bias && <p className="metric-detail">Bias: {data.regime.market_bias} · Strength: {data.regime.directional_strength} · Families: {pretty(data.regime.strategy_family_eligibility ?? "NONE")}</p>}
          {data.regime?.signal_groups?.length ? <ul className="reason-list">{data.regime.signal_groups.map(group=><li key={group.name}>{pretty(group.name)}: {pretty(group.direction)}</li>)}</ul> : null}
          <p className="metric-detail">Confidence score is a deterministic evidence score, not a calibrated probability of profit.</p>
        </Panel>
        <Panel title="Statistical alpha">
          <AlphaPanel alpha={data.alpha}/>
        </Panel>
        <Panel title="Market structure">
          {data.features ? <dl className="kv-grid"><div className="kv"><dt>Spot / ATM</dt><dd>{number(data.features.spot)} / {number(data.features.atm_strike,0)}</dd></div><div className="kv"><dt>Support</dt><dd className="positive">{number(support,0)}</dd></div><div className="kv"><dt>Resistance</dt><dd className="negative">{number(resistance,0)}</dd></div><div className="kv"><dt>Local PCR OI</dt><dd>{number(data.features.pcr_features.local_pcr_oi)}</dd></div><div className="kv"><dt>Local PCR ΔOI</dt><dd>{number(data.features.pcr_features.local_pcr_oi_change)}</dd></div><div className="kv"><dt>Intraday OI</dt><dd><Badge value={data.features.data_quality.intraday_oi_usable?"SUCCESS":"FAILED"}/></dd></div><div className="kv"><dt>Futures basis</dt><dd>{number(data.features.futures_features.futures_basis)}</dd></div><div className="kv"><dt>VIX bucket</dt><dd>{data.features.volatility_features.vix_regime??"N/A"}</dd></div><div className="kv"><dt>Observed open</dt><dd>{number(data.features.price_structure_features.session_open_proxy)}</dd></div><div className="kv"><dt>Observed high / low</dt><dd>{number(data.features.price_structure_features.collector_observed_high)} / {number(data.features.price_structure_features.collector_observed_low)}</dd></div><div className="kv"><dt>VWAP</dt><dd>N/A</dd></div></dl> : <Empty>MARKET STRUCTURE UNAVAILABLE</Empty>}
        </Panel>
        <Panel title="Pipeline health">
          {data.pipeline ? <><div className="status-grid">{[["Feature",data.pipeline.feature_status],["Alpha",data.pipeline.alpha_status??"SKIPPED"],["Regime",data.pipeline.regime_status],["AI",data.pipeline.ai_status],["Strategy",data.pipeline.strategy_status],["Risk",data.pipeline.risk_status],["Shadow",data.pipeline.shadow_status]].map(([label,status])=><div className="status-cell" key={label}><span>{label}</span><Badge value={status}/></div>)}</div><ul className="reason-list"><li>Last success: {timeIST(data.pipeline_health.last_success?.completed_at)}</li><li>Last partial: {timeIST(data.pipeline_health.last_partial?.completed_at)}</li><li>Last failure: {timeIST(data.pipeline_health.last_failed?.completed_at)}</li></ul></> : <Empty>NO PIPELINE RUN</Empty>}
        </Panel>

        <Panel title="Strike-level OI structure" className="span-2" action={<div className="toggle"><button className={oiMode==="OI"?"active":""} onClick={()=>setOiMode("OI")}>OI</button><button disabled={!data.features?.data_quality.intraday_oi_usable} className={oiMode==="DOI"?"active":""} onClick={()=>setOiMode("DOI")}>ΔOI</button></div>}>
          {data.features?.oi_features.contracts.length ? <OIChart contracts={data.features.oi_features.contracts} mode={oiMode} atm={data.features.atm_strike} support={support} resistance={resistance}/> : <Empty>OPTION OI STRUCTURE UNAVAILABLE</Empty>}
        </Panel>
        <Panel title="Same-day statistical alpha" className="span-2">{data.alpha_history?.length ? <AlphaHistoryChart values={data.alpha_history}/> : <Empty>NO ALPHA HISTORY YET</Empty>}</Panel>
        <Panel title="Option positioning"><div className="table-wrap">{positioning.length ? <table><thead><tr><th>Strike</th><th>Type</th><th>LTP</th><th>OI</th><th>ΔOI</th><th>Positioning</th></tr></thead><tbody>{positioning.map((row,index)=><tr key={`${row.strike}-${row.option_type}-${index}`}><td>{number(row.strike,0)}</td><td>{row.option_type}</td><td>{number(row.ltp)}</td><td>{integer(row.open_interest)}</td><td>{integer(row.change_in_open_interest)}</td><td>{pretty(row.positioning_class)}</td></tr>)}</tbody></table> : <Empty>POSITIONING DATA UNAVAILABLE</Empty>}</div></Panel>

        <Panel title="Strategy candidates" className="span-2">
          {data.candidates?.candidates.length ? <CandidateTable candidates={data.candidates.candidates}/> : <Empty>NO ELIGIBLE CREDIT SPREAD{data.candidates?.reason_codes.length ? ` · ${data.candidates.reason_codes.map(pretty).join(" · ")}` : ""}</Empty>}
        </Panel>
        <Panel title="AI research · secondary">{data.ai_research?.market_view ? <><div className="kv-grid"><div className="kv"><dt>AI view</dt><dd>{data.ai_research.market_view}</dd></div><div className="kv"><dt>Confidence</dt><dd>{percent(data.ai_research.confidence)}</dd></div><div className="kv"><dt>Agreement</dt><dd>{pretty(data.ai_research.agreement_status??"N/A")}</dd></div></div><ul className="reason-list">{data.ai_research.key_observations?.slice(0,3).map(item=><li key={item}>{item}</li>)}{data.ai_research.risks?.slice(0,2).map(item=><li key={item}>Risk: {item}</li>)}{data.ai_research.missing_evidence?.slice(0,2).map(item=><li key={item}>Uncertainty: {item}</li>)}</ul><p className="metric-detail">{data.ai_research.model} · {data.ai_research.prompt_version} · Cost ${number(data.ai_research.estimated_cost_usd,6)}</p></> : <Empty>AI RESEARCH NOT RUN</Empty>}</Panel>

        <Panel title="Risk decisions" className="span-2"><RiskTable decisions={data.risk?.decisions ?? []}/></Panel>
        <Panel title="Open shadow trade">
          {data.open_shadow_trade ? <><Badge value="OPEN"/><dl className="kv-grid" style={{marginTop:14}}><div className="kv"><dt>Strategy</dt><dd>{pretty(data.open_shadow_trade.strategy_type)}</dd></div><div className="kv"><dt>Entry</dt><dd>{timeIST(data.open_shadow_trade.entry_timestamp)}</dd></div><div className="kv"><dt>Entry spot / expiry</dt><dd>{number(data.open_shadow_trade.entry_spot)} / {data.open_shadow_trade.expiry}</dd></div><div className="kv"><dt>Short / long</dt><dd>{number(data.open_shadow_trade.short_leg.strike,0)} / {number(data.open_shadow_trade.long_leg.strike,0)}</dd></div><div className="kv"><dt>Entry credit / basis</dt><dd>{number(data.open_shadow_trade.entry_credit)} / {pretty(data.open_shadow_trade.entry_pricing_basis)}</dd></div><div className="kv"><dt>Exit debit</dt><dd>{number(lastMark?.exit_debit)}</dd></div><div className="kv"><dt>P&L / unit</dt><dd className={(lastMark?.pnl_per_unit??0)>=0?"positive":"negative"}>{number(lastMark?.pnl_per_unit)}</dd></div><div className="kv"><dt>P&L / lot</dt><dd>{money(lastMark?.pnl_per_lot)}</dd></div><div className="kv"><dt>Max profit / loss</dt><dd>{money(data.open_shadow_trade.max_profit_per_lot)} / {money(data.open_shadow_trade.max_loss_per_lot)}</dd></div><div className="kv"><dt>MFE / MAE</dt><dd>{number(data.open_shadow_trade.mfe_per_unit)} / {number(data.open_shadow_trade.mae_per_unit)}</dd></div><div className="kv"><dt>Holding time</dt><dd>{data.open_shadow_trade.holding_minutes==null?"N/A":`${number(data.open_shadow_trade.holding_minutes,0)}m`}</dd></div><div className="kv"><dt>Target / stop</dt><dd>{number(data.open_shadow_thresholds?.profit_target_pnl_per_unit)} / {number(data.open_shadow_thresholds?.stop_loss_pnl_per_unit)}</dd></div></dl></> : <Empty>NO OPEN SHADOW TRADE</Empty>}
        </Panel>

        {data.open_shadow_trade && <Panel title="Shadow P&L path" className="span-2">{data.open_shadow_marks.length ? <PnlChart marks={data.open_shadow_marks} thresholds={data.open_shadow_thresholds}/> : <Empty>NO SHADOW MARKS YET</Empty>}</Panel>}
        <Panel title="Today’s regime history" className={data.open_shadow_trade?"":"span-2"}><div className="history-strip">{data.regime_history.length ? [...data.regime_history].reverse().map(item=><div className={`history-segment ${item.regime}`} key={`${item.snapshot_id}-${item.timestamp}`} title={`${timeIST(item.timestamp)} · ${item.regime} · ${percent(item.confidence)}`}><strong>{timeIST(item.timestamp).slice(0,5)}</strong><br/>{item.regime==="NO_TRADE"?"NO":item.regime.slice(0,4)}</div>) : <Empty>NO SNAPSHOTS TODAY</Empty>}</div></Panel>

        <Panel title="Daily research summary" className="span-2"><div className="kv-grid">{[["Snapshots",data.daily_summary.snapshots_collected],["Usable OI",data.daily_summary.snapshots_with_usable_oi],["Bullish",data.daily_summary.regime_counts.BULLISH],["Bearish",data.daily_summary.regime_counts.BEARISH],["Range",data.daily_summary.regime_counts.RANGE],["No trade",data.daily_summary.regime_counts.NO_TRADE],["Candidate sets",data.daily_summary.candidate_sets_created],["Approved",data.daily_summary.approved_risk_decisions],["Rejected",data.daily_summary.rejected_risk_decisions],["Shadow entries",data.daily_summary.shadow_entries],["Shadow exits",data.daily_summary.shadow_exits],["Realized P&L",money(data.daily_summary.shadow_realized_pnl)],["AI calls",data.daily_summary.ai_calls],["AI cost",`$${number(data.daily_summary.ai_cost,6)}`]].map(([label,value])=><div className="kv" key={String(label)}><dt>{label}</dt><dd>{value}</dd></div>)}</div></Panel>
        <Panel title="Historical shadow performance"><div className="kv-grid">{[["Total",data.shadow_performance.total_trades],["Closed",data.shadow_performance.closed_trades],["Win rate",percent(data.shadow_performance.per_lot.win_rate)],["Net P&L",money(data.shadow_performance.per_lot.net_pnl)],["Gross profit",money(data.shadow_performance.per_lot.gross_profit)],["Gross loss",money(data.shadow_performance.per_lot.gross_loss)],["Average win",money(data.shadow_performance.per_lot.average_win)],["Average loss",money(data.shadow_performance.per_lot.average_loss)],["Profit factor",number(data.shadow_performance.per_lot.profit_factor)],["Expectancy",money(data.shadow_performance.per_lot.expectancy_per_trade)],["Max drawdown",money(data.shadow_performance.per_lot.max_drawdown)],["Avg holding",data.shadow_performance.average_holding_minutes==null?"N/A":`${number(data.shadow_performance.average_holding_minutes,0)}m`]].map(([label,value])=><div className="kv" key={String(label)}><dt>{label}</dt><dd>{value}</dd></div>)}</div></Panel>
        {data.shadow_performance.closed_trades>0 && <><Panel title="Cumulative shadow P&L"><EquityChart values={closedPnL}/></Panel><Panel title="Shadow drawdown"><EquityChart values={closedPnL} drawdown/></Panel><Panel title="Win / loss by strategy"><BreakdownChart data={Object.entries(data.shadow_breakdown.strategy_type??{}).map(([name,item])=>({name,wins:item.per_lot.wins,losses:item.per_lot.losses}))}/></Panel><Panel title="Performance by VIX regime"><BreakdownChart data={Object.entries(data.shadow_breakdown.vix_regime??{}).map(([name,item])=>({name,wins:item.per_lot.wins,losses:item.per_lot.losses}))}/></Panel><Panel title="Performance by confidence"><BreakdownChart data={Object.entries(data.shadow_breakdown.regime_confidence_bucket??{}).map(([name,item])=>({name,wins:item.per_lot.wins,losses:item.per_lot.losses}))}/></Panel></>}
      </div>
    </>}
  </main>;
}

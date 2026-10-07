"use client";

import { useCallback } from "react";
import { api } from "@/lib/api";
import { ageLabel, ageSeconds, money, number, pretty, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Panel } from "@/components/ui";
import type {
  DashboardData, DetailedSystemHealth, MarketSnapshot, OperationalEvent, PipelineRun,
  RiskDecision, RiskEvaluationSet, StrategyCandidateSet, SystemMetrics,
} from "@/types";

type CandidateSummary = Pick<StrategyCandidateSet, "snapshot_id" | "candidate_count" | "eligible" | "strategy_type" | "strategy_version" | "created_at">;
type SessionSupport = {
  health: DetailedSystemHealth | null;
  metrics: SystemMetrics | null;
  events: OperationalEvent[];
  snapshots: MarketSnapshot[];
  pipelines: PipelineRun[];
  candidates: CandidateSummary[];
  risks: RiskEvaluationSet[];
};

const unavailable = "—";
const reasonLabels: Record<string,string> = {
  ZERO_DTE_EXCLUDED: "Expiry-day entries excluded",
  MISSING_BID: "Executable bid unavailable",
  MISSING_ASK: "Executable ask unavailable",
  QUOTE_STALE: "Executable quote is stale",
  INSUFFICIENT_CREDIT: "Credit is below the required minimum",
  NO_CANDIDATE: "No eligible spread candidate",
  NO_STRATEGY_CANDIDATE: "No eligible spread candidate",
  MAX_LOSS_PER_TRADE: "Maximum loss exceeds the risk limit",
};

const istParts = (value:string) => Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
  timeZone:"Asia/Kolkata", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false,
}).formatToParts(new Date(value)).map(item=>[item.type,item.value]));
const dateIST = (value:string) => { const p=istParts(value); return `${p.year}-${p.month}-${p.day}`; };
const minuteIST = (value:string) => { const p=istParts(value); return `${p.hour}:${p.minute}`; };
const safePercent = (value:number|null|undefined) => value == null ? unavailable : `${number(value,1)}%`;
const reasonLabel = (code:string) => reasonLabels[code] ?? pretty(code).toLowerCase().replace(/^./, c=>c.toUpperCase());
const unique = (values:string[]) => [...new Set(values.filter(Boolean))];

async function optional<T>(promise:Promise<T>, fallback:T):Promise<T> { try { return await promise; } catch { return fallback; } }

function marketSession(data:DashboardData, health:DetailedSystemHealth|null) {
  if (health?.market_session_state) return health.market_session_state.replace("_","-");
  const p=istParts(data.system.server_time_ist); const minute=Number(p.hour)*60+Number(p.minute);
  const weekday=new Intl.DateTimeFormat("en-US",{timeZone:"Asia/Kolkata",weekday:"short"}).format(new Date(data.system.server_time_ist));
  if (["Sat","Sun"].includes(weekday)) return "CLOSED";
  if (minute<555) return "PRE-MARKET";
  return minute<=930 ? "OPEN" : "CLOSED";
}

function systemMode(data:DashboardData):"CAPTURE ONLY"|"DECISION ONLY"|"PAPER"|"LIVE"|"UNAVAILABLE" {
  const explicit=(data.execution_mode??data.risk?.execution_mode??data.candidates?.execution_mode)?.replaceAll("_"," ").toUpperCase();
  if (explicit==="CAPTURE ONLY"||explicit==="DECISION ONLY"||explicit==="PAPER"||explicit==="LIVE") return explicit;
  return "UNAVAILABLE";
}

function StatusHeader({data,support}:{data:DashboardData;support:SessionSupport|null}) {
  const mode=systemMode(data); const session=marketSession(data,support?.health??null);
  const strategyVersion=data.candidates?.strategy_logic_version ?? data.candidates?.candidates[0]?.strategy_logic_version ?? data.candidates?.strategy_version ?? unavailable;
  const age=data.snapshot ? ageLabel(ageSeconds(data.snapshot.timestamp_ist,new Date(data.system.server_time_ist).getTime())) : unavailable;
  return <>
    {mode==="DECISION ONLY"&&<div className="safety-banner">Research mode — decisions are recorded, no simulated or broker orders are being placed.</div>}
    {mode==="PAPER"&&<div className="safety-banner paper">Paper execution — simulated fills from observed market data. No broker orders.</div>}
    <section className="session-status" aria-label="Session status">
      <div className="session-primary"><span>Market session</span><strong className={`session-state ${session}`}>{session}</strong></div>
      <div className={`session-primary mode-${mode.replaceAll(" ","-")}`}><span>System mode</span><strong>{mode}</strong></div>
      <div><span>Latest snapshot</span><strong>{data.snapshot?timeIST(data.snapshot.timestamp_ist):unavailable}</strong><small>{data.snapshot?`${age} old`:"Unavailable"}</small></div>
      <div><span>NIFTY spot</span><strong>{data.snapshot?number(data.snapshot.nifty_spot):unavailable}</strong></div>
      <div><span>India VIX</span><strong>{data.snapshot?.india_vix==null?unavailable:number(data.snapshot.india_vix)}</strong></div>
      <div><span>Nearest expiry</span><strong>{data.snapshot?.expiry??unavailable}</strong></div>
      <div><span>Strategy version</span><strong>{strategyVersion}</strong></div>
      <div><span>Pipeline</span><strong><Badge value={data.pipeline?.status??"UNAVAILABLE"}/></strong></div>
    </section>
  </>;
}

function DataHealth({data,support}:{data:DashboardData;support:SessionSupport|null}) {
  const collector=support?.health?.collector; const quality=support?.health?.market_data;
  const options=data.snapshot?.options??[];
  const timestamped=options.filter(item=>item.source_market_timestamp!=null).length;
  const quoteTimestampCoverage=options.length ? timestamped/options.length*100 : null;
  const observedBidAskCoverage=options.length ? options.filter(item=>item.bid!=null&&item.ask!=null).length/options.length*100 : null;
  const depthUnits=unique(options.map(item=>item.depth_unit??"UNKNOWN"));
  const depthUnit=depthUnits.length===0?unavailable:depthUnits.length===1?depthUnits[0]:"MIXED";
  const depthStatus=depthUnit==="UNKNOWN"||depthUnit==="MIXED"?"Depth capacity unverified":depthUnit===unavailable?"Unavailable":"Broker quantity semantics available";
  const expected=collector?.expected_snapshots_so_far_today??support?.metrics?.snapshots_expected;
  const captured=collector?.actual_snapshots_today??support?.metrics?.snapshots_actual??data.daily_summary.snapshots_collected;
  const missing=collector?.missing_snapshot_estimate??(expected==null?null:Math.max(0,expected-captured));
  const items:Array<[string,string,boolean?]>=[
    ["Snapshot cadence",`${data.system.collector_interval_minutes} minutes`],
    ["Expected snapshots",expected==null?unavailable:String(expected)],
    ["Captured snapshots",String(captured)],
    ["Missing snapshots",missing==null?unavailable:String(missing),Boolean(missing)],
    ["Last successful capture",timeIST(collector?.data_freshness.last_snapshot_at??data.snapshot?.timestamp_ist)],
    ["Bid / ask coverage",safePercent(quality?.bid_ask_coverage_pct??observedBidAskCoverage)],
    ["Quote timestamp availability",safePercent(quoteTimestampCoverage)],
    ["Depth status",depthStatus,depthStatus==="Depth capacity unverified"],
    ["Depth unit",depthUnit,depthUnit==="UNKNOWN"||depthUnit==="MIXED"],
  ];
  return <Panel title="Data health"><div className="health-matrix">{items.map(([label,value,warn])=><div className="health-row" key={label}><span>{label}</span><strong className={warn?"warning":""}>{value}</strong></div>)}</div></Panel>;
}

function DecisionFunnel({data,support,mode}:{data:DashboardData;support:SessionSupport|null;mode:ReturnType<typeof systemMode>}) {
  const snapshotIds=new Set((support?.snapshots??[]).filter(item=>dateIST(item.timestamp_ist)===data.daily_summary.date).map(item=>item.id));
  const candidates=(support?.candidates??[]).filter(item=>snapshotIds.has(item.snapshot_id));
  const eligible=candidates.filter(item=>item.eligible).length;
  const candidateCount=candidates.reduce((sum,item)=>sum+item.candidate_count,0);
  const values:Array<[string,string|number]>=[
    ["Snapshots",data.daily_summary.snapshots_collected], ["Eligible windows",support?eligible:unavailable],
    ["Candidates",support?candidateCount:unavailable], ["Risk approved",data.daily_summary.approved_risk_decisions],
    ["Entry pending",data.forward_paper?.pending??unavailable], ["Filled",unavailable], ["Open",data.forward_paper?.open??unavailable], ["Closed",unavailable],
  ];
  return <Panel title="Decision funnel" className="span-2"><div className="decision-funnel">{values.map(([label,value],index)=><div className="funnel-stage" key={label}><span>{label}</span><strong>{value}</strong>{index<values.length-1&&<i aria-hidden="true">→</i>}</div>)}</div>{mode==="DECISION ONLY"&&<p className="execution-note">Execution disabled — decision research only</p>}{mode==="UNAVAILABLE"&&<p className="execution-note">Execution state unavailable — fill stages are not inferred.</p>}</Panel>;
}

function Reasons({codes}:{codes:string[]}) { return codes.length?<ul className="decision-reasons">{codes.map(code=><li key={code}><span>{reasonLabel(code)}</span><code>{code}</code></li>)}</ul>:<span className="muted">No rejection reasons recorded</span>; }

function LatestDecision({data}:{data:DashboardData}) {
  const decision:RiskDecision|undefined=data.risk?.decisions.find(item=>item.decision==="APPROVED")??data.risk?.decisions[0];
  const candidate=data.candidates?.candidates.find(item=>item.candidate_id===decision?.candidate_reference)??data.candidates?.candidates[0];
  const codes=unique([...(decision?.failed_checks??[]),...(decision?.reason_codes??[]),...(candidate?[]:(data.candidates?.reason_codes??[]))]);
  return <Panel title="Latest decision">
    {!data.risk&&!data.candidates?<Empty>NO DECISION RECORDED</Empty>:<>
      <div className="latest-decision-head"><div><span>Decision time</span><strong>{timeIST(data.regime?.timestamp??data.pipeline?.completed_at??data.snapshot?.timestamp_ist)}</strong></div><Badge value={decision?.decision??(data.candidates?.eligible?"CANDIDATE":"NO_CANDIDATE")}/></div>
      <dl className="decision-grid">
        <div><dt>Directional bias</dt><dd>{pretty(data.regime?.market_bias??data.regime?.regime??unavailable)}</dd></div>
        <div><dt>Strategy</dt><dd>{pretty(candidate?.strategy_type??decision?.candidate_strategy??data.candidates?.strategy_type??unavailable)}</dd></div>
        <div><dt>Expiry</dt><dd>{data.snapshot?.expiry??unavailable}</dd></div>
        <div><dt>Short / long strike</dt><dd>{candidate?`${number(candidate.short_leg.strike,0)} / ${number(candidate.long_leg.strike,0)}`:unavailable}</dd></div>
        <div><dt>Spread width</dt><dd>{candidate?number(candidate.spread_width,0):unavailable}</dd></div>
        <div><dt>Expected credit</dt><dd>{candidate?number(candidate.net_credit):unavailable}</dd></div>
        <div><dt>Max risk / lot</dt><dd>{decision?.max_loss_per_lot==null?unavailable:money(decision.max_loss_per_lot)}</dd></div>
        <div><dt>Regime</dt><dd>{pretty(data.regime?.regime??unavailable)}</dd></div>
      </dl>
      <div className="decision-reason-block"><span>Rejection reason(s)</span><Reasons codes={codes}/></div>
    </>}
  </Panel>;
}

function RejectionBreakdown({data,support}:{data:DashboardData;support:SessionSupport|null}) {
  const snapshotIds=new Set((support?.snapshots??[]).filter(item=>dateIST(item.timestamp_ist)===data.daily_summary.date).map(item=>item.id));
  const counts=new Map<string,number>();
  (support?.risks??[]).filter(item=>snapshotIds.has(item.snapshot_id)).flatMap(item=>item.decisions).forEach(item=>{
    unique([...(item.failed_checks??[]),...(item.reason_codes??[])]).forEach(code=>counts.set(code,(counts.get(code)??0)+1));
  });
  const rows=[...counts].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));
  return <Panel title="Today’s rejection reasons">{!support?<Empty>REJECTION DATA UNAVAILABLE</Empty>:rows.length?<ol className="rejection-ranking">{rows.map(([code,count])=><li key={code}><div><strong>{reasonLabel(code)}</strong><code>{code}</code></div><b>{count}</b></li>)}</ol>:<Empty>NO REJECTIONS RECORDED TODAY</Empty>}</Panel>;
}

const clockToMinute=(value:string)=>{const [h,m]=value.split(":").map(Number);return h*60+m;};
const minuteToClock=(value:number)=>`${String(Math.floor(value/60)).padStart(2,"0")}:${String(value%60).padStart(2,"0")}`;
function SessionTimeline({data,support}:{data:DashboardData;support:SessionSupport|null}) {
  const start=clockToMinute(data.system.collector_start_time); const end=clockToMinute(data.system.collector_end_time); const step=data.system.collector_interval_minutes;
  const buckets=Array.from({length:Math.floor((end-start)/step)+1},(_,index)=>minuteToClock(start+index*step));
  const snapshots=(support?.snapshots??[]).filter(item=>dateIST(item.timestamp_ist)===data.daily_summary.date);
  const snapshotByTime=new Map(snapshots.map(item=>[minuteIST(item.timestamp_ist),item]));
  const pipelineBySnapshot=new Map((support?.pipelines??[]).map(item=>[item.market_snapshot_id,item]));
  const riskBySnapshot=new Map((support?.risks??[]).map(item=>[item.snapshot_id,item]));
  const candidateIds=new Set((support?.candidates??[]).map(item=>item.snapshot_id));
  const fills=new Set(data.shadow_trades.filter(item=>dateIST(item.entry_timestamp)===data.daily_summary.date).map(item=>item.market_snapshot_id_entry));
  const errorSnapshotIds=new Set((support?.events??[]).filter(item=>["ERROR","CRITICAL"].includes(item.severity)).map(item=>item.snapshot_id).filter((id):id is number=>id!=null));
  const nowDate=dateIST(data.system.server_time_ist); const nowMinute=clockToMinute(minuteIST(data.system.server_time_ist));
  const cells=buckets.map(time=>{const snapshot=snapshotByTime.get(time);let state="missing";let label="Missing observation";
    if (data.daily_summary.date===nowDate&&clockToMinute(time)>nowMinute){state="future";label="Not due yet";}
    if(snapshot){state="captured";label="Captured";const pipeline=pipelineBySnapshot.get(snapshot.id);const risk=riskBySnapshot.get(snapshot.id);
      if(candidateIds.has(snapshot.id)){state="attempt";label="Decision attempt";}
      if((risk?.approved_count??0)>0){state="approved";label="Risk approved";}
      if(fills.has(snapshot.id)){state="fill";label="Legacy shadow entry (not forward paper)";}
      if(pipeline?.status==="FAILED"||errorSnapshotIds.has(snapshot.id)){state="error";label="Pipeline error";}
    }
    return {time,state,label};});
  return <Panel title="Session timeline" className="span-2"><div className="timeline-legend"><span className="captured">Capture</span><span className="attempt">Decision</span><span className="approved">Approved</span><span className="fill">Fill</span><span className="error">Error</span><span className="missing">Missing</span></div>{support?<><div className="session-timeline" aria-label="Intraday session timeline">{cells.map(cell=><span key={cell.time} className={cell.state} title={`${cell.time} · ${cell.label}`} aria-label={`${cell.time} ${cell.label}`}/>)}</div><div className="timeline-axis"><span>{buckets[0]}</span><span>12:00</span><span>{buckets.at(-1)}</span></div></>:<Empty>SESSION TIMELINE UNAVAILABLE</Empty>}</Panel>;
}

function PaperArea({mode,data}:{mode:ReturnType<typeof systemMode>;data:DashboardData}) {
  const paper=data.forward_paper;
  return <Panel title="Paper trade monitor" className="span-2"><div className="paper-state-row">{(["pending","open","closed","unresolved"] as const).map(state=><div key={state}><span>{pretty(state)}</span><strong>{paper?.[state]??unavailable}</strong><small>{paper?"Forward paper · all sessions":"Not exposed by API"}</small></div>)}</div>
    {mode==="DECISION ONLY"&&<p className="execution-note">Execution disabled — no paper fills or broker orders are being placed.</p>}
    {mode==="UNAVAILABLE"&&<p className="execution-note">Paper execution state is not exposed by the current API.</p>}
    {!paper&&<Empty>FORWARD PAPER TRADE STATE UNAVAILABLE · LEGACY SHADOW RECORDS ARE NOT USED</Empty>}
    {paper&&<p className="execution-note">Observed-book simulation only. Unknown depth blocks fills. No profitability claim.</p>}
  </Panel>;
}

export function SessionOverview({data}:{data:DashboardData}) {
  const loader=useCallback(async():Promise<SessionSupport>=>{
    const [health,metrics,events,snapshots,pipelines,candidates,risks]=await Promise.all([
      optional(api.systemHealth(),null), optional(api.systemMetrics(),null), optional(api.systemEvents(500),[]),
      optional(api.snapshots(500),[]), optional(api.pipelines(500),[]), optional(api.candidatesList(500),[]), optional(api.riskList(500),[]),
    ]);
    return {health,metrics,events,snapshots,pipelines,candidates,risks};
  },[]);
  const {data:support}=usePolling(loader,30000); const mode=systemMode(data);
  return <div className="session-overview">
    <StatusHeader data={data} support={support}/>
    <div className="grid grid-3 session-grid"><DataHealth data={data} support={support}/><DecisionFunnel data={data} support={support} mode={mode}/><LatestDecision data={data}/><RejectionBreakdown data={data} support={support}/><SessionTimeline data={data} support={support}/><PaperArea mode={mode} data={data}/></div>
  </div>;
}

"use client";
import { ResponsiveTable } from "@/components/responsive-table";
import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/hooks/use-polling";
import { dateIST, integer, money, number, percent, timeIST } from "@/lib/format";
import { Badge, Empty, Metric, Panel, SectionError } from "@/components/ui";
import { Icon } from "@/components/icons";
import type { CommodityInstrument } from "@/types/commodity-acts";
import type { PaperDecision, PaperKind, PaperRecord } from "@/types/commodity-paper";

const stamp = (at?: string) => at && Number.isFinite(Date.parse(at)) ? `${dateIST(at)} ${timeIST(at)}` : "N/A";

function PaperStatus({ instrument }: { instrument: CommodityInstrument }) {
  const loader = useCallback(() => api.commodityPaperSummary(), []);
  const { data, error } = usePolling(loader, 15000);
  if (error) return <SectionError>COMMODITY PAPER STATUS UNAVAILABLE</SectionError>;
  if (!data) return <Empty>LOADING PAPER STATUS</Empty>;
  const row = data.instruments?.find(item => item.instrument_id === instrument);
  if (!row) return <Empty>PAPER STATE UNAVAILABLE</Empty>;
  const latest = row.latest;
  const margin = latest?.candidate?.construction_evidence?.margin;
  const signal = latest?.decision;
  const directionalScore = signal?.directional_score;

  return <><div className="instrument-hero"><div className="instrument-identity"><p className="eyebrow"><Icon name={instrument === "CRUDEOILM" ? "flame" : "gas"} size={15}/>{instrument} · Selected MCX future</p><h2>{latest?.future_symbol ?? "No captured contract"}</h2><div className="instrument-price">{number(latest?.future_price)}</div><p>Observed future reference · not a live executable option price</p><div className="source-status" style={{ marginTop: 16 }}><Badge value={row.readiness ?? row.status ?? "UNAVAILABLE"}/>{latest && <Badge value={row.observation_freshness ?? "UNAVAILABLE"}/>}<Badge value={row.market_open == null ? row.session_configured === false ? "SESSION_UNCONFIGURED" : "SESSION_UNAVAILABLE" : row.market_open ? "MARKET_OPEN" : "MARKET_CLOSED"}/></div></div>
      <div className="evidence-summary"><div className="evidence-row"><span>Directional state</span><strong>{signal?.direction ?? "Unavailable"}</strong></div><div className="evidence-row"><span>Directional context score</span><strong>{number(directionalScore, 1)} / {number(signal?.directional_score_max, 0)}</strong></div>{typeof directionalScore === "number" && Number.isFinite(directionalScore) ? <div className="risk-track"><span style={{ width: `${Math.max(0, Math.min(100, directionalScore))}%` }}/></div> : <p className="metric-detail">Directional score unavailable</p>}<div className="evidence-row"><span>Commodity entry qualification</span><Badge value={signal?.entry_qualification?.status ?? "UNAVAILABLE"}/></div><div className="evidence-row"><span>Option expiry</span><strong>{latest?.option_expiry ?? "Unavailable"}</strong></div><div className="evidence-row"><span>Execution evidence</span><Badge value={row.execution_evidence?.source ?? "UNVERIFIED"}/></div><div className="evidence-row"><span>Reserved stress risk</span><strong>{money(row.reserved_stress_risk)}</strong></div><p className="metric-detail" style={{ margin: 0 }}>Stress reservation is not guaranteed maximum loss. Budget utilization is unavailable unless the API exposes the approved limit.</p></div></div>
    <div className="compact-metrics"><Metric label="Realized gross today" value={money(row.realized_gross_today)} detail="Stored paper exits · INR"/><Metric label="Unrealized gross" value={money(row.unrealized_gross)} detail="No inferred marks"/><Metric label="Open positions" value={integer(row.open_positions)} detail={`Pending entries: ${integer(row.pending_entries)}`}/><Metric label="Completed trades" value={integer(row.closed_trades)} detail={`Rejected entries: ${integer(row.rejected_entries)}`}/></div>
    {row.configuration_blockers?.length ? <p className="warning" role="status">Configuration: {row.configuration_blockers.map(value => value.replaceAll("_", " ")).join(" · ")}</p> : null}
    {!latest && <Empty>NO OBSERVATIONS YET</Empty>}
    <p>Paper entry activation: {row.paper_enabled == null ? "UNAVAILABLE" : row.paper_enabled ? "ENABLED" : "DISABLED"}. Existing exposure still requires running capture workers for recovery/exit management.</p>
    <p>Market session: {row.market_open == null ? "UNAVAILABLE" : row.market_open ? "OPEN" : "CLOSED"}. Worker process state: UNAVAILABLE (not inferred from activity).</p>
    <details className="instrument-evidence-disclosure"><summary>Contract, execution, risk & accounting evidence</summary>
    <dl className="commodity-fields">
      <div><dt>Selected future / price</dt><dd>{latest?.future_symbol ?? "N/A"} / {number(latest?.future_price)}</dd></div>
      <div><dt>Future / option expiry</dt><dd>{latest?.future_expiry ?? "N/A"} / {latest?.option_expiry ?? "N/A"}</dd></div>
      <div><dt>Automatic contract evidence renewal</dt><dd>{latest?.contract_renewal_status ?? "UNAVAILABLE"}</dd></div>
      <div><dt>Bid-ask / raw depth coverage</dt><dd>{percent(latest?.data_quality?.bid_ask_coverage == null ? null : latest.data_quality.bid_ask_coverage*100)} / {percent(latest?.data_quality?.depth_coverage == null ? null : latest.data_quality.depth_coverage*100)}</dd></div>
      <div><dt>Broker reference timestamp</dt><dd>{stamp(latest?.source_market_timestamp ?? undefined)}</dd></div>
      <div><dt>Observation freshness / age seconds</dt><dd>{row.observation_freshness ?? "UNAVAILABLE"} / {number(row.observation_age_seconds, 1)}</dd></div>
      <div><dt>ACTS direction / market state</dt><dd>{latest?.decision?.direction ?? "N/A"} / {latest?.decision?.market_state ?? "N/A"}</dd></div>
      <div><dt>Directional context band</dt><dd>{signal?.score_band ?? "N/A"}</dd></div>
      {signal?.opportunity_score != null && <div><dt>Historical compatibility score (legacy)</dt><dd>{number(signal.opportunity_score)} / 100</dd></div>}
      <div><dt>Pending entries / open positions</dt><dd>{integer(row.pending_entries)} / {integer(row.open_positions)}</dd></div>
      <div><dt>Pending exits / unresolved exposure</dt><dd>{integer(row.pending_exits)} / {integer(row.unresolved_exposure)}</dd></div>
      <div><dt>Rejected entries / completed trades</dt><dd>{integer(row.rejected_entries)} / {integer(row.closed_trades)}</dd></div>
      <div><dt>Wins / losses / sampled sessions</dt><dd>{integer(row.wins)} / {integer(row.losses)} / {integer(row.sample_sessions)}</dd></div>
      <div><dt>Cumulative realized gross</dt><dd>{money(row.realized_gross)}</dd></div>
      <div><dt>Realized net (configured costs only)</dt><dd>{money(row.realized_net)}</dd></div>
      <div><dt>Fresh unrealized gross / net</dt><dd>{money(row.unrealized_gross)} / {money(row.unrealized_net)}</dd></div>
      <div><dt>Reserved stress-risk estimate (not maximum loss)</dt><dd>{money(row.reserved_stress_risk)}</dd></div>
      <div><dt>Historical spread defined-risk reservation</dt><dd>{money(row.reserved_defined_risk)}</dd></div>
      <div><dt>Single short / underlying quantity / bid premium</dt><dd>{latest?.candidate?.strategy_type ?? "N/A"} / {integer(latest?.candidate?.contract_quantity)} / {number(latest?.candidate?.executable_credit)}</dd></div>
      <div><dt>Selected stress-loss scenario estimate</dt><dd>{money(latest?.candidate?.stress_loss_estimate)}</dd></div>
      <div><dt>Broker margin status</dt><dd>{margin?.status ?? row.margin_status ?? "MARGIN_UNVERIFIED"}</dd></div>
      <div><dt>Single-instrument broker margin estimate</dt><dd>{money(margin?.required_margin)}</dd></div>
      <div><dt>Premium / verified margin estimate (not ROC)</dt><dd>{percent(margin?.premium_to_verified_margin_estimate == null ? null : margin.premium_to_verified_margin_estimate*100)}</dd></div>
      <div><dt>Pre-expiry exits overdue</dt><dd>{integer(row.expiry_exit_overdue)}</dd></div>
      <div><dt>Confirmed contract lot / INR per point per lot</dt><dd>{integer(row.execution_evidence?.lot_size)} / {number(row.execution_evidence?.rupees_per_point_per_lot)}</dd></div>
      <div><dt>Execution depth semantics</dt><dd>{row.execution_evidence?.depth_unit ?? "UNVERIFIED"} (underlying units; historical raw UNKNOWN retained)</dd></div>
      <div><dt>Execution evidence source</dt><dd>{row.execution_evidence?.source ?? "UNVERIFIED"}</dd></div>
      <div><dt>Observation / processing</dt><dd>{stamp(latest?.observed_at)} / {stamp(latest?.processed_at)}</dd></div>
    </dl>
    <p>Risk blocks: {(latest?.risk_blocks ?? []).join(", ") || "N/A"}</p>
    <p>Entry blockers: {(latest?.decision?.blockers ?? []).join(", ") || "N/A"}</p>
    <p>Candidate rejection counts: {Object.entries(latest?.decision?.candidate_rejections ?? {}).map(([key, count]) => `${key}: ${count}`).join(", ") || "N/A"}</p>
    <Qualification signal={signal}/>
    <p>Directional context components (raw points): {Object.entries(signal?.directional_components ?? {}).map(([key, value]) => `${key}: ${number(value)}`).join(", ") || "N/A"}</p>
    </details>
    <p className="execution-note">SINGLE-LEG NAKED OPTION SELLING — PAPER ONLY. Loss is not bounded by a hedge; stops and stress scenarios are not guaranteed maximum losses. Entry sells at a later observed BID; marks and exits buy at subsequent observed ASK. Quotes must be fresh and depth sufficient; LTP is never a fill. Broker margin estimates are not actual blocked capital; unknown margin is not zero capital usage. Net is unavailable without a complete, dated cost model. Paper results are descriptive research, not a profitability claim.</p>
  </>;
}

function Qualification({ signal }: { signal?: PaperDecision | null }) {
  const qualification = signal?.entry_qualification;
  const evidence = qualification?.evidence;
  return <div aria-label="Commodity native qualification">
    <p>ACTS direction and execution qualification are separate. Context is normalized using only the original directional weights; no option execution points contribute.</p>
    <p>Qualification: {qualification?.version ?? "N/A"} · {qualification?.status ?? "UNAVAILABLE"}</p>
    <p>Selected candidate: {evidence?.option_token ?? "N/A"} · {evidence?.option_type ?? "N/A"} {number(evidence?.strike)}</p>
    <dl className="commodity-fields">
      <div><dt>OTM distance / observed steps</dt><dd>{number(evidence?.otm_distance_points)} / {number(evidence?.observed_strike_step && evidence.otm_distance_points != null ? evidence.otm_distance_points / evidence.observed_strike_step : null)}</dd></div>
      <div><dt>Observed BID / ASK</dt><dd>{number(evidence?.bid)} / {number(evidence?.ask)}</dd></div>
      <div><dt>BID / ASK depth (units)</dt><dd>{integer(evidence?.bid_quantity_units)} / {integer(evidence?.ask_quantity_units)}</dd></div>
      <div><dt>OI / volume</dt><dd>{integer(evidence?.open_interest)} / {integer(evidence?.volume)}</dd></div>
      <div><dt>Source quote age seconds / DTE</dt><dd>{number(evidence?.source_age_seconds, 1)} / {integer(evidence?.dte_calendar_days)}</dd></div>
      <div><dt>Bid-ask spread</dt><dd>{percent(evidence?.bid_ask_spread_pct)}</dd></div>
      <div><dt>Premium / stress-loss estimate (not credit-to-width)</dt><dd>{number(evidence?.premium_to_stress_loss_estimate)}</dd></div>
      {Object.entries(qualification?.checks ?? {}).map(([key, check]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{number(check.value)} · {check.status} · minimum {number(check.minimum)}</dd></div>)}
    </dl>
    <p>Qualification rejections: {qualification?.reasons?.join(" · ") || "N/A"}</p>
    {signal?.candidate_evaluations?.length ? <details><summary>Candidate selection and rejections</summary>{signal.candidate_evaluations.map((item, index) => <p key={`${item.option_token}:${index}`}>{item.option_token} · {number(item.strike)} · {item.status} · {item.reason ?? "N/A"}</p>)}</details> : null}
  </div>;
}

function RecordRow({ row, index }: { row: PaperRecord; index: number }) {
  const candidate = row.candidate ?? row.latest?.candidate;
  const signal = row.decision ?? row.latest?.decision;
  const blocked = [...(row.latest?.risk_blocks ?? []), ...(signal?.blockers ?? [])].join(", ");
  return <tr><td>{stamp(row.observed_at ?? row.decision_timestamp)}<small>{row.id?.slice(0, 12) ?? row.sequence ?? index}</small></td>
    <td><Badge value={row.state ?? row.event_type ?? row.kind ?? "UNAVAILABLE"}/>{signal && <small>{signal.status ?? "N/A"} / {signal.direction ?? "N/A"} / {number(signal.directional_score ?? signal.opportunity_score)}{signal.directional_score != null ? " context" : " legacy"}</small>}</td>
    <td>{candidate?.short_leg?.trading_symbol ?? "N/A"}{candidate?.long_leg ? ` / ${candidate.long_leg.trading_symbol ?? "N/A"} (historical spread)` : candidate?.short_leg ? ` / ${candidate.strategy_type ?? "SINGLE SHORT"}` : ""}<small>{row.action ?? row.execution?.action ?? ""} / units {integer(row.contract_quantity ?? candidate?.contract_quantity)}</small></td>
    <td>{stamp(row.entry_timestamp)} / {stamp(row.exit_timestamp)}</td>
    <td>{number(row.entry_credit ?? (row.kind === "ENTRY" ? row.execution?.value : null))} / {number(row.exit_debit ?? (row.kind === "EXIT" ? row.execution?.value : null))}</td>
    <td>{money(row.gross_realized_pnl ?? row.unrealized_gross)} / {money(row.net_realized_pnl ?? row.unrealized_net)}</td>
    <td>{row.exit_reason ?? row.rejection_reason ?? row.unresolved_reason ?? row.last_attempt?.reason ?? row.reason ?? (blocked || "N/A")}
      <small>{row.margin_status ?? ""} {row.policy?.cost_source ?? ""}</small>{signal?.entry_qualification && <details><summary>Frozen entry qualification</summary><Qualification signal={signal}/></details>}</td></tr>;
}

function Ledger({ instrument, kind, offset, onPage }: { instrument: CommodityInstrument; kind: PaperKind; offset: number; onPage: (value: number) => void }) {
  const loader = useCallback(() => api.commodityPaperPage(instrument, kind, offset), [instrument, kind, offset]);
  const { data, error } = usePolling(loader, 15000);
  if (error) return <SectionError>COMMODITY PAPER LEDGER UNAVAILABLE</SectionError>;
  if (!data) return <Empty>LOADING PAPER LEDGER</Empty>;
  const rows = data.items ?? [];
  return <>{data.status === "MIGRATION_REQUIRED" ? <Empty>PAPER MIGRATION REQUIRED</Empty> : rows.length ?
    <div className="table-wrap"><ResponsiveTable aria-label="Commodity paper lifecycle ledger"><thead><tr><th>Observation / ID</th><th>State / event</th><th>Exact short contract / historical legs</th><th>Entry / exit time</th><th>SELL premium / BUY premium</th><th>Gross / net P&amp;L</th><th>Reason / evidence</th></tr></thead>
      <tbody>{rows.map((row, index) => <RecordRow key={row.id ?? row.sequence ?? index} row={row} index={index}/>)}</tbody></ResponsiveTable></div> : <Empty>NO PAPER RECORDS</Empty>}
    <div className="commodity-pagination"><button disabled={offset === 0} onClick={() => onPage(Math.max(0, offset-50))}>Previous paper page</button>
      <span>{rows.length} {kind} records</span><button disabled={!data.has_more || offset >= 100000} onClick={() => onPage(offset+50)}>Next paper page</button></div>
  </>;
}

export function CommodityPaper({ initialInstrument = "CRUDEOILM" }: { initialInstrument?: CommodityInstrument } = {}) {
  const [instrument, setInstrument] = useState<CommodityInstrument>(initialInstrument);
  const [kind, setKind] = useState<PaperKind>("positions");
  const [offset, setOffset] = useState(0);
  return <Panel title="COMMODITY ACTS SINGLE SHORT - PAPER ONLY - NO BROKER ORDERS"><div className="commodity-filter">
    <label>Paper instrument <select value={instrument} onChange={event => { setInstrument(event.target.value as CommodityInstrument); setOffset(0); }}>
      <option value="CRUDEOILM">CRUDEOILM</option><option value="NATGASMINI">NATGASMINI</option></select></label>
    <label>Paper records <select value={kind} onChange={event => { setKind(event.target.value as PaperKind); setOffset(0); }}>
      <option value="positions">Positions and trade history</option><option value="orders">Pending/rejected orders</option><option value="fills">Atomic fills</option><option value="events">Audit events</option></select></label>
    </div><PaperStatus key={instrument} instrument={instrument}/><Ledger key={`${instrument}:${kind}:${offset}`} instrument={instrument} kind={kind} offset={offset} onPage={setOffset}/></Panel>;
}

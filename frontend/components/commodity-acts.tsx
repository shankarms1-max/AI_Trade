"use client";
import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { dateIST, integer, number, percent, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Panel, SectionError } from "@/components/ui";
import type { CommodityDecision, CommodityInstrument } from "@/types/commodity-acts";

const stamp = (value?: string) => value ? `${dateIST(value)} · ${timeIST(value)}` : "N/A";
const coverage = (value?: number) => value == null ? "N/A" : percent(value * 100);

function Evidence({ data }: { data: CommodityDecision }) {
  const components = data.acts.score_components ?? {};
  return <details className="commodity-evidence"><summary>Score and market geometry evidence</summary>
    <p>Stage: {data.stage ?? "UNAVAILABLE"}. Research score classification only, not a trade signal or paper entry.</p>
    <p>ACTS event time: {stamp(data.acts.timestamp)}. Observed at: {stamp(data.observed_at)}. Processed at: {stamp(data.decided_at)}.</p>
    <p>Original 100-point weights · no rescaling · execution unavailable (maximum score without execution: {integer(data.maximum_score_without_execution)}).</p>
    <dl className="commodity-fields">{Object.entries(components).map(([name, value]) => <div key={name}><dt>{name.replaceAll("_", " ")}</dt><dd>{number(value)}</dd></div>)}</dl>
    <p>Unavailable: {data.unavailable_components?.join(", ") || "N/A"}</p>
    <p>Observed strikes: {data.geometry.strikes?.map(value => number(value)).join(" · ") || "N/A"}</p>
    <p>Adjacent gaps: {data.geometry.adjacent_gaps?.map(value => number(value)).join(" · ") || "N/A"}</p>
    <p>Median / max bid-ask spread: {percent(data.geometry.median_bid_ask_spread_pct)} / {percent(data.geometry.max_bid_ask_spread_pct)}</p>
    <p>OI / volume coverage: {coverage(data.geometry.oi_coverage)} / {coverage(data.geometry.volume_coverage)} · Depth unit: {data.geometry.depth_unit ?? "UNKNOWN"}</p>
    <p>Observed credit comparisons are quote differences, not approved spreads or fills.</p>
    {data.geometry.credit_geometry_summary?.length ? <div className="table-wrap"><table aria-label="Observed credit geometry"><thead><tr><th>Observed steps</th><th>Comparisons</th><th>Min credit</th><th>Max credit</th></tr></thead><tbody>
      {data.geometry.credit_geometry_summary.map(row => <tr key={row.observed_steps}><td>{row.observed_steps}</td><td>{integer(row.comparisons)}</td><td>{number(row.min_observed_credit)}</td><td>{number(row.max_observed_credit)}</td></tr>)}
    </tbody></table></div> : <p>N/A</p>}
  </details>;
}

export function CommodityOverview() {
  const loader = useCallback(() => api.commodityActsSummary(), []);
  const { data, error } = usePolling(loader, 20000);
  if (error) return <SectionError>COMMODITY RESEARCH UNAVAILABLE</SectionError>;
  if (!data) return <Empty>LOADING COMMODITY RESEARCH</Empty>;
  return <><p className="muted">As of {stamp(data.as_of)} · Broker process state is not inferred from captured activity.</p>
    <div className="commodity-grid">{data.instruments.map(instrument => {
      const latest = instrument.latest;
      return <Panel key={instrument.instrument_id} title={instrument.display_name} className="commodity-card">
        <Badge value={instrument.readiness}/><p>Worker configuration: {instrument.configured_enabled ? "ENABLED" : "DISABLED"} · Process state unavailable</p>
        {!instrument.session_configured && <p className="warning">VERIFIED MCX SESSION SETTINGS REQUIRED</p>}
        <p>These cards show V1 scoring/research evidence only. Use the single-short paper panel for authoritative entry and lifecycle state. New commodity spread entries are disabled.</p>
        <dl className="commodity-fields">
          <div><dt>New paper entry structure</dt><dd>SINGLE SHORT (PAPER POLICY REQUIRED)</dd></div>
          <div><dt>Depth quantity units</dt><dd>{latest?.geometry.depth_unit ?? "UNKNOWN"} {latest?.geometry.depth_unit === "UNITS" ? "(underlying units)" : "(unconfirmed)"}</dd></div>
          <div><dt>Option-to-future linkage</dt><dd>UNCONFIRMED: {latest?.data_provenance?.option_future_link ?? "No validated linkage"}</dd></div>
          <div><dt>Normalized tick-size semantics</dt><dd>UNCONFIRMED</dd></div>
          <div><dt>Future reference price</dt><dd>{number(latest?.future_price)}</dd></div>
          <div><dt>Reference basis</dt><dd>{latest?.reference_basis ?? "UNAVAILABLE"}</dd></div>
          <div><dt>Future contract / expiry</dt><dd>{latest?.future_instrument_id ?? "N/A"} / {latest?.future_expiry ?? "N/A"}</dd></div>
          <div><dt>Option expiry / lot size</dt><dd>{latest?.option_expiry ?? "N/A"} / {integer(latest?.lot_size)}</dd></div>
          <div><dt>ATM / observed strike step</dt><dd>{number(latest?.atm)} / {number(latest?.geometry.modal_strike_step)}</dd></div>
          <div><dt>Bid-ask / raw depth coverage</dt><dd>{coverage(latest?.geometry.bid_ask_coverage)} / {coverage(latest?.geometry.depth_coverage)}</dd></div>
          <div><dt>Alpha readiness</dt><dd>{latest?.alpha.validity_state ?? "UNAVAILABLE"}</dd></div>
          <div><dt>Alpha 1 / Alpha 2 state</dt><dd>{latest?.alpha.alpha_1_direction ?? "N/A"} / {latest?.alpha.alpha_2_direction ?? "N/A"}</dd></div>
          <div><dt>ACTS market state / direction</dt><dd>{latest?.acts.market_state ?? "N/A"} / {latest?.acts.direction ?? "N/A"}</dd></div>
          <div><dt>ACTS score / band</dt><dd>{number(latest?.acts.opportunity_score)} / 100 · {latest?.acts.score_band ?? "N/A"}</dd></div>
          <div><dt>Underlying ACTS status (entry disabled)</dt><dd>{latest?.acts.status ?? "NOT_CAPTURED"}</dd></div>
          <div><dt>Observations / decisions</dt><dd>{integer(instrument.observation_count)} / {integer(instrument.decision_count)}</dd></div>
          <div><dt>Latest observation</dt><dd>{stamp(latest?.observed_at)}</dd></div>
          <div><dt>Session ID</dt><dd>{latest?.session_id ?? "N/A"}</dd></div>
        </dl>
        <p className="commodity-blockers">Blockers: {[...(latest?.blockers ?? []), ...(latest?.acts.blockers ?? [])].join(" · ") || "N/A"}</p>
        {latest && <Evidence data={latest}/>}
      </Panel>;
    })}</div>
  </>;
}

function DecisionPage({ instrument, offset, onPage }: { instrument: CommodityInstrument; offset: number; onPage: (value: number) => void }) {
  const loader = useCallback(() => api.commodityActsDecisions(instrument, offset), [instrument, offset]);
  const { data, error } = usePolling(loader, 20000);
  if (error) return <SectionError>COMMODITY DECISION JOURNAL UNAVAILABLE</SectionError>;
  if (!data) return <Empty>LOADING DECISION JOURNAL</Empty>;
  return <>{data.items.length ? <div className="table-wrap"><table aria-label="Commodity shadow decision journal"><thead><tr>
    <th>Observation (IST)</th><th>Instrument</th><th>Readiness</th><th>Market state</th><th>Score / band</th><th>Direction</th><th>Shadow decision</th><th>Blockers / evidence</th>
  </tr></thead><tbody>{data.items.map(row => <tr key={`${row.instrument_id}:${row.snapshot_id}`}>
    <td>{stamp(row.observed_at)}</td><td>{row.instrument_id}</td><td>{row.readiness}</td><td>{row.acts.market_state ?? "N/A"}</td>
    <td>{number(row.acts.opportunity_score)} / 100 · {row.acts.score_band ?? "N/A"}</td><td>{row.acts.direction ?? "N/A"}</td>
    <td>{row.decision_kind ?? "SHADOW_DECISION"} · {row.acts.status ?? "N/A"}</td><td>{row.acts.blockers?.join(" · ") || "N/A"}<Evidence data={row}/></td>
  </tr>)}</tbody></table></div> : <Empty>NO CAPTURED COMMODITY DECISIONS</Empty>}
    <div className="commodity-pagination"><button disabled={!offset} onClick={() => onPage(Math.max(0, offset - data.limit))}>Previous</button>
      <span>{data.items.length} records · newest first · research only</span><button disabled={!data.has_more || offset + data.limit > 100000} onClick={() => onPage(offset + data.limit)}>Next</button></div>
  </>;
}

export function CommodityJournal() {
  const [instrument, setInstrument] = useState<CommodityInstrument>("CRUDEOILM");
  const [offset, setOffset] = useState(0);
  return <Panel title="COMMODITY DECISION JOURNAL"><label className="commodity-filter">Instrument
    <select value={instrument} onChange={event => { setInstrument(event.target.value as CommodityInstrument); setOffset(0); }}>
      <option value="CRUDEOILM">CRUDEOILM</option><option value="NATGASMINI">NATGASMINI</option>
    </select></label>
    <DecisionPage key={`${instrument}:${offset}`} instrument={instrument} offset={offset} onPage={setOffset}/>
  </Panel>;
}

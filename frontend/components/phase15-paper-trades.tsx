"use client";

import { ResponsiveTable } from "@/components/responsive-table";
import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { dateIST, integer, money, number, timeIST } from "@/lib/format";
import { observationState } from "@/lib/monitoring";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Panel, SectionError } from "@/components/ui";
import { blockerLabel } from "@/components/phase15-monitor";
import { OrbRange, OptionExecution } from "@/components/phase15-orb-evidence";
import type { Phase15PaperTrade, Phase15Quote, Phase15TradePage } from "@/types";

const STRATEGIES = [
  ["ACTS_V2", "ACTS V2"], ["ORB_15", "ORB 15"], ["ORB_30", "ORB 30"], ["ORB_60", "ORB 60"],
  ["EMA_PULLBACK", "EMA Pullback"], ["SUPERTREND_RSI_VOLUME", "Supertrend + RSI + Volume"],
];
const STATES = ["OPEN", "CLOSED", "ENTRY_REJECTED", "PENDING_PAPER_ENTRY", "UNRESOLVED_EXPOSURE"];
const timestamp = (value: string | null | undefined) => value ? `${dateIST(value)} · ${timeIST(value)}` : "N/A";
const duration = (value: number | null) => value == null ? "N/A" : `${integer(Math.floor(value / 60))}m ${integer(Math.floor(value % 60))}s`;
const tone = (value: number | null) => value == null ? "muted" : value < 0 ? "negative" : value > 0 ? "positive" : "";

function Gross({ trade }: { trade: Phase15PaperTrade }) {
  const value = trade.state === "CLOSED" ? trade.realized_gross_pnl :
    trade.state === "OPEN" && trade.mark_available ? trade.current_unrealized_gross_pnl : null;
  return <span className={tone(value)}>{money(value)}</span>;
}

function QuoteEvidence({ title, quotes }: { title: string; quotes: Record<string, Phase15Quote> }) {
  if (!Object.keys(quotes).length) return <p className="muted">{title}: N/A</p>;
  return <div className="phase15-evidence"><h4>{title}</h4><div className="table-wrap"><ResponsiveTable data-layout="book" aria-label={title}>
    <thead><tr><th>Leg / instrument</th><th>Side</th><th>Fill</th><th>Bid / Ask</th><th>Bid / Ask qty</th><th>Depth unit</th><th>Source timestamp</th><th>Tick</th></tr></thead>
    <tbody>{Object.entries(quotes).map(([leg, quote]) => <tr key={leg}>
      <td>{leg} · {quote.trading_symbol ?? quote.instrument_token ?? "N/A"}<small>{quote.exchange ?? "N/A"} · {quote.expiry ?? "N/A"} · {number(quote.strike, 0)}</small></td>
      <td>{quote.side ?? "N/A"}</td><td>{number(quote.fill_price)}</td><td>{number(quote.bid)} / {number(quote.ask)}</td>
      <td>{integer(quote.bid_quantity)} / {integer(quote.ask_quantity)}</td><td>{quote.depth_unit ?? "UNKNOWN"}</td>
      <td>{timestamp(quote.source_market_timestamp)}</td><td>{number(quote.tick_size)}</td>
    </tr>)}</tbody></ResponsiveTable></div></div>;
}

function ResearchDetails({ trade }: { trade: Phase15PaperTrade }) {
  return <details className="phase15-research"><summary>Research details · {trade.trade_id}</summary>
    {trade.strategy_features && <OrbRange title="Frozen entry ORB range" evidence={trade.strategy_features} frozen/>}
    <OptionExecution trade={trade}/>
    <dl className="phase15-trade-fields">
      <div><dt>Decision</dt><dd>{timestamp(trade.decision_timestamp)}</dd></div>
      <div><dt>Entry</dt><dd>{timestamp(trade.entry_timestamp)}</dd></div>
      <div><dt>Exit</dt><dd>{timestamp(trade.exit_timestamp)}</dd></div>
      <div><dt>Lot size / lots</dt><dd>{integer(trade.lot_size)} / {integer(trade.lots)}</dd></div>
      <div><dt>MFE / MAE (points)</dt><dd>{number(trade.mfe)} / {number(trade.mae)}</dd></div>
      <div><dt>Best gross points</dt><dd>{number(trade.best_gross_points)}</dd></div>
      <div><dt>Holding duration</dt><dd>{duration(trade.holding_seconds)}</dd></div>
      <div><dt>Entry reason</dt><dd title={trade.entry_reason ?? undefined}>{blockerLabel(trade.entry_reason)}</dd></div>
      <div><dt>Exit reason</dt><dd title={trade.exit_reason ?? undefined}>{trade.terminal_status ?? trade.exit_reason ?? "N/A"}</dd></div>
      <div><dt>Expiry terminalized (not a fill)</dt><dd>{timestamp(trade.terminalized_at)}</dd></div>
      <div><dt>Exit policy</dt><dd>{trade.exit_policy_version ?? "UNAVAILABLE"}</dd></div>
      <div><dt>Score / band</dt><dd>{number(trade.opportunity_score)} / {trade.score_band ?? "N/A"}</dd></div>
      <div><dt>Stored allowed widths</dt><dd>{trade.policy_allowed_widths.length ? trade.policy_allowed_widths.join(", ") : "N/A"}</dd></div>
      <div><dt>Latest mark attempt</dt><dd>{timestamp(trade.latest_mark_timestamp)}</dd></div>
      <div><dt>Mark status</dt><dd>{trade.mark_available ? "AVAILABLE" : "MARK UNAVAILABLE"}</dd></div>
      <div><dt>Rejection reason</dt><dd>{trade.rejection_reason ?? "N/A"}</dd></div>
      <div><dt>Unresolved / mark reason</dt><dd>{trade.unresolved_reason ?? trade.mark_unavailable_reason ?? "N/A"}</dd></div>
    </dl>
    <p className="phase15-policy-hash">Stored policy hash: <code>{trade.policy_hash ?? "N/A"}</code></p>
    <div className="phase15-evidence"><h4>Candidate rejection evidence</h4>
      {Object.keys(trade.candidate_rejections).length ? <ul>{Object.entries(trade.candidate_rejections).map(([reason, count]) =>
        <li key={reason}>{reason}: {integer(count)}</li>)}</ul> : <p className="muted">N/A</p>}
    </div>
    <div className="phase15-evidence"><h4>Mirror signal timeline</h4>
      {trade.mirror_diagnostics.length ? <ul>{trade.mirror_diagnostics.map(event => <li key={event.name}>
        <strong>{blockerLabel(event.name)}</strong> · {timestamp(event.observed_at)}
        {event.opportunity_score != null && <> · Score {number(event.opportunity_score)} / {event.score_band ?? "N/A"}</>}
        {event.mark_available != null && <> · {event.mark_available ? `Gross ${money(event.gross_pnl)}` : "MARK UNAVAILABLE"}</>}
      </li>)}</ul> : <p className="muted">NO STORED MIRROR EVENTS</p>}
    </div>
    <p>False breakout first observed: {timestamp(trade.false_breakout_first_at)}</p>
    <QuoteEvidence title="Entry quote evidence" quotes={trade.entry_quotes}/>
    <QuoteEvidence title="Exit quote evidence" quotes={trade.exit_quotes}/>
    <QuoteEvidence title="Latest mark quote evidence" quotes={trade.latest_mark_quotes}/>
    <p className="muted">GROSS_ONLY · No inferred prices, depth units or net costs.</p>
  </details>;
}

function Pagination({ page, onPage }: { page: Phase15TradePage; onPage: (offset: number) => void }) {
  return <div className="phase15-pagination">
    <button disabled={page.offset === 0} onClick={() => onPage(Math.max(0, page.offset - page.limit))}>Previous</button>
    <span>{page.items.length ? `${page.offset + 1}–${page.offset + page.items.length}` : "0 records"} · newest first</span>
    <button disabled={!page.has_more || page.offset + page.limit > 100000} onClick={() => onPage(page.offset + page.limit)}>Next</button>
  </div>;
}

export function PositionCard({trade}:{trade:Phase15PaperTrade}) {
  const markFreshness=observationState(trade.latest_mark_timestamp);
  const historical=trade.mark_available&&markFreshness!=="FRESH";
  const attention=trade.state==="UNRESOLVED_EXPOSURE"||trade.expiry_terminalization;
  return <article className={`phase15-position ${attention?"needs-attention":""}`} aria-label={`${trade.display_name} position ${trade.trade_id}`}>
    <header><div><h3>{trade.display_name}</h3><span>{trade.direction==="BEAR"?"BEAR CALL":trade.direction==="BULL"?"BULL PUT":"Direction N/A"}</span></div><Badge value={trade.state}/></header>
    {trade.terminal_status&&<p className="attention-text">{trade.terminal_status}</p>}
    <div className="position-mark"><span>{trade.state==="CLOSED"?"Realized gross":historical?"Last gross mark":"Gross mark"}</span><strong><Gross trade={trade}/></strong><small>{trade.mark_available?`${markFreshness} · Captured exact mark ${timestamp(trade.latest_mark_timestamp)}`:"MARK UNAVAILABLE"}</small></div>
    <div className="position-legs"><span>Short {trade.short_option_type??trade.entry_quotes.short?.option_type??"N/A"} <b>{number(trade.short_strike,0)}</b></span><span>Hedge {trade.hedge_option_type??trade.entry_quotes.long?.option_type??"N/A"} <b>{number(trade.hedge_strike,0)}</b></span></div>
    <dl className="position-prices"><div><dt>Entry credit</dt><dd>{money(trade.entry_credit)}</dd></div><div><dt>{historical?"Last marked debit":"Current debit"}</dt><dd>{money(trade.mark_available?trade.current_debit:null)}</dd></div></dl>
    <div className="position-meta"><span>Entry {timeIST(trade.entry_timestamp)}</span><span>Holding {duration(trade.holding_seconds)}</span></div>
    <p className="position-management">{trade.management?.requested_exit_reason?`Exit requested: ${blockerLabel(trade.management.requested_exit_reason)}`:(trade.exit_policy_version==="MIRROR_EXIT_V1"?"Exit rule: mirrored strategy signal":`Exit policy: ${trade.exit_policy_version??"N/A"}`)}{trade.management?.forced_exit_time_ist&&<> · Forced exit {trade.management.forced_exit_time_ist} IST</>}</p>
    {trade.unresolved_reason&&<p className="attention-text">{blockerLabel(trade.unresolved_reason)}</p>}
    <ResearchDetails trade={trade}/>
  </article>;
}

function PositionsPage({offset,onPage}:{offset:number;onPage:(offset:number)=>void}) {
  const loader=useCallback(()=>api.phase15Positions(offset),[offset]);
  const {data,error}=usePolling(loader,20000);
  if(error)return <SectionError>OPEN PAPER POSITIONS UNAVAILABLE · Exposure must not be assumed flat</SectionError>;
  if(!data)return <div className="position-skeleton" aria-busy="true">Loading exact paper positions…</div>;
  return <>
    {data.items.length?<div className="phase15-position-grid">{data.items.map(trade=><PositionCard trade={trade} key={`${trade.strategy_id}:${trade.trade_id}`}/>)}</div>:<Empty>NO OPEN PAPER POSITIONS</Empty>}
    {Boolean(data.offset||data.has_more)&&<Pagination page={data} onPage={onPage}/>}
  </>;
}
export function Phase15Positions(){const[offset,setOffset]=useState(0);return <section className="positions-section" id="open-positions" aria-label="Open paper positions"><header className="section-title"><h2>OPEN POSITIONS</h2><span>Exact held contracts · Gross only</span></header><PositionsPage key={offset} offset={offset} onPage={setOffset}/></section>;}

function todayIST() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

function JournalPage({ date, strategy, state, offset, onPage }: {
  date: string; strategy: string; state: string; offset: number; onPage: (offset: number) => void;
}) {
  const loader = useCallback(() => api.phase15Journal({ date: date === "today" ? todayIST() : undefined,
    strategy: strategy || undefined, state: state || undefined, offset }), [date, strategy, state, offset]);
  const { data, error } = usePolling(loader, 20000);
  if (error) return <SectionError>PAPER TRADE JOURNAL UNAVAILABLE</SectionError>;
  if (!data) return <Empty>LOADING PAPER TRADE JOURNAL</Empty>;
  return <>
    <p className="phase15-capture-note">{data.date ?? "All dates"} · Decision observation date (IST) · GROSS_ONLY · Net costs unavailable</p>
    {data.items.length?<div className="table-wrap"><ResponsiveTable className="phase15-journal-table" aria-label="Paper trade journal">
      <thead><tr><th>Strategy / State</th><th>Time · IST</th><th>Short / Hedge</th><th>Credit / Debit</th><th>Gross P&L</th><th>Reason / Policy</th><th>Evidence</th></tr></thead>
      <tbody>{data.items.map(trade=><tr key={`${trade.strategy_id}:${trade.trade_id}`}>
        <td><strong>{trade.display_name}</strong><small>{trade.direction??"N/A"}</small><Badge value={trade.state}/></td>
        <td>{timeIST(trade.entry_timestamp??trade.decision_timestamp)}<small>{dateIST(trade.decision_timestamp)} · Exit {timeIST(trade.exit_timestamp)}</small></td>
        <td>{number(trade.short_strike,0)} / {number(trade.hedge_strike,0)}<small>{number(trade.spread_width,0)} pts</small></td>
        <td><span>Entry {money(trade.entry_credit)}</span><small>{trade.state==="CLOSED"?"Exit":"Current"} {money(trade.state==="CLOSED"?trade.exit_debit:trade.mark_available?trade.current_debit:null)}</small></td>
        <td><Gross trade={trade}/><small>{trade.expiry_terminalization?"EXIT PRICE UNAVAILABLE":trade.state==="CLOSED"?"REALIZED GROSS":trade.mark_available?"GROSS MTM":"MARK UNAVAILABLE"}</small></td>
        <td>{blockerLabel(trade.entry_reason)}<small>{trade.terminal_status??trade.exit_reason??trade.rejection_reason??trade.unresolved_reason??"N/A"}</small><small>{trade.exit_policy_version??"UNAVAILABLE"}</small></td>
        <td><ResearchDetails trade={trade}/></td>
      </tr>)}</tbody>
    </ResponsiveTable></div>:<Empty>NO PAPER TRADES MATCH THESE FILTERS</Empty>}
    <Pagination page={data} onPage={onPage}/>
  </>;
}

export function Phase15Journal() {
  const [date, setDate] = useState("today");
  const [strategy, setStrategy] = useState("");
  const [state, setState] = useState("");
  const [offset, setOffset] = useState(0);
  return <Panel title="TRADE JOURNAL" className="phase15-trade-panel">
    <details className="journal-filters"><summary>Filters · {date === "today" ? "Today" : "All dates"}{strategy ? ` · ${strategy.replaceAll("_"," ")}` : ""}{state ? ` · ${state.replaceAll("_"," ")}` : ""}</summary><div className="phase15-journal-filters">
      <label>Date<select aria-label="Date" value={date} onChange={event => { setDate(event.target.value); setOffset(0); }}><option value="today">Today</option><option value="all">All</option></select></label>
      <label>Strategy<select aria-label="Strategy" value={strategy} onChange={event => { setStrategy(event.target.value); setOffset(0); }}><option value="">All strategies</option>
        {STRATEGIES.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <label>State<select aria-label="State" value={state} onChange={event => { setState(event.target.value); setOffset(0); }}><option value="">All states</option>
        {STATES.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
    </div>
    </details>
    {/* A filter/page change remounts the reader: late old responses cannot overwrite the selected page. */}
    <JournalPage key={`${date}:${strategy}:${state}:${offset}`} date={date} strategy={strategy} state={state} offset={offset} onPage={setOffset}/>
  </Panel>;
}

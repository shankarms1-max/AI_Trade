"use client";

import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { dateIST, integer, money, number, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Panel, SectionError } from "@/components/ui";
import { blockerLabel } from "@/components/phase15-monitor";
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
  return <div className="phase15-evidence"><h4>{title}</h4><div className="table-wrap"><table aria-label={title}>
    <thead><tr><th>Leg / instrument</th><th>Side</th><th>Fill</th><th>Bid / Ask</th><th>Bid / Ask qty</th><th>Depth unit</th><th>Source timestamp</th><th>Tick</th></tr></thead>
    <tbody>{Object.entries(quotes).map(([leg, quote]) => <tr key={leg}>
      <td>{leg} · {quote.trading_symbol ?? quote.instrument_token ?? "N/A"}<small>{quote.exchange ?? "N/A"} · {quote.expiry ?? "N/A"} · {number(quote.strike, 0)}</small></td>
      <td>{quote.side ?? "N/A"}</td><td>{number(quote.fill_price)}</td><td>{number(quote.bid)} / {number(quote.ask)}</td>
      <td>{integer(quote.bid_quantity)} / {integer(quote.ask_quantity)}</td><td>{quote.depth_unit ?? "UNKNOWN"}</td>
      <td>{timestamp(quote.source_market_timestamp)}</td><td>{number(quote.tick_size)}</td>
    </tr>)}</tbody></table></div></div>;
}

function ResearchDetails({ trade }: { trade: Phase15PaperTrade }) {
  return <details className="phase15-research"><summary>Research details · {trade.trade_id}</summary>
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

function PositionsPage({ offset, onPage }: { offset: number; onPage: (offset: number) => void }) {
  const loader = useCallback(() => api.phase15Positions(offset), [offset]);
  const { data, error } = usePolling(loader, 20000);
  if (error) return <SectionError>OPEN PAPER POSITIONS UNAVAILABLE</SectionError>;
  if (!data) return <Empty>LOADING OPEN PAPER POSITIONS</Empty>;
  return <>
    <p className="phase15-capture-note">All entry dates · Latest captured marks as of {timestamp(data.as_of)} · GROSS_ONLY</p>
    {data.items.length ? <div className="phase15-position-grid">{data.items.map(trade =>
      <article className="phase15-position" key={`${trade.strategy_id}:${trade.trade_id}`} aria-label={`${trade.display_name} position ${trade.trade_id}`}>
        <header><div><h3>{trade.display_name} · {trade.direction ?? "N/A"}</h3><small>{trade.trade_id}</small></div><Badge value={trade.state}/></header>
        <div className="phase15-position-spread">Short {number(trade.short_strike, 0)} / Hedge {number(trade.hedge_strike, 0)}<strong>Width {number(trade.spread_width, 0)} pts</strong></div>
        <dl className="phase15-trade-fields">
          <div><dt>Entry credit</dt><dd>{number(trade.entry_credit)}</dd></div>
          <div><dt>Current debit</dt><dd>{number(trade.mark_available ? trade.current_debit : null)}</dd></div>
          <div><dt>Unrealized gross MTM</dt><dd><Gross trade={trade}/></dd></div>
          <div><dt>Holding duration</dt><dd>{duration(trade.holding_seconds)}</dd></div>
          <div><dt>MFE / MAE (points)</dt><dd>{number(trade.mfe)} / {number(trade.mae)}</dd></div>
          <div><dt>Exit policy</dt><dd>{trade.exit_policy_version ?? "UNAVAILABLE"}</dd></div>
          <div><dt>Entry timestamp</dt><dd>{timestamp(trade.entry_timestamp)}</dd></div>
          <div><dt>Lot size / lots</dt><dd>{integer(trade.lot_size)} / {integer(trade.lots)}</dd></div>
        </dl>
        <p className={trade.mark_available ? "muted" : "phase15-mark-unavailable"}>{trade.mark_available ? "Latest exact mark" : "MARK UNAVAILABLE"} · {timestamp(trade.latest_mark_timestamp)}</p>
        {trade.unresolved_reason && <p className="phase15-mark-unavailable">{trade.unresolved_reason}</p>}
        <ResearchDetails trade={trade}/>
      </article>)}</div> : <Empty>NO OPEN PAPER POSITIONS</Empty>}
    <Pagination page={data} onPage={onPage}/>
  </>;
}

export function Phase15Positions() {
  const [offset, setOffset] = useState(0);
  return <Panel title="OPEN POSITIONS" className="phase15-trade-panel">
    <PositionsPage key={offset} offset={offset} onPage={setOffset}/>
  </Panel>;
}

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
    {data.items.length ? <div className="table-wrap"><table className="phase15-journal-table" aria-label="Paper trade journal">
      <thead><tr><th>Strategy / Trade</th><th>State / Direction</th><th>Decision / Entry / Exit (IST)</th><th>Short / Hedge</th><th>Width</th><th>Entry credit</th><th>Exit / Current debit</th><th>Realized gross / MTM</th><th>MFE / MAE (pts)</th><th>Holding</th><th>Reasons / Policy</th><th>Evidence</th></tr></thead>
      <tbody>{data.items.map(trade => <tr key={`${trade.strategy_id}:${trade.trade_id}`}>
        <td>{trade.display_name}<small>{trade.trade_id}</small></td><td><Badge value={trade.state}/><small>{trade.direction ?? "N/A"}</small></td>
        <td>{timestamp(trade.decision_timestamp)}<small>Entry {timestamp(trade.entry_timestamp)}</small><small>Exit {timestamp(trade.exit_timestamp)}</small></td>
        <td>{number(trade.short_strike, 0)} / {number(trade.hedge_strike, 0)}</td><td>{number(trade.spread_width, 0)} pts</td>
        <td>{number(trade.entry_credit)}</td><td>{number(trade.state === "CLOSED" ? trade.exit_debit : trade.mark_available ? trade.current_debit : null)}
          {trade.state === "OPEN" && !trade.mark_available && <small>MARK UNAVAILABLE</small>}</td>
        <td><Gross trade={trade}/><small>{trade.expiry_terminalization ? "EXIT PRICE UNAVAILABLE" : trade.state === "CLOSED" ? "REALIZED GROSS" : "GROSS MTM"}</small></td>
        <td>{number(trade.mfe)} / {number(trade.mae)}</td><td>{duration(trade.holding_seconds)}</td>
        <td>{blockerLabel(trade.entry_reason)}<small>{trade.terminal_status ?? trade.exit_reason ?? trade.rejection_reason ?? trade.unresolved_reason ?? "N/A"}</small><small>{trade.exit_policy_version ?? "UNAVAILABLE"}</small></td>
        <td><ResearchDetails trade={trade}/></td>
      </tr>)}</tbody></table></div> : <Empty>NO PAPER TRADES MATCH THESE FILTERS</Empty>}
    <Pagination page={data} onPage={onPage}/>
  </>;
}

export function Phase15Journal() {
  const [date, setDate] = useState("today");
  const [strategy, setStrategy] = useState("");
  const [state, setState] = useState("");
  const [offset, setOffset] = useState(0);
  return <Panel title="TRADE JOURNAL" className="phase15-trade-panel">
    <div className="phase15-journal-filters">
      <label>Date<select value={date} onChange={event => { setDate(event.target.value); setOffset(0); }}><option value="today">Today</option><option value="all">All</option></select></label>
      <label>Strategy<select value={strategy} onChange={event => { setStrategy(event.target.value); setOffset(0); }}><option value="">All strategies</option>
        {STRATEGIES.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <label>State<select value={state} onChange={event => { setState(event.target.value); setOffset(0); }}><option value="">All states</option>
        {STATES.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
    </div>
    {/* A filter/page change remounts the reader: late old responses cannot overwrite the selected page. */}
    <JournalPage key={`${date}:${strategy}:${state}:${offset}`} date={date} strategy={strategy} state={state} offset={offset} onPage={setOffset}/>
  </Panel>;
}

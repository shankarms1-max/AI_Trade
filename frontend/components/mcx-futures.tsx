"use client";
import Link from "next/link";
import { useCallback, useState } from "react";
import { usePolling } from "@/hooks/use-polling";
import { mcxApi } from "@/lib/mcx-futures";
import { describeFailure, toFailure, type RequestError } from "@/lib/http";
import { ageLabel, dash, DASH, dateIST, integer, istDateKey, money, number, parseTimestamp, percent, pretty, stampIST, timeIST } from "@/lib/format";
import { Icon } from "@/components/icons";
import { PollStatus } from "@/components/poll-status";
import { ResponsiveTable } from "@/components/responsive-table";
import { SectionBoundary } from "@/components/section-boundary";
import { EngineStateBadge } from "@/components/engines";
import { Badge, Empty, Metric, Panel } from "@/components/ui";
import { MCX_INSTRUMENTS, type McxDecision, type McxDecisionRecord, type McxInstrument, type McxInstrumentId, type McxPerf, type McxStatus, type McxTrade } from "@/types/mcx-futures";

export const MCX_NAMES: Record<McxInstrumentId, string> = { CRUDEOILM: "Crude Oil Mini", NATGASMINI: "Natural Gas Mini" };
const tone = (value: number | null | undefined) => value == null ? "" : value > 0 ? "positive" : value < 0 ? "negative" : "";
const price = (value: number | null | undefined) => dash(value, v => number(v));
const inr = (value: number | null | undefined) => dash(value, money);
/** Session clock fields may be ISO timestamps or plain IST clock strings ("23:00"); never re-interpreted. */
const clock = (value: string | null) => value == null ? DASH : value.includes("T") && parseTimestamp(value) != null ? stampIST(value) : `${value} IST`;
const NOT_CONFIGURED = <p><strong>MCX FUTURES NOT CONFIGURED</strong> · the MCX futures paper engine API is not available in this deployment. No data is shown rather than guessed.</p>;

export function McxPaperBanner({ status }: { status?: McxStatus | null }) {
  const calibration = status?.calibration_status;
  return <>
    <p className="command-banner ast-paper-banner mcx-banner"><Icon name="shield"/><strong>PAPER ONLY · NO BROKER ORDERS</strong>
      <span>Simulated fills on observed MCX quotes · {status ? `${status.engine_version} · mode ${status.mode}` : "engine version —"}</span></p>
    <p className={`execution-note calibration-note ${calibration === "PRE_REGISTERED_UNVALIDATED" ? "is-unvalidated" : ""}`} aria-label="Calibration status">
      <strong>Calibration: {calibration ?? DASH}</strong>{" "}
      {calibration === "PRE_REGISTERED_UNVALIDATED"
        ? "Parameters were pre-registered before trading. No historical validation exists yet, so paper results here are not evidence of an edge."
        : calibration ? "As reported by the engine." : "Calibration status not yet reported."}
    </p>
  </>;
}

function ScoreBars({ components }: { components: Record<string, number> }) {
  const entries = Object.entries(components);
  if (!entries.length) return <p className="muted">No score components reported.</p>;
  const scale = Math.max(...entries.map(([, v]) => Math.abs(v))) || 1;
  return <ul className="score-bars" aria-label="Score breakdown">{entries.map(([name, value]) =>
    <li key={name}><span>{pretty(name)}</span>
      <span className="score-track" aria-hidden="true"><i className={value < 0 ? "is-negative" : ""} style={{ width: `${Math.round(Math.abs(value) / scale * 100)}%` }}/></span>
      <b className={tone(value)}>{number(value, 2)}</b></li>)}</ul>;
}

function Vetoes({ vetoes }: { vetoes: string[] }) {
  return vetoes.length ? <ul className="mcx-vetoes" aria-label="Vetoes">{vetoes.map(v => <li key={v}><code>{v}</code></li>)}</ul> : <p className="muted">No vetoes.</p>;
}

function DecisionBlock({ decision }: { decision: McxDecision | null }) {
  if (!decision) return <Empty>No decision recorded yet</Empty>;
  return <>
    <dl className="commodity-fields">
      <div><dt>Status</dt><dd>{decision.status ? <Badge value={decision.status}/> : DASH}</dd></div>
      <div><dt>Direction</dt><dd>{decision.direction ?? DASH}</dd></div>
      <div><dt>Score</dt><dd>{dash(decision.score, v => number(v, 2))}</dd></div>
      <div><dt>At</dt><dd>{stampIST(decision.at)}</dd></div>
      <div><dt>Market state</dt><dd>{decision.market_state ? pretty(decision.market_state) : DASH}</dd></div>
      <div><dt>Cycle</dt><dd>{decision.cycle_state ? <Badge value={decision.cycle_state}/> : DASH}{decision.cycle_id && <small> {decision.cycle_id}</small>}</dd></div>
      <div><dt>Re-arm reason</dt><dd>{decision.rearm_reason ?? DASH}</dd></div>
    </dl>
    <h4 className="mcx-subhead">Score breakdown</h4><ScoreBars components={decision.components}/>
    <h4 className="mcx-subhead">Vetoes</h4><Vetoes vetoes={decision.vetoes}/>
  </>;
}

function Utilisation({ label, used, limit, detail }: { label: string; used: number | null; limit: number | null; detail: string }) {
  const ratio = used != null && limit != null && limit > 0 ? Math.min(1, Math.max(0, used / limit)) : null;
  return <div className="risk-meter"><div><span>{label}</span><b>{ratio == null ? DASH : percent(ratio * 100)}</b></div>
    <span className="score-track" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={ratio == null ? undefined : Math.round(ratio * 100)}>
      <i className={ratio != null && ratio >= 0.8 ? "is-negative" : ""} style={{ width: `${ratio == null ? 0 : Math.round(ratio * 100)}%` }}/></span>
    <small>{detail}</small></div>;
}

export function McxInstrumentPanel({ row, now, detailLink = true }: { row: McxInstrument; now: number; detailLink?: boolean }) {
  const { contract, session, feed, position, risk } = row;
  const quoteAge = feed.age_seconds ?? (parseTimestamp(feed.last_quote_at) == null ? null : Math.max(0, Math.floor((now - parseTimestamp(feed.last_quote_at)!) / 1000)));
  const lossUsed = risk.daily_realized == null ? null : Math.max(0, -risk.daily_realized);
  return <section className="mcx-instrument" aria-label={`${row.instrument} paper engine`} data-state={row.state}>
    <header className="mcx-instrument-head">
      <span className={`instrument-icon ${row.instrument === "CRUDEOILM" ? "crude" : "gas"}`}><Icon name={row.instrument === "CRUDEOILM" ? "flame" : "gas"}/></span>
      <div><h2>{detailLink ? <Link href={`/mcx-futures/${row.instrument}`}>{row.instrument}</Link> : row.instrument}</h2><small>{MCX_NAMES[row.instrument]} · MCX futures · PAPER</small></div>
      <EngineStateBadge state={row.state}/>
    </header>
    {row.state_reason && <p className="engine-reason">{row.state_reason}</p>}
    <div className="mcx-blocks">
      <Panel title="CONTRACT">{contract ? <dl className="commodity-fields">
        <div><dt>Trading symbol</dt><dd>{contract.trading_symbol}</dd></div><div><dt>Expiry</dt><dd>{contract.expiry ? dateIST(contract.expiry) : DASH}</dd></div>
        <div><dt>Lot size</dt><dd>{dash(contract.lot_size, integer)}{contract.quotation_unit ? ` · ${contract.quotation_unit}` : ""}</dd></div>
        <div><dt>Tick size</dt><dd>{dash(contract.tick_size, v => number(v, 2))}</dd></div>
        <div><dt>Token</dt><dd>{contract.token ?? DASH}</dd></div><div><dt>Source</dt><dd>{contract.source ?? DASH}</dd></div>
      </dl> : <Empty>Contract not resolved</Empty>}</Panel>
      <Panel title="SESSION" action={<Badge value={session.status} tone={session.status === "OPEN" ? "good" : session.status === "PRE_OPEN" ? "warn" : "neutral"}/>}><dl className="commodity-fields">
        <div><dt>Window</dt><dd>{session.window ?? DASH}</dd></div><div><dt>Next open</dt><dd>{stampIST(session.next_open)}</dd></div>
        <div><dt>Last entry</dt><dd>{clock(session.last_entry_time)}</dd></div><div><dt>Forced exit</dt><dd>{clock(session.forced_exit_time)}</dd></div>
      </dl></Panel>
      <Panel title="FEED" action={<Badge value={feed.stale ? "STALE" : feed.last_quote_at ? "FRESH" : "NO_QUOTE"}/>}><dl className="commodity-fields">
        <div><dt>Last quote</dt><dd>{stampIST(feed.last_quote_at)}</dd></div><div><dt>Quote age</dt><dd>{quoteAge == null ? DASH : `${ageLabel(Math.round(quoteAge))} ago`}</dd></div>
        <div><dt>Bid / Ask</dt><dd>{price(feed.bid)} / {price(feed.ask)}</dd></div><div><dt>LTP</dt><dd>{price(feed.ltp)}</dd></div>
        <div><dt>Spread</dt><dd>{dash(feed.spread_ticks, v => `${number(v, 1)} ticks`)}</dd></div>
      </dl></Panel>
      <Panel title="LAST DECISION"><DecisionBlock decision={row.last_decision}/></Panel>
      <Panel title="PAPER POSITION" action={position ? <Badge value={position.direction}/> : undefined}>{position ? <>
        <div className="compact-metrics">
          <Metric label="Unrealized (MTM)" value={inr(position.unrealized_pnl)} tone={tone(position.unrealized_pnl)} detail={position.mark_at ? `Marked ${stampIST(position.mark_at)}` : "Mark time unavailable"}/>
          <Metric label="Mark price" value={price(position.mark_price)}/>
        </div>
        <dl className="commodity-fields">
          <div><dt>Entry</dt><dd>{price(position.entry_price)} · {stampIST(position.entry_at)}</dd></div><div><dt>Stop</dt><dd>{price(position.stop_price)}</dd></div>
          <div><dt>Quantity</dt><dd>{dash(position.quantity_lots, v => `${integer(v)} lot${v === 1 ? "" : "s"}`)}</dd></div>
        </dl></> : <Empty>No open paper position</Empty>}</Panel>
      <Panel title="TODAY & RISK">
        <div className="compact-metrics">
          <Metric label="Realized today (closed)" value={money(row.realized_pnl_today)} tone={tone(row.realized_pnl_today)}/>
          <Metric label="Paper trades today" value={integer(row.trades_today)}/>
          <Metric label="Entries rejected" value={integer(row.entries_rejected_today)} detail={row.last_rejection ? `${row.last_rejection.reason ?? DASH} · ${row.last_rejection.at ? timeIST(row.last_rejection.at) : DASH}` : "No rejection today"}/>
        </div>
        <Utilisation label="Daily loss limit used" used={lossUsed} limit={risk.daily_loss_limit} detail={`Realized ${inr(risk.daily_realized)} of limit ${inr(risk.daily_loss_limit)}`}/>
        <Utilisation label="Entries used" used={risk.entries_today} limit={risk.max_entries} detail={`${dash(risk.entries_today, integer)} of ${dash(risk.max_entries, integer)} entries · max risk per trade ${inr(risk.max_risk_per_trade)}`}/>
      </Panel>
    </div>
  </section>;
}

function useMcxStatus() {
  const loader = useCallback((signal: AbortSignal) => mcxApi.status(signal).then(value => ({ value, now: Date.now() })), []);
  return usePolling(loader, 10000);
}

function Missing({ instrument }: { instrument: McxInstrumentId }) {
  return <section className="mcx-instrument" aria-label={`${instrument} paper engine`}><header className="mcx-instrument-head"><div><h2>{instrument}</h2><small>{MCX_NAMES[instrument]}</small></div></header>
    <Empty>{instrument} is not reported by the engine</Empty></section>;
}

/** `/mcx-futures`: both instruments side by side. */
export function McxFuturesOverview() {
  const poll = useMcxStatus();
  const status = poll.data?.value ?? null;
  return <>
    <McxPaperBanner status={status}/>
    <PollStatus poll={poll} label="MCX FUTURES STATUS" notConfigured={NOT_CONFIGURED}/>
    {!status ? !poll.failure && <Empty>Loading MCX futures status</Empty> :
      <div className="mcx-grid">{MCX_INSTRUMENTS.map(id => {
        const row = status.instruments.find(item => item.instrument === id);
        return <SectionBoundary key={id} label={id}>{row ? <McxInstrumentPanel row={row} now={poll.data!.now}/> : <Missing instrument={id}/>}</SectionBoundary>;
      })}</div>}
    {status && <p className="accounting-note">As of {stampIST(status.generated_at)}. Realized P&amp;L counts closed paper trades; unrealized is the open position marked to its last quote. {DASH} means not reported.</p>}
  </>;
}

/** Group rows by IST calendar date (sessions never split on the browser's local midnight). */
function byIstDate<T>(rows: T[], at: (row: T) => string | null): Array<[string, T[]]> {
  const groups = new Map<string, T[]>();
  for (const row of rows) {
    const key = istDateKey(at(row)) ?? "Undated";
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  return [...groups.entries()];
}
const groupLabel = (key: string) => key === "Undated" ? "Date unavailable" : dateIST(`${key}T12:00:00+05:30`);

function DecisionTimeline({ instrument }: { instrument: McxInstrumentId }) {
  const loader = useCallback((signal: AbortSignal) => mcxApi.decisions(instrument, 50, signal), [instrument]);
  const poll = usePolling(loader, 15000);
  const items = poll.data?.items ?? [];
  return <Panel title="DECISION TIMELINE">
    <PollStatus poll={poll} label="DECISIONS" notConfigured={NOT_CONFIGURED}/>
    {!poll.data ? !poll.failure && <Empty>Loading decisions</Empty> : !items.length ? <Empty>No decisions recorded yet</Empty> :
      byIstDate(items, d => d.at).map(([key, rows]) => <div key={key} className="mcx-timeline-day"><h3>{groupLabel(key)}</h3>
        <ol className="mcx-timeline">{rows.map((d: McxDecisionRecord, index) => <li key={`${d.at}-${index}`}>
          <time dateTime={d.at}>{timeIST(d.at)}</time>
          <div><div className="engine-badges">{d.status ? <Badge value={d.status}/> : <span>{DASH}</span>}{d.direction && <Badge value={d.direction} tone="neutral"/>}{d.cycle_state && <Badge value={d.cycle_state} tone="neutral"/>}</div>
            <p>Score {dash(d.score, v => number(v, 2))}{d.transition ? ` · ${pretty(d.transition)}` : ""}{d.cycle_id ? ` · cycle ${d.cycle_id}` : ""}{d.evidence_at ? ` · evidence ${timeIST(d.evidence_at)}` : ""}</p>
            {d.vetoes.length > 0 && <Vetoes vetoes={d.vetoes}/>}
            {Object.keys(d.components).length > 0 && <details className="research-disclosure"><summary>Score breakdown</summary><ScoreBars components={d.components}/></details>}
          </div></li>)}</ol></div>)}
  </Panel>;
}

function TradeRows({ trades }: { trades: McxTrade[] }) {
  return <>{byIstDate(trades, t => t.entry_at ?? t.exit_at).map(([key, rows]) => <tbody key={key}>
    <tr className="group-row"><th scope="rowgroup" colSpan={14}>{groupLabel(key)} · {rows.length} record{rows.length === 1 ? "" : "s"}</th></tr>
    {rows.map(t => <tr key={t.trade_id}>
      <th scope="row">{timeIST(t.entry_at)}<small>{t.trade_id}</small></th>
      <td>{t.direction ?? DASH}</td><td>{t.contract ?? DASH}</td><td>{dash(t.quantity_lots, integer)} × {dash(t.lot_size, integer)}</td>
      <td>{price(t.entry_price)}</td><td>{price(t.exit_price)}<small>{t.exit_at ? timeIST(t.exit_at) : DASH}</small></td>
      <td>{t.exit_reason ? pretty(t.exit_reason) : t.rejection_reason ? <code>{t.rejection_reason}</code> : DASH}</td>
      <td className={tone(t.gross_pnl)}>{inr(t.gross_pnl)}</td><td>{inr(t.fees)}</td><td>{inr(t.slippage_cost)}</td>
      <td className={tone(t.net_pnl)}>{inr(t.net_pnl)}</td><td><Badge value={t.state}/></td><td>{t.version ?? DASH}</td><td>{dash(t.journal_sequence, integer)}</td>
    </tr>)}</tbody>)}</>;
}

function TradeBlotter({ instrument }: { instrument: McxInstrumentId }) {
  const loader = useCallback((signal: AbortSignal) => mcxApi.trades(instrument, { limit: 50 }, signal), [instrument]);
  const poll = usePolling(loader, 15000);
  const [older, setOlder] = useState<{ items: McxTrade[]; cursor: string | null; loading: boolean; failure: RequestError | null } | null>(null);
  const cursor = older ? older.cursor : poll.data?.next_before ?? null;
  const loadOlder = async () => {
    if (!cursor || older?.loading) return;
    setOlder(prev => ({ items: prev?.items ?? [], cursor, loading: true, failure: null }));
    try {
      const page = await mcxApi.trades(instrument, { limit: 50, before: cursor });
      setOlder(prev => ({ items: [...(prev?.items ?? []), ...page.items], cursor: page.next_before, loading: false, failure: null }));
    } catch (error) { setOlder(prev => ({ items: prev?.items ?? [], cursor, loading: false, failure: toFailure(error) })); }
  };
  const seen = new Set<string>();
  const trades = [...(poll.data?.items ?? []), ...(older?.items ?? [])].filter(t => !seen.has(t.trade_id) && seen.add(t.trade_id));
  return <Panel title="PAPER TRADE BLOTTER" action={<Badge value="PAPER"/>}>
    <PollStatus poll={poll} label="TRADES" notConfigured={NOT_CONFIGURED}/>
    {!poll.data ? !poll.failure && <Empty>Loading paper trades</Empty> : !trades.length ? <Empty>No paper trades recorded yet</Empty> : <>
      <div className="table-wrap sticky-first"><ResponsiveTable aria-label={`${instrument} paper trades`} data-layout="scroll" className="mcx-blotter"><thead><tr>
        <th>Entry (IST)</th><th>Side</th><th>Contract</th><th>Lots × size</th><th>Entry</th><th>Exit</th><th>Exit / rejection</th>
        <th>Gross</th><th>Fees</th><th>Slippage</th><th>Net</th><th>State</th><th>Version</th><th>Journal seq</th></tr></thead>
        <TradeRows trades={trades}/></ResponsiveTable></div>
      <div className="commodity-pagination"><span>{trades.length} paper records loaded</span>
        {older?.failure && <span className="negative">Older page failed: {describeFailure(older.failure)}</span>}
        {cursor ? <button type="button" onClick={() => void loadOlder()} disabled={older?.loading}>{older?.loading ? "Loading…" : "Load older trades"}</button> : <span>All records loaded</span>}</div>
    </>}
  </Panel>;
}

function PerfMetrics({ perf, label }: { perf: McxPerf; label: string }) {
  return <div className="mcx-perf" aria-label={label}>
    <div className="compact-metrics">
      <Metric label="Closed paper trades" value={integer(perf.trades)}/>
      <Metric label="Win rate" value={dash(perf.win_rate, v => percent(v * 100))}/>
      <Metric label="Expectancy / trade" value={inr(perf.expectancy)} tone={tone(perf.expectancy)}/>
      <Metric label="Average win" value={inr(perf.avg_win)}/>
      <Metric label="Average loss" value={inr(perf.avg_loss)}/>
      <Metric label="Profit factor" value={dash(perf.profit_factor, v => number(v, 2))}/>
      <Metric label="Max drawdown" value={inr(perf.max_drawdown)}/>
    </div>
    <dl className="commodity-fields pnl-split">
      <div><dt>Gross</dt><dd className={tone(perf.gross)}>{inr(perf.gross)}</dd></div><div><dt>Fees</dt><dd>{inr(perf.fees)}</dd></div>
      <div><dt>Net</dt><dd className={tone(perf.net)}>{inr(perf.net)}</dd></div>
      <div><dt>Realized (closed)</dt><dd className={tone(perf.realized)}>{inr(perf.realized)}</dd></div>
      <div><dt>Unrealized (open, marked)</dt><dd className={tone(perf.unrealized)}>{inr(perf.unrealized)}</dd></div>
    </dl>
  </div>;
}

function Performance({ instrument }: { instrument: McxInstrumentId }) {
  const loader = useCallback((signal: AbortSignal) => mcxApi.performance(instrument, signal), [instrument]);
  const poll = usePolling(loader, 30000);
  const perf = poll.data?.by_instrument[instrument];
  return <Panel title="FORWARD PAPER PERFORMANCE" action={<Badge value="PAPER"/>}>
    <PollStatus poll={poll} label="PERFORMANCE" notConfigured={NOT_CONFIGURED}/>
    <p className="muted">Closed paper trades on this engine only. Not a backtest and not historical validation. Win rate is reported as a fraction and shown as a percentage.</p>
    {!poll.data ? !poll.failure && <Empty>Loading performance</Empty> : !perf ? <Empty>No performance reported for {instrument}</Empty> : perf.trades === 0
      ? <><Empty>No closed paper trades yet</Empty><PerfMetrics perf={perf} label={`${instrument} performance`}/></>
      : <PerfMetrics perf={perf} label={`${instrument} performance`}/>}
  </Panel>;
}

/** `/mcx-futures/[instrument]`: status, decision timeline, trade blotter and performance for one instrument. */
export function McxInstrumentDetail({ instrument }: { instrument: McxInstrumentId }) {
  const poll = useMcxStatus();
  const status = poll.data?.value ?? null;
  const row = status?.instruments.find(item => item.instrument === instrument);
  const notConfigured = poll.failure?.kind === "not_configured";
  return <>
    <McxPaperBanner status={status}/>
    <PollStatus poll={poll} label={`${instrument} STATUS`} notConfigured={NOT_CONFIGURED}/>
    {!status ? !poll.failure && <Empty>Loading {instrument} status</Empty> :
      <SectionBoundary label={`${instrument} status`}>{row ? <McxInstrumentPanel row={row} now={poll.data!.now} detailLink={false}/> : <Missing instrument={instrument}/>}</SectionBoundary>}
    {!notConfigured && <>
      <SectionBoundary label="Decision timeline"><DecisionTimeline instrument={instrument}/></SectionBoundary>
      <SectionBoundary label="Trade blotter"><TradeBlotter instrument={instrument}/></SectionBoundary>
      <SectionBoundary label="Performance"><Performance instrument={instrument}/></SectionBoundary>
    </>}
    <p className="workspace-links"><Link href="/mcx-futures">← Both instruments</Link>{MCX_INSTRUMENTS.filter(id => id !== instrument).map(id => <Link key={id} href={`/mcx-futures/${id}`}>{id} →</Link>)}</p>
  </>;
}

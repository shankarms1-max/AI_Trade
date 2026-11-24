"use client";
import { useCallback, type ReactNode } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { usePolling } from "@/hooks/use-polling";
import { astApi } from "@/lib/ast-futures";
import { ageLabel, dateIST, integer, istDateKey, money, number, parseTimestamp, percent, timeIST } from "@/lib/format";
import { observationState } from "@/lib/monitoring";
import { Icon } from "@/components/icons";
import { ResponsiveTable } from "@/components/responsive-table";
import { SectionBoundary } from "@/components/section-boundary";
import { Badge, Empty, Metric, Panel, SectionError } from "@/components/ui";
import { PollStatus, RetryButton } from "@/components/poll-status";
import { describeFailure } from "@/lib/http";
import type { AstPerformance, AstStatus, AstTrade } from "@/types/ast-futures";

const ACTIVE = ["PENDING_PAPER_ENTRY", "OPEN", "PENDING_PAPER_EXIT", "UNRESOLVED_EXPOSURE"];
const LABELS: Record<string, string> = {
  AST_OPENING_RANGE_UNAVAILABLE: "Opening range not available yet",
  AST_CURRENT_COMPLETED_BAR_UNAVAILABLE: "No current completed 5-minute bar",
  AST_WARMUP_REQUIRED: "SuperTrend warming up",
  ORB_NOT_BROKEN: "Price inside the opening range",
  AST_ORB_DIRECTION_CONFLICT: "Breakout and AST direction disagree",
  MAX_OPEN_POSITIONS_REACHED: "One position already active",
  AST_OPPORTUNITY_CONSUMED: "Opportunity already traded",
  SESSION_EXIT_BOUNDARY: "At or after the 15:20 boundary",
  KILL_SWITCH_ACTIVE: "Operational kill switch active",
  SIGNAL_BAR_FUTURES_IDENTITY_UNAVAILABLE: "Signal bar contract unknown",
  FUTURES_PRICE_UNAVAILABLE: "Futures LTP unavailable",
  FUTURES_LOT_SIZE_MISMATCH: "Broker lot size differs from configuration",
  FUTURES_IDENTITY_UNAVAILABLE_OR_EXPIRED: "Futures contract unknown or expired",
  OBSERVATION_TIMING_INVALID: "Observation timing invalid",
  AST_FLIP: "AST flip",
  FORCED_EXIT: "Forced exit 15:20",
  SAFETY_EXIT_KILL_SWITCH: "Safety exit (kill switch)",
  HELD_CONTRACT_UNAVAILABLE: "Held contract unavailable",
  MISSED_SESSION_EXIT: "Missed session exit",
};
const human = (code: string) => LABELS[code.split(":")[0]] ?? code.replaceAll("_", " ").toLowerCase().replace(/^\w/, c => c.toUpperCase());
const stamp = (value?: string | null) => value ? `${dateIST(value)} · ${timeIST(value)}` : "N/A";
const tone = (value: number | null | undefined) => value == null ? "" : value > 0 ? "positive" : value < 0 ? "negative" : "";
const signed = (value: number | null | undefined, digits = 2) => value == null ? "N/A" : `${value > 0 ? "+" : ""}${number(value, digits)}`;
const istClock = (now: number) => new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(now));
const istDate = (value: string) => istDateKey(value);

/** Today's capture exists only when the latest observation belongs to the reported trading date. */
function observedToday(status: AstStatus): boolean {
  return Boolean(status.last_observation_at && istDate(status.last_observation_at) === status.trading_date);
}

function StatusStrip({ status, now }: { status: AstStatus; now: number }) {
  const position = status.position;
  const freshness = observedToday(status) ? observationState(status.last_observation_at, now, 60) : "WAITING";
  const tiles: Array<[string, ReactNode, string]> = [
    ["Worker", <Badge key="w" value={status.worker === "RUNNING" ? "RUNNING" : "UNHEALTHY"}/>, `${status.worker === "RUNNING" ? "" : `Worker ${status.worker} · `}${status.heartbeat_age_seconds != null ? `heartbeat ${ageLabel(Math.round(status.heartbeat_age_seconds))} ago` : "no heartbeat file"}`],
    ["Market", <Badge key="m" value={status.in_session ? "MARKET_OPEN" : "MARKET_CLOSED"}/>, status.in_session ? `Observation ${freshness.replaceAll("_", " ").toLowerCase()}` : "IST session window 09:15–15:30 · holidays not inferred"],
    ["Strategy state", <Badge key="s" value={status.status ?? "NO_OBSERVATION"}/>, status.trading_date],
    ["Position", <Badge key="p" value={position ? `${position.side ?? ""}_${position.state}`.replace(/^_/, "") : "FLAT"}/>, position ? "One paper lot" : "No paper exposure"],
    ["Journal", <Badge key="j" value={status.journal_verification.startsWith("VALID") ? "VALID" : "INVALID"}/>, `Sequence ${integer(status.journal_sequence)}`],
    ["Forced exit", <Badge key="f" value={status.forced_exit_status}/>, "15:20 IST"],
  ];
  return <div className="overview-health-grid ast-status-strip" aria-label="AST status">
    {tiles.map(([label, value, detail]) => <div className="health-tile" key={label}><span>{label}</span>{value}<p>{detail}</p></div>)}
  </div>;
}

function Alerts({ status, now }: { status: AstStatus; now: number }) {
  const alerts: string[] = [];
  if (status.position?.state === "UNRESOLVED_EXPOSURE") alerts.push(`UNRESOLVED PAPER EXPOSURE — ${status.position.unresolved_reason ?? "reason unavailable"}. No exit price is invented; operator review is required.`);
  if (status.worker !== "RUNNING") alerts.push(`WORKER ${status.worker} — heartbeat is ${status.heartbeat_age_seconds != null ? `${Math.round(status.heartbeat_age_seconds)}s old` : "missing"}. Displayed state may be stale.`);
  if (status.in_session && observedToday(status) && observationState(status.last_observation_at, now, 60) === "STALE") alerts.push("MARKET OBSERVATIONS STALE — no futures observation in the last 60 seconds.");
  if (status.kill_switch) alerts.push("KILL SWITCH ACTIVE — new entries blocked; any open paper position takes a SAFETY exit.");
  if (!status.journal_verification.startsWith("VALID")) alerts.push(`JOURNAL VERIFICATION FAILED — ${status.journal_verification}`);
  if (status.live_trading !== false || status.mode !== "PAPER") alerts.push("UNEXPECTED MODE — this dashboard expects PAPER with live trading disabled.");
  if (!alerts.length) return null;
  return <div className="ast-alerts" role="alert">{alerts.map(text => <p key={text} className="section-error">{text}</p>)}</div>;
}

function InstrumentHero({ status, now }: { status: AstStatus; now: number }) {
  const futures = status.futures_instrument;
  const today = observedToday(status);
  const config = status.policy_configuration;
  const observed = parseTimestamp(status.last_observation_at);
  const age = observed != null ? Math.max(0, Math.floor((now - observed) / 1000)) : null;
  return <section className="instrument-identity ast-instrument" aria-label="NIFTY futures instrument">
    <p className="eyebrow"><span/>NIFTY futures · observed contract</p>
    {today && futures ? <>
      <h2>{futures.instrument_id ?? "Contract unavailable"}</h2>
      <div className="instrument-price">{number(futures.ltp)}</div>
      <p>Observed futures LTP · paper reference, not an exchange bid/ask</p>
    </> : <><h2>Waiting for first market observation</h2><p>Captures run 09:15–15:30 IST. {status.last_observation_at ? `Last observation ${stamp(status.last_observation_at)}.` : "No observation recorded yet."}</p></>}
    <dl className="commodity-fields">
      <div><dt>Expiry</dt><dd>{today ? futures?.expiry ?? "N/A" : "N/A"}</dd></div>
      <div><dt>Lot size (configured / observed)</dt><dd>{integer(config?.lot_size)} / {today ? integer(futures?.observed_lot_size) : "N/A"}</dd></div>
      <div><dt>Quantity</dt><dd>{config ? `${config.lots} lot · ${integer(config.lots * config.lot_size)} units` : "N/A"}</dd></div>
      <div><dt>Last observation</dt><dd>{stamp(status.last_observation_at)}</dd></div>
      <div><dt>Freshness</dt><dd>{today && age != null ? `${ageLabel(age)} ago` : "No capture today"}</dd></div>
      <div><dt>Observation check</dt><dd>{today ? futures?.observation_blocker ? human(futures.observation_blocker) : "Executable on LTP basis" : "N/A"}</dd></div>
    </dl>
  </section>;
}

function FrozenStrategy({ status }: { status: AstStatus }) {
  const fixed = status.fixed_parameters;
  const config = status.policy_configuration;
  const rows: Array<[string, string]> = [["ATR", String(fixed.atr_period)], ["Multiplier", String(fixed.multiplier)], ["Regime", fixed.regime],
    ["Optimizer", fixed.optimizer], ["Opening range", "09:15–09:30 IST"], ["Forced exit", "15:20 IST"],
    ["History sessions", config ? String(config.history_sessions) : "N/A"], ["Lots", config ? String(config.lots) : "N/A"], ["Fill basis", status.fill_basis]];
  return <Panel title="FROZEN STRATEGY · AST_FUTURES_V1_DIRECT"><dl className="commodity-fields ast-frozen">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
    <p className="muted">Fixed 22 / 3.9 policy. This is not the older optimizer-based AST strategy. Policy {status.policy_hash ? status.policy_hash.slice(0, 12) : "unconfigured"}.</p></Panel>;
}

function RangeAndAst({ status, now }: { status: AstStatus; now: number }) {
  const orb = status.opening_range;
  const ast = status.ast;
  const bar = status.last_completed_bar;
  const clock = istClock(now);
  const location = orb && bar?.close != null ? bar.close > orb.high ? "ABOVE_RANGE" : bar.close < orb.low ? "BELOW_RANGE" : "INSIDE_RANGE" : null;
  const building = !orb && status.in_session && clock < "09:30";
  return <Panel title="OPENING RANGE · AST 22 / 3.9" action={location ? <Badge value={location}/> : building ? <Badge value="RANGE_BUILDING"/> : undefined}>
    {orb ? <dl className="commodity-fields">
      <div><dt>OR high</dt><dd>{number(orb.high)}</dd></div><div><dt>OR low</dt><dd>{number(orb.low)}</dd></div>
      <div><dt>OR width</dt><dd>{number(orb.high - orb.low)} pts</dd></div><div><dt>Range</dt><dd>{timeIST(orb.range_start)} – {timeIST(orb.range_end)}</dd></div>
    </dl> : <p className="muted">{building ? "Opening range building from the first three completed 5-minute bars (09:15–09:30 IST)." : status.in_session && observedToday(status) ? "Opening range unavailable for this session; it is never rebuilt from later bars." : "The opening range forms from 09:15 to 09:30 IST."}</p>}
    <dl className="commodity-fields">
      <div><dt>AST direction</dt><dd>{ast?.direction ? <Badge value={ast.direction}/> : "N/A"}</dd></div>
      <div><dt>AST value</dt><dd>{number(ast?.value)}</dd></div>
      <div><dt>ATR value</dt><dd>{number(ast?.atr)}</dd></div>
      <div><dt>Last transition</dt><dd>{ast?.transition ? stamp(ast.transition) : "N/A"}</dd></div>
      <div><dt>Last completed 5m bar</dt><dd>{bar ? `${timeIST(bar.start)}–${timeIST(bar.end)}` : "N/A"}</dd></div>
      <div><dt>Bar close</dt><dd>{bar ? `${number(bar.close)}${bar.valid ? "" : ` · invalid (${bar.reason ?? "unknown"})`}` : "N/A"}</dd></div>
    </dl>
  </Panel>;
}

function PositionCard({ status }: { status: AstStatus }) {
  const position = status.position;
  const detail = status.position_detail;
  if (!position) return <Panel title="CURRENT PAPER POSITION"><Empty>No open paper position</Empty></Panel>;
  const mark = status.mark?.trade_id === position.trade_id ? status.mark : null;
  const unresolved = position.state === "UNRESOLVED_EXPOSURE";
  return <section className={`panel ast-position ${unresolved ? "is-critical" : "is-active"}`} aria-label="Current paper position">
    <header className="panel-head"><h2 className="panel-title">CURRENT PAPER POSITION</h2><div className="ast-badges"><Badge value={position.side ?? "UNKNOWN"}/><Badge value={position.state}/></div></header>
    <div className="panel-body">
      {position.state === "PENDING_PAPER_ENTRY" && <p className="execution-note">Signal accepted. Paper entry waits for the next executable futures observation.</p>}
      {detail?.pending_exit_reason && position.state === "PENDING_PAPER_EXIT" && <p className="execution-note">Pending paper exit: <strong>{human(detail.pending_exit_reason)}</strong> ({detail.pending_exit_reason})</p>}
      {unresolved && <p className="section-error">Unresolved exposure: {position.unresolved_reason ?? "reason unavailable"}. No exit or P&L is assumed.</p>}
      <div className="compact-metrics">
        <Metric label="Entry (paper fill)" value={number(position.entry_price)} detail={stamp(position.entry_timestamp)}/>
        <Metric label="Latest mark (LTP)" value={number(mark?.price)} detail={mark ? stamp(mark.at) : "Mark unavailable"}/>
        <Metric label="Gross points" value={signed(mark?.points)} tone={tone(mark?.points)}/>
        <Metric label="Gross INR (paper)" value={status.position_mark_gross_inr == null ? "N/A" : money(status.position_mark_gross_inr)} tone={tone(status.position_mark_gross_inr)}/>
      </div>
      <dl className="commodity-fields">
        <div><dt>Contract</dt><dd>{position.future_instrument_id ?? "N/A"} · {position.future_expiry ?? "N/A"}</dd></div>
        <div><dt>Quantity</dt><dd>{detail?.quantity != null ? `${integer(detail.quantity)} units (1 lot)` : `${integer(position.lot_size)} units`}</dd></div>
        <div><dt>MFE / MAE</dt><dd>{signed(mark?.mfe_points)} / {signed(mark?.mae_points)} pts</dd></div>
        <div><dt>Fill basis</dt><dd>{detail?.fill_basis ?? status.fill_basis} · LTP proxy</dd></div>
      </dl>
    </div>
  </section>;
}

function TodayPanel({ status }: { status: AstStatus }) {
  const loader = useCallback(() => astApi.trades(status.trading_date), [status.trading_date]);
  const { data, error } = usePolling(loader, 15000);
  const closed = (data?.items ?? []).filter(t => t.state === "CLOSED");
  const inr = closed.length && closed.every(t => t.gross_inr != null) ? closed.reduce((sum, t) => sum + (t.gross_inr as number), 0) : null;
  return <Panel title={`TODAY · ${status.trading_date}`}>
    <div className="compact-metrics">
      <Metric label="Trades today" value={integer(status.trades_today.filter(t => t.entry_timestamp).length)} detail={`${status.trades_today.length} paper records`}/>
      <Metric label="Gross points today" value={signed(status.today_gross_points)} tone={tone(status.today_gross_points)}/>
      <Metric label="Gross INR today" value={data ? closed.length ? money(inr) : money(0) : "N/A"} tone={tone(inr)}/>
      <Metric label="Wins / losses" value={data ? `${closed.filter(t => (t.gross_points ?? 0) > 0).length} / ${closed.filter(t => (t.gross_points ?? 0) < 0).length}` : "N/A"} detail={status.position ? `Position ${status.position.side} ${status.position.state}` : "Flat"}/>
    </div>
    {error ? <SectionError>TODAY&apos;S LEDGER UNAVAILABLE</SectionError> : !data ? <Empty>Loading today&apos;s paper ledger</Empty> : data.items.length ? <TradeTable trades={data.items} label="Today's paper trades"/> : <Empty>No paper trades today</Empty>}
  </Panel>;
}

function TradeTable({ trades, label }: { trades: AstTrade[]; label: string }) {
  return <div className="table-wrap"><ResponsiveTable aria-label={label}><thead><tr><th>Time</th><th>Side</th><th>Contract</th><th>Entry</th><th>Exit</th><th>Points</th><th>INR</th><th>Exit reason</th><th>State</th></tr></thead><tbody>
    {trades.map(t => <tr key={t.trade_id}>
      <td>{timeIST(t.entry_timestamp ?? t.signal_timestamp)}</td><td>{t.side ?? "N/A"}</td><td>{t.future_instrument_id ?? "N/A"}</td>
      <td>{number(t.entry_price)}</td><td>{number(t.exit_price)}</td><td className={tone(t.gross_points)}>{signed(t.gross_points)}</td>
      <td className={tone(t.gross_inr)}>{t.gross_inr == null ? "N/A" : money(t.gross_inr)}</td>
      <td>{t.exit_reason ? human(t.exit_reason) : t.rejection_reason ? human(t.rejection_reason) : t.unresolved_reason ? human(t.unresolved_reason) : "—"}</td>
      <td><Badge value={t.state}/></td></tr>)}
  </tbody></ResponsiveTable></div>;
}

function DailyPaperChart({ daily }: { daily: AstPerformance["daily"] }) {
  return <div className="chart-sm" aria-label="Cumulative forward paper points by day"><ResponsiveContainer width="100%" height="100%"><LineChart data={daily}>
    <CartesianGrid stroke="var(--border)"/><XAxis dataKey="date" stroke="var(--muted)" fontSize={10} minTickGap={24} interval="preserveStartEnd" tickMargin={6}/><YAxis stroke="var(--muted)" fontSize={11} width={52} tickMargin={4}/>
    <Tooltip contentStyle={{ color: "var(--text)", background: "var(--panel)", border: "1px solid var(--border)" }}/><ReferenceLine y={0} stroke="var(--muted)"/>
    <Line dataKey="cumulative_points" name="Cumulative paper points" stroke="var(--accent)" strokeWidth={2} dot={false}/></LineChart></ResponsiveContainer></div>;
}

function ForwardPerformance() {
  const loader = useCallback((signal: AbortSignal) => astApi.performance(signal), []);
  const { data, error } = usePolling(loader, 15000);
  if (error) return <Panel title="FORWARD PAPER PERFORMANCE"><SectionError>FORWARD PAPER PERFORMANCE UNAVAILABLE</SectionError></Panel>;
  if (!data) return <Panel title="FORWARD PAPER PERFORMANCE"><Empty>Loading forward paper performance</Empty></Panel>;
  return <Panel title="FORWARD PAPER PERFORMANCE" action={<Badge value="PAPER"/>}>
    <p className="muted">Closed paper trades since this engine started, on the FUTURES_LTP_PAPER_V1 basis. Not a backtest. Drawdown uses closed-trade equity, not mark-to-market.</p>
    <div className="compact-metrics">
      <Metric label="Closed paper trades" value={integer(data.closed_trades)} detail={`${data.wins} wins · ${data.losses} losses${data.flat ? ` · ${data.flat} flat` : ""}`}/>
      <Metric label="Win rate" value={data.win_rate == null ? "N/A" : percent(data.win_rate * 100)}/>
      <Metric label="Gross points" value={signed(data.gross_points)} tone={tone(data.gross_points)} detail={data.average_points == null ? undefined : `${signed(data.average_points)} avg / trade`}/>
      <Metric label="Net research points" value={signed(data.net_research_points)} tone={tone(data.net_research_points)} detail={data.net_research_points == null ? "No friction configured" : "After configured friction"}/>
      <Metric label="Gross INR" value={data.gross_inr == null ? "N/A" : money(data.gross_inr)} tone={tone(data.gross_inr)}/>
      <Metric label="Max realized drawdown" value={`${number(data.max_realized_drawdown_points)} pts`} detail={`Current ${number(data.current_drawdown_points)} pts below peak ${number(data.current_peak_points)}`}/>
      <Metric label="Best / worst trade" value={data.best_trade ? `${signed(data.best_trade.gross_points)} / ${signed(data.worst_trade?.gross_points)}` : "N/A"}/>
    </div>
    {data.excluded_unresolved_exposures > 0 && <p className="warning">{data.excluded_unresolved_exposures} unresolved exposure(s) excluded: no exit price exists.</p>}
    {data.daily.length >= 2 ? <DailyPaperChart daily={data.daily}/> : <p className="muted">The cumulative daily chart appears once closed paper trades exist on two or more sessions.</p>}
  </Panel>;
}

function DecisionEvidence({ status }: { status: AstStatus }) {
  const blockers = status.blockers ?? [];
  return <Panel title="DECISION EVIDENCE" action={<Badge value={status.status ?? "NO_OBSERVATION"}/>}>
    {blockers.length ? <ul className="ast-blockers">{blockers.map(code => <li key={code}><span>{human(code)}</span><code>{code}</code></li>)}</ul> : <p className="muted">{status.status ? "No blockers on the latest observation." : "No observation processed yet."}</p>}
    <dl className="commodity-fields">
      <div><dt>Fill blocker</dt><dd>{status.fill_blocker ? <>{human(status.fill_blocker)} <code>{status.fill_blocker}</code></> : "None"}</dd></div>
      <div><dt>Kill switch</dt><dd><Badge value={status.kill_switch ? "KILL_SWITCH_ACTIVE" : "OFF"}/></dd></div>
      <div><dt>Forced exit</dt><dd>{status.forced_exit_status.replaceAll("_", " ")}</dd></div>
      <div><dt>Last completed bar</dt><dd>{status.last_completed_bar ? `${timeIST(status.last_completed_bar.start)}–${timeIST(status.last_completed_bar.end)}` : "N/A"}</dd></div>
      <div><dt>Last observation</dt><dd>{stamp(status.last_observation_at)}</dd></div>
      <div><dt>Worker enabled</dt><dd>{status.worker_enabled == null ? "Unknown" : status.worker_enabled ? "Yes" : "No"}</dd></div>
    </dl>
  </Panel>;
}

function Journal({ status }: { status: AstStatus | null }) {
  const loader = useCallback((signal: AbortSignal) => astApi.events(25, signal), []);
  const { data, error } = usePolling(loader, 15000);
  return <Panel title="JOURNAL · AUDIT EVIDENCE">
    <dl className="commodity-fields">
      <div><dt>Journal sequence</dt><dd>{integer(data?.journal_sequence ?? status?.journal_sequence)}</dd></div>
      <div><dt>Verification</dt><dd>{status ? status.journal_verification : "N/A"}</dd></div>
      <div className="ast-hash"><dt>Journal head</dt><dd><code>{data?.journal_head ?? status?.journal_head ?? "N/A"}</code></dd></div>
    </dl>
    {error ? <SectionError>JOURNAL EVENTS UNAVAILABLE</SectionError> : <details className="research-disclosure"><summary>Latest lifecycle events ({integer(data?.total)})</summary>
      {!data ? <Empty>Loading journal events</Empty> : data.items.length ? <div className="table-wrap"><ResponsiveTable aria-label="Lifecycle journal events"><thead><tr><th>Seq</th><th>Time</th><th>Event</th><th>Trade</th><th>Evidence</th><th>Hash</th></tr></thead><tbody>
        {data.items.map(e => <tr key={e.sequence}><td>{e.sequence}</td><td>{timeIST(e.timestamp)}</td><td><Badge value={e.event_type}/></td><td>{e.trade_id ? e.trade_id.slice(0, 10) : "—"}</td>
          <td>{Object.entries(e.summary).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : String(v)}`).join(" · ") || "—"}</td><td><code>{e.event_hash.slice(0, 12)}</code></td></tr>)}
      </tbody></ResponsiveTable></div> : <Empty>No lifecycle events yet</Empty>}
    </details>}
  </Panel>;
}

export function AstFuturesMonitor() {
  // The clock is read once per poll, outside render, so ages refresh with every status response.
  const loader = useCallback((signal: AbortSignal) => astApi.status(signal).then(value => ({ value, now: Date.now() })), []);
  const poll = usePolling(loader, 5000);
  const { data, failure } = poll;
  const status = data?.value ?? null;
  const now = data?.now ?? 0;
  const active = status?.position && ACTIVE.includes(status.position.state);
  // The AST engine runs in its own isolated stack; elsewhere the bridge answers 503 SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED.
  // That is a deployment fact, not an outage: show it once and stop the dependent sections from polling.
  if (failure?.kind === "not_configured" && !status) return <AstNotConfigured code={failure.code} onRetry={() => void poll.refresh()}/>;
  return <>
    {failure && !status ? <SectionError>AST STATUS UNAVAILABLE — {describeFailure(failure)}. The read API or bridge is not reachable. Other sections keep loading independently. <RetryButton onRetry={() => void poll.refresh()}/></SectionError>
      : !status ? <Empty>Loading AST futures status</Empty> : <>
        <PollStatus poll={poll} label="AST STATUS"/>
        <StatusStrip status={status} now={now}/>
        <Alerts status={status} now={now}/>
        {active && <SectionBoundary label="Current paper position"><PositionCard status={status}/></SectionBoundary>}
        <div className="instrument-hero"><SectionBoundary label="Instrument"><InstrumentHero status={status} now={now}/></SectionBoundary>
          <SectionBoundary label="Frozen strategy"><FrozenStrategy status={status}/></SectionBoundary></div>
        {!active && <SectionBoundary label="Current paper position"><PositionCard status={status}/></SectionBoundary>}
        <div className="grid grid-2 ast-grid"><SectionBoundary label="Opening range"><RangeAndAst status={status} now={now}/></SectionBoundary>
          <SectionBoundary label="Decision evidence"><DecisionEvidence status={status}/></SectionBoundary></div>
        <SectionBoundary label="Today's performance"><TodayPanel status={status}/></SectionBoundary>
      </>}
    <SectionBoundary label="Forward paper performance"><ForwardPerformance/></SectionBoundary>
    <SectionBoundary label="Journal"><Journal status={status}/></SectionBoundary>
  </>;
}

export function AstNotConfigured({ code, onRetry }: { code: string | null; onRetry: () => void }) {
  return <section className="panel ast-not-configured" aria-label="AST Futures not configured" role="status">
    <header className="panel-head"><h2 className="panel-title">NOT CONFIGURED IN THIS DEPLOYMENT</h2><Badge value="NOT_CONFIGURED" tone="neutral"/></header>
    <div className="panel-body">
      <p><strong>AST Futures runs in its isolated stack; not configured in this deployment.</strong></p>
      <p className="muted">The read-only bridge reported {code ?? "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED"}: either this deployment has no AST Direct origin, or the isolated AST stack is unreachable from it. No AST data is shown rather than guessed. Open the dashboard served by the AST stack to monitor it.</p>
      <RetryButton onRetry={onRetry} label="Check again"/>
    </div>
  </section>;
}

export function AstPaperBanner() {
  return <><p className="command-banner ast-paper-banner"><Icon name="shield"/><strong>PAPER ONLY · NO BROKER ORDERS</strong><span>live trading disabled</span></p>
    <p className="execution-note"><strong>FUTURES_LTP_PAPER_V1</strong> — paper fills use the observed NIFTY futures LTP as a proxy. They are not exchange bid/ask or broker executions, and no spread or depth is represented.</p></>;
}

"use client";
import Link from "next/link";
import { useCallback } from "react";
import { usePolling } from "@/hooks/use-polling";
import { enginesApi } from "@/lib/engines";
import { ageLabel, dash, DASH, integer, money, number, parseTimestamp, pretty, stampIST, timeIST } from "@/lib/format";
import { Icon } from "@/components/icons";
import { PollStatus } from "@/components/poll-status";
import { ResponsiveTable } from "@/components/responsive-table";
import { Badge, Empty } from "@/components/ui";
import type { Engine, EngineMode, EngineState } from "@/types/engines";

/** Every state has its own colour AND its own text; colour is never the only signal. */
export const STATE_TONE: Record<EngineState, string> = {
  RUNNING: "good", IDLE: "neutral", MARKET_CLOSED: "accent", DATA_STALE: "warn", ERROR: "bad", DISABLED: "neutral", NOT_CONFIGURED: "neutral",
};
export function EngineStateBadge({ state }: { state: EngineState }) {
  return <span className="engine-state" data-state={state}><Badge value={state} tone={STATE_TONE[state]}/></span>;
}
export function ModeBadge({ mode }: { mode: EngineMode | null }) {
  if (mode === "LIVE") return <span className="live-warning" role="alert" aria-label="LIVE mode warning"><Icon name="shield" size={14}/>LIVE MODE · real broker orders possible</span>;
  return mode ? <Badge value={mode} tone={mode === "PAPER" ? "accent" : "neutral"}/> : <span className="muted">Mode {DASH}</span>;
}
const tone = (value: number | null) => value == null ? "" : value > 0 ? "positive" : value < 0 ? "negative" : "";
const pnl = (value: number | null) => dash(value, money);
const age = (value: string | null, now: number) => {
  const at = parseTimestamp(value);
  return at == null ? DASH : `${ageLabel(Math.max(0, Math.floor((now - at) / 1000)))} ago`;
};
const positionText = (engine: Engine) => {
  const p = engine.active_position;
  if (!p) return "Flat";
  return `${p.direction}${p.contract ? ` ${p.contract}` : ""} · qty ${dash(p.quantity, integer)} @ ${dash(p.entry_price, v => number(v))}`;
};
const decisionText = (engine: Engine) => {
  const d = engine.last_decision;
  if (!d) return DASH;
  return [d.status ? pretty(d.status) : DASH, d.direction, d.score == null ? null : `score ${number(d.score, 1)}`, d.at ? timeIST(d.at) : null].filter(Boolean).join(" · ");
};

export function EngineCard({ engine, now }: { engine: Engine; now: number }) {
  const position = engine.active_position;
  return <article className={`engine-card ${engine.mode === "LIVE" ? "is-live" : ""}`} aria-label={`${engine.label} engine`} data-state={engine.state}>
    <header>
      <div><h3>{engine.detail_href ? <Link href={engine.detail_href}>{engine.label}</Link> : engine.label}</h3>
        <small>{pretty(engine.family)}{engine.version ? ` · ${engine.version}` : ""}</small></div>
      <EngineStateBadge state={engine.state}/>
    </header>
    <div className="engine-badges"><ModeBadge mode={engine.mode}/>{engine.instrument && <Badge value={engine.instrument} tone="neutral"/>}</div>
    {engine.state_reason && <p className="engine-reason">{engine.state_reason}</p>}
    <dl className="engine-fields">
      <div><dt>Last update</dt><dd title={stampIST(engine.last_update)}>{age(engine.last_update, now)}</dd></div>
      <div><dt>Trades today</dt><dd>{dash(engine.trades_today, integer)}</dd></div>
      <div><dt>Realized today</dt><dd className={tone(engine.realized_pnl_today)}>{pnl(engine.realized_pnl_today)}</dd></div>
      <div><dt>Unrealized (open)</dt><dd className={tone(engine.unrealized_pnl)}>{pnl(engine.unrealized_pnl)}</dd></div>
      <div className="engine-wide"><dt>Position</dt><dd>{positionText(engine)}{position && <small>Mark {dash(position.mark_price, v => number(v))} · {position.mark_at ? timeIST(position.mark_at) : `mark time ${DASH}`}</small>}</dd></div>
      <div className="engine-wide"><dt>Last decision</dt><dd>{decisionText(engine)}</dd></div>
      <div className="engine-wide"><dt>Latest block reason</dt><dd>{engine.last_decision?.primary_blocker ? <code>{engine.last_decision.primary_blocker}</code> : DASH}</dd></div>
    </dl>
  </article>;
}

function useEngines() {
  const loader = useCallback((signal: AbortSignal) => enginesApi.status(signal).then(value => ({ value, now: Date.now() })), []);
  return usePolling(loader, 15000);
}

const NOT_CONFIGURED = <p><strong>ENGINE STATUS NOT CONFIGURED</strong> · /api/engines/status is not available in this deployment.</p>;

/** Overview strip on `/`: one card per engine, above the existing ACTS workspace. */
export function EnginesOverview() {
  const poll = useEngines();
  const status = poll.data?.value;
  return <section className="engines-overview" aria-label="Engines overview">
    <div className="section-title"><div><h2>Engines</h2><p>Every paper, research and futures engine · read-only{status ? ` · as of ${timeIST(status.generated_at)}` : ""}</p></div>
      <Link href="/engines">All engines<Icon name="arrow" size={13}/></Link></div>
    <PollStatus poll={poll} label="ENGINE STATUS" notConfigured={NOT_CONFIGURED}/>
    {!status ? !poll.failure && <div className="engine-grid is-loading" aria-busy="true">Loading engine status…</div>
      : status.engines.length ? <div className="engine-grid">{status.engines.map(engine => <EngineCard key={engine.id} engine={engine} now={poll.data!.now}/>)}</div>
      : <Empty>No engines reported</Empty>}
  </section>;
}

/** `/engines`: one row per engine; horizontally scrollable with a sticky engine column. */
export function EnginesTable() {
  const poll = useEngines();
  const status = poll.data?.value;
  const now = poll.data?.now ?? 0;
  const live = status?.engines.filter(engine => engine.mode === "LIVE") ?? [];
  return <>
    <PollStatus poll={poll} label="ENGINE STATUS" notConfigured={NOT_CONFIGURED}/>
    {live.length > 0 && <p className="live-warning is-banner" role="alert"><Icon name="shield" size={15}/>{live.length} engine{live.length > 1 ? "s" : ""} report LIVE mode: {live.map(e => e.label).join(", ")}. This dashboard has no controls; verify broker exposure directly.</p>}
    {!status ? !poll.failure && <Empty>Loading engine status</Empty> : !status.engines.length ? <Empty>No engines reported</Empty> :
      <div className="table-wrap sticky-first"><ResponsiveTable aria-label="Engine status" data-layout="scroll"><thead><tr>
        <th>Engine</th><th>State</th><th>Mode</th><th>Instrument</th><th>Last update</th><th>Position</th><th>Trades today</th>
        <th>Realized today</th><th>Unrealized</th><th>Last decision</th><th>Block reason</th></tr></thead><tbody>
        {status.engines.map(engine => <tr key={engine.id} data-state={engine.state}>
          <th scope="row">{engine.detail_href ? <Link href={engine.detail_href}>{engine.label}</Link> : engine.label}<small>{pretty(engine.family)}{engine.version ? ` · ${engine.version}` : ""}</small></th>
          <td><EngineStateBadge state={engine.state}/>{engine.state_reason && <small>{engine.state_reason}</small>}</td>
          <td className="mode-cell"><ModeBadge mode={engine.mode}/></td>
          <td>{engine.instrument ?? DASH}</td>
          <td>{age(engine.last_update, now)}<small>{stampIST(engine.last_update)}</small></td>
          <td>{positionText(engine)}{engine.active_position && <small>Mark {dash(engine.active_position.mark_price, v => number(v))} · {engine.active_position.mark_at ? timeIST(engine.active_position.mark_at) : DASH}</small>}</td>
          <td>{dash(engine.trades_today, integer)}</td>
          <td className={tone(engine.realized_pnl_today)}>{pnl(engine.realized_pnl_today)}</td>
          <td className={tone(engine.unrealized_pnl)}>{pnl(engine.unrealized_pnl)}</td>
          <td>{decisionText(engine)}</td>
          <td>{engine.last_decision?.primary_blocker ? <code>{engine.last_decision.primary_blocker}</code> : DASH}</td>
        </tr>)}
      </tbody></ResponsiveTable></div>}
    {status && <p className="accounting-note">Realized P&amp;L is closed paper trades today; unrealized is open positions at their last mark. They are never summed here. {DASH} means the engine did not report a value.</p>}
  </>;
}

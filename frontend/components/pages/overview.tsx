"use client";
import Link from "next/link";
import { useMemo } from "react";
import { useUrlState } from "@/components/filters-context";
import { DimensionBadges, latestBlocker } from "@/components/pages/dimensions";
import { useDrawerLinks } from "@/components/pages/drawers";
import { legsExpander, openColumns, rowClass } from "@/components/pages/position-columns";
import { AlertBanner } from "@/components/terminal/alerts";
import { IntradayChart } from "@/components/terminal/charts";
import { DataTable } from "@/components/terminal/data-table";
import { Metrics } from "@/components/pages/metrics";
import { EmptyState, ResourceView } from "@/components/terminal/states";
import { PageTitle, Panel, PnL, StatusBadge, StrategyChip, Val } from "@/components/terminal/primitives";
import { useResource } from "@/hooks/use-resource";
import { entityParams, filterParams, istToday, windowOf } from "@/lib/filters";
import { parse, urls } from "@/lib/portfolio-api";
import { int, istClock, istDateTime, num, title } from "@/lib/terminal-format";
import type { Position, StrategyCard } from "@/types/portfolio";

export function OpenPositionsTable({ compact = true, limit = 50 }: { compact?: boolean; limit?: number }) {
  const { filters } = useUrlState();
  const { openPosition } = useDrawerLinks();
  const base = { ...entityParams(filters), limit, offset: 0 };
  const open = useResource(urls.positions({ ...base, tab: "open" }), parse.positions);
  const unresolved = useResource(urls.positions({ ...base, tab: "unresolved" }), parse.positions);
  const rows = useMemo(() => {
    const seen = new Set<string>(), out: Position[] = [];
    for (const p of [...(unresolved.data?.items ?? []), ...(open.data?.items ?? [])]) if (!seen.has(p.position_id)) { seen.add(p.position_id); out.push(p); }
    return out;
  }, [open.data, unresolved.data]);
  const cols = useMemo(() => openColumns({ onOpen: p => openPosition(p.position_id), compact, showVersion: !compact }), [compact]); // eslint-disable-line react-hooks/exhaustive-deps
  const primary = open.data || !unresolved.data ? open : unresolved;
  return <ResourceView resource={primary} label="Open positions" skeletonRows={3}>
    {() => rows.length ? <DataTable label="Open positions" rows={rows} rowKey={p => p.position_id} columns={cols} rowClass={rowClass} expand={legsExpander} onRowClick={p => openPosition(p.position_id)}/> : <EmptyState hint="Entries appear here when an engine opens a paper position.">No open positions</EmptyState>}
  </ResourceView>;
}

export function StrategyTable({ cards }: { cards: StrategyCard[] }) {
  const cols = [
    { key: "strategy", header: "Strategy", render: (c: StrategyCard) => <Link className="tm-link" href={`/strategies/${encodeURIComponent(c.engine_id)}`}><StrategyChip label={c.label} family={c.family}/></Link> },
    { key: "version", header: "Version", render: (c: StrategyCard) => <span>{c.version}{c.retired ? <> <StatusBadge value="RETIRED" tone="na"/></> : null}</span> },
    { key: "instrument", header: "Instrument", render: (c: StrategyCard) => c.instrument },
    { key: "mode", header: "Mode", render: (c: StrategyCard) => <StatusBadge value={c.mode} tone="info"/> },
    { key: "state", header: "Operational state", render: (c: StrategyCard) => <span title={c.state_reason ?? undefined}><StatusBadge dimension="state" value={c.operational_state} label="State"/></span> },
    { key: "dims", header: "Dimensions", render: (c: StrategyCard) => <DimensionBadges card={c}/> },
    { key: "trades", header: "Trades", align: "right" as const, render: (c: StrategyCard) => int(c.today.trades) },
    { key: "open", header: "Open", align: "right" as const, render: (c: StrategyCard) => c.unresolved > 0 ? <span className="pnl-neg" title="Includes unresolved exposure">{int(c.today.open_positions)} ⚠</span> : int(c.today.open_positions) },
    { key: "realized", header: "Realized net", align: "right" as const, render: (c: StrategyCard) => <PnL value={c.today.realized_net}/> },
    { key: "unrealized", header: "Unrealized", align: "right" as const, render: (c: StrategyCard) => <PnL value={c.today.unrealized_gross} reason={c.today.partial ? "open mark excluded" : null}/> },
    { key: "net", header: "Net", align: "right" as const, render: (c: StrategyCard) => <span><PnL value={c.today.total_net}/>{c.today.partial && <small className="pnl-na" style={{ display: "block", color: "var(--tm-warn)" }}>PARTIAL</small>}</span> },
    { key: "last", header: "Last activity", render: (c: StrategyCard) => istDateTime(c.last_update) },
    { key: "blocker", header: "Latest blocker", render: (c: StrategyCard) => <Val>{latestBlocker(c) ? title(latestBlocker(c)) : null}</Val> },
  ];
  return <DataTable label="Strategy performance" rows={cards} rowKey={c => c.engine_id} columns={cols} rowClass={c => c.unresolved > 0 ? "is-critical" : undefined}/>;
}

function RecentActivity() {
  const { filters } = useUrlState();
  const { openOrder } = useDrawerLinks();
  const orders = useResource(urls.orders({ ...entityParams(filters), limit: 10 }), parse.orders);
  const fills = useResource(urls.fills({ limit: 10 }), parse.fills);
  return <>
    <Panel title="Recent orders (simulated)" flush>
      <ResourceView resource={orders} label="Orders" isEmpty={d => d.items.length === 0} empty={<EmptyState>No orders yet</EmptyState>} skeletonRows={3}>{d =>
        <DataTable label="Recent orders" rows={d.items} rowKey={o => o.order_id} onRowClick={openOrder} columns={[
          { key: "t", header: "Time", render: o => <button type="button" className="tm-link" onClick={() => openOrder(o)} aria-label={`Open order ${o.order_id}`}>{istDateTime(o.submitted_at)}</button> },
          { key: "s", header: "Strategy", render: o => <StrategyChip label={o.strategy_label}/> },
          { key: "c", header: "Contract", render: o => <span className="tm-mono">{o.contract ?? "—"}</span> },
          { key: "side", header: "Side", render: o => `${o.side} ${o.intent}` },
          { key: "q", header: "Qty", align: "right", render: o => int(o.filled_quantity) },
          { key: "p", header: "Avg fill", align: "right", render: o => o.avg_fill_price == null ? "—" : num(o.avg_fill_price) },
          { key: "st", header: "Status", render: o => <StatusBadge dimension="order" value={o.status}/> },
        ]}/>}</ResourceView>
    </Panel>
    <Panel title="Recent fills (simulated)" flush>
      <ResourceView resource={fills} label="Fills" isEmpty={d => d.items.length === 0} empty={<EmptyState>No fills yet</EmptyState>} skeletonRows={3}>{d =>
        <DataTable label="Recent fills" rows={d.items} rowKey={f => f.fill_id} columns={[
          { key: "t", header: "Time", render: f => istClock(f.at) }, { key: "c", header: "Contract", render: f => <span className="tm-mono">{f.contract ?? "—"}</span> },
          { key: "s", header: "Side", render: f => `${f.side} ${f.intent}` }, { key: "q", header: "Qty", align: "right", render: f => int(f.quantity) }, { key: "p", header: "Price", align: "right", render: f => num(f.price) },
        ]}/>}</ResourceView>
    </Panel>
  </>;
}

export function Overview() {
  const { filters } = useUrlState();
  const win = windowOf(filters, "today");
  const summary = useResource(urls.summary(filterParams(filters, "today")), parse.summary);
  const alerts = useResource(urls.alerts(), parse.alerts);
  const risk = useResource(urls.risk(entityParams(filters)), parse.risk);
  const strategies = useResource(urls.strategies(), parse.strategies);
  const equity = useResource(urls.equity({ ...entityParams(filters), date: win.to ?? istToday() }), parse.equity);
  const cards = (strategies.data?.items ?? []).filter(c => (!filters.engine || c.engine_id === filters.engine) && (!filters.instrument || c.instrument === filters.instrument) && (!filters.version || c.version === filters.version));
  const critical = (alerts.data?.items ?? []).filter(a => a.severity === "CRITICAL");
  const attention = summary.data?.attention ?? [];
  const live = filters.mode === "LIVE";
  return <div>
    <PageTitle title="Overview" note={<>{win.from ? (win.from === win.to ? `Session ${win.from}` : `${win.from} → ${win.to ?? "now"}`) : "All dates"} · {filters.mode} · Asia/Kolkata</>}/>
    {live ? <EmptyState hint="No live execution path exists in this deployment. LIVE totals are never mixed with PAPER.">LIVE: no live engines</EmptyState> : <>
      {critical.length > 0 ? <AlertBanner items={critical}/> : attention.length > 0 && <div className="tm-banner" data-tone="crit" role="alert"><b>{attention.length} NEEDS ATTENTION</b><ul>{attention.map(a => <li key={a.position_id}>{a.label}: {a.contract} <StatusBadge value={a.flags[0] ?? "UNRESOLVED"} tone="crit"/></li>)}</ul></div>}
      <ResourceView resource={summary} label="Summary" skeletonRows={3}>{s => <Metrics summary={s} risk={risk.data} riskFailed={!!risk.failure && !risk.data}/>}</ResourceView>
      <Panel title="Open positions" actions={<Link className="tm-link" href="/positions">All positions</Link>} flush><OpenPositionsTable/></Panel>
      <div className="tm-grid main-side">
        <Panel title={`Intraday P&L · ${win.to ?? istToday()}`}>
          <ResourceView resource={equity} label="Intraday P&L" skeletonRows={4}>{e => e.available ? <>
            <IntradayChart equity={e}/>
            <p className="tm-caption">{e.source ?? "Event-sourced from the journal"}. Dashed segments: a minute where an open position had no executable mark (partial). {e.note ?? ""}</p>
          </> : <EmptyState hint="No chart is drawn without recorded marks, entries or exits.">{e.note ?? "No intraday data for this date"}</EmptyState>}</ResourceView>
        </Panel>
        <div><RecentActivity/></div>
      </div>
      <Panel title="Strategy performance · today" actions={<Link className="tm-link" href="/strategies">All strategies</Link>} flush>
        <ResourceView resource={strategies} label="Strategies" isEmpty={() => cards.length === 0} empty={<EmptyState>No strategy matches the filters</EmptyState>}>{() => <StrategyTable cards={cards}/>}</ResourceView>
      </Panel>
    </>}
  </div>;
}

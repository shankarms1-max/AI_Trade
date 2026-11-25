"use client";
import { useUrlState } from "@/components/filters-context";
import { useDrawerLinks } from "@/components/pages/drawers";
import { shortContract } from "@/components/pages/position-columns";
import { DataTable, type Column } from "@/components/terminal/data-table";
import { EmptyState, LiveGate, ResourceView } from "@/components/terminal/states";
import { FlagBadges, KV, MetricTile, PageTitle, Panel, PnL, StatusBadge, Tiles, Unavailable, Val } from "@/components/terminal/primitives";
import { LIVE_INTERVAL, useResource } from "@/hooks/use-resource";
import { entityParams } from "@/lib/filters";
import { parse, urls } from "@/lib/portfolio-api";
import { int, istDateTime, pctFraction, rupees } from "@/lib/terminal-format";
import type { Risk as RiskData, RiskEngine } from "@/types/portfolio";

const money = (v: number | null, reason = "limit not available") => <Val reason={reason}>{v == null ? null : rupees(v, 0)}</Val>;

const engineCols: Array<Column<RiskEngine>> = [
  { key: "e", header: "Strategy", sticky: true, render: e => <b>{e.label}</b> },
  { key: "open", header: "Open", align: "right", render: e => int(e.open_positions) },
  { key: "risk", header: "Open position risk", align: "right", title: "Defined maximum loss of open positions", render: e => money(e.open_position_risk, "not computed") },
  { key: "dr", header: "Daily realized net", align: "right", render: e => <PnL value={e.daily_realized_net}/> },
  { key: "dl", header: "Daily realized loss", align: "right", render: e => money(e.daily_realized_loss, "not computed") },
  { key: "lim", header: "Daily loss limit", align: "right", render: e => money(e.daily_loss_limit) },
  { key: "rem", header: "Remaining budget", align: "right", render: e => e.remaining_daily_loss_budget == null ? <Unavailable reason={e.daily_loss_limit == null ? "no deployed limit" : undefined}/> : <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
    {e.daily_loss_limit ? <span className="tm-bar" aria-hidden="true" data-tone={e.remaining_daily_loss_budget / e.daily_loss_limit < 0.25 ? "neg" : e.remaining_daily_loss_budget / e.daily_loss_limit < 0.5 ? "warn" : "pos"}><i style={{ width: `${Math.max(0, Math.min(100, (e.remaining_daily_loss_budget / e.daily_loss_limit) * 100))}%` }}/></span> : null}
    {rupees(e.remaining_daily_loss_budget, 0)}</span> },
  { key: "mpt", header: "Max risk / trade", align: "right", render: e => money(e.max_risk_per_trade) },
  { key: "ent", header: "Entries today / max", align: "right", render: e => `${int(e.entries_today)} / ${e.max_entries_per_day == null ? "—" : int(e.max_entries_per_day)}` },
  { key: "stop", header: "Stop coverage", align: "right", render: e => e.stop_coverage.open === 0 ? <span className="tm-dash" title="No open positions">n/a</span> : <span className={e.stop_coverage.with_stop < e.stop_coverage.open ? "pnl-neg" : ""}>{e.stop_coverage.with_stop} / {e.stop_coverage.open}</span> },
  { key: "src", header: "Limits source", render: e => <span className="tm-muted" title={e.limits_source ?? undefined}>{e.limits_source ?? "—"}</span> },
];

function Exposure({ risk }: { risk: RiskData }) {
  const x = risk.exposure;
  return <Panel title="Exposure">
    <Tiles>
      <MetricTile label="Defined risk (open)" value={money(x.defined_risk_total, "not computed")} note="sum of defined maximum loss"/>
      <MetricTile label="Futures long notional" value={money(x.futures_long_notional, "not computed")} note="contract value, not margin"/>
      <MetricTile label="Futures short notional" value={money(x.futures_short_notional, "not computed")} note="contract value, not margin"/>
      <MetricTile label="Margin utilisation" value={x.margin_utilization == null ? <Unavailable/> : pctFraction(x.margin_utilization)} note={x.margin_utilization == null ? "broker margin is not recorded and is never assumed" : null}/>
      <MetricTile label="Capital allocation" value={x.capital_allocation == null ? <Unavailable/> : rupees(x.capital_allocation, 0)} note={x.capital_allocation == null ? (x.capital_note ?? "no paper capital configured") : null}/>
    </Tiles>
    {x.notional_note && <p className="tm-caption">{x.notional_note}</p>}
    {x.capital_note && <p className="tm-caption">{x.capital_note}</p>}
  </Panel>;
}

export function Risk() {
  const { filters } = useUrlState();
  const { openPosition } = useDrawerLinks();
  const live = filters.mode === "LIVE";
  const risk = useResource(live ? null : urls.risk(entityParams(filters)), parse.risk, LIVE_INTERVAL, { keepPrevious: "risk" });
  return <div>
    <PageTitle title="Risk" note={`${filters.mode} · per-engine limits come from each engine's deployed policy · as of now`}/>
    <LiveGate live={live}>
      <ResourceView resource={risk} label="Risk" skeletonRows={5}>{r => <>
        {r.unresolved_exposure.length > 0 && <div className="tm-banner" data-tone="crit" role="alert"><b>UNRESOLVED EXPOSURE</b><div>{r.unresolved_exposure.length} position(s) expired, past forced exit or unpriced. Their risk is not netted into any total.</div></div>}
        <Panel title="Risk by engine" flush>
          <DataTable label="Risk by engine" rows={r.engines} rowKey={e => e.engine_id} columns={engineCols} empty={<EmptyState>No engines match the filters</EmptyState>}/>
        </Panel>
        <Exposure risk={r}/>
        <div className="tm-grid fit">
          <Panel title="Concentration (share of open risk)" flush>
            <DataTable label="Concentration" rows={r.concentration} rowKey={c => c.engine_id} columns={[
              { key: "e", header: "Engine", render: c => r.engines.find(e => e.engine_id === c.engine_id)?.label ?? c.engine_id },
              { key: "s", header: "Share of open risk", align: "right", render: c => c.share_of_open_risk == null ? <Unavailable/> : <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><span className="tm-bar" aria-hidden="true"><i style={{ width: `${Math.min(100, c.share_of_open_risk * 100)}%` }}/></span>{pctFraction(c.share_of_open_risk)}</span> },
            ]}/>
          </Panel>
          <Panel title="Unresolved exposure" flush>
            <DataTable label="Unresolved exposure" rows={r.unresolved_exposure} rowKey={u => u.position_id} empty={<EmptyState>No unresolved exposure</EmptyState>} rowClass={() => "is-critical"} columns={[
              { key: "p", header: "Position", render: u => <button type="button" className="tm-link" onClick={() => openPosition(u.position_id)}>{u.label}: {shortContract(u.contract)}</button> },
              { key: "f", header: "Flags", render: u => <FlagBadges flags={u.flags}/> },
              { key: "l", header: "Defined max loss", align: "right", render: u => money(u.defined_max_loss, "not computed") },
            ]}/>
          </Panel>
        </div>
        <Panel title="Limitations">
          <ul className="tm-list">{r.limitations.map(l => <li key={l}>{l}</li>)}</ul>
          <KV items={[["Mode", <StatusBadge key="m" value={r.mode} tone="info"/>], ["As of", istDateTime(r.as_of)]]}/>
        </Panel>
      </>}</ResourceView>
    </LiveGate>
  </div>;
}

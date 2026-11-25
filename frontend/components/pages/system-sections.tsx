"use client";
import { DataTable } from "@/components/terminal/data-table";
import { EmptyState, ResourceView } from "@/components/terminal/states";
import { PageTitle, Panel, StatusBadge, StrategyChip, Val } from "@/components/terminal/primitives";
import { ANALYTICS_INTERVAL, LIVE_INTERVAL, useResource } from "@/hooks/use-resource";
import { parseEngineStatus } from "@/lib/engines";
import { parse, urls } from "@/lib/portfolio-api";
import { istDateTime, num, title } from "@/lib/terminal-format";
import type { EngineStatus } from "@/types/engines";
import type { Sources } from "@/types/portfolio";

const parseEngines = (json: unknown): EngineStatus => parseEngineStatus(json, "");

export function ChargeModel({ sources }: { sources: Sources }) {
  const rows = Object.entries(sources.charge_model);
  return <Panel title="Charge model status" flush>
    <DataTable label="Charge model" rows={rows} rowKey={([k]) => k} columns={[
      { key: "c", header: "Component", render: ([k]) => title(k) },
      { key: "s", header: "Status", render: ([, v]) => <StatusBadge value={v.split(/[\s:(]/)[0]} title={v}/> },
      { key: "d", header: "Basis", render: ([, v]) => <span className="tm-muted">{v}</span> },
    ]}/>
    <p className="tm-caption" style={{ padding: "0 10px" }}>Brokerage per order assumed: <b>₹{num(sources.brokerage_per_order ?? 0)}</b>. Only statutory components are fixed; UNVERIFIED / APPROX components are labelled wherever charges are shown. Charges are computed by the backend, never in the browser.</p>
  </Panel>;
}

/** Top section of System Health: data sources, per-engine feed state, charge model. The legacy operational console renders below. */
export function SystemSections() {
  const sources = useResource(urls.sources(), parse.sources, ANALYTICS_INTERVAL);
  const engines = useResource(urls.enginesStatus(), parseEngines, LIVE_INTERVAL);
  return <div>
    <PageTitle title="System health" note="Read-only. Data sources, engine feeds and the charge model first; the detailed operations console follows."/>
    <Panel title="Data sources" flush>
      <ResourceView resource={sources} label="Data sources" isEmpty={d => d.sources.length === 0} empty={<EmptyState>No sources reported</EmptyState>}>{d =>
        <DataTable label="Data sources" rows={d.sources} rowKey={s => s.source} columns={[
          { key: "s", header: "Source", render: s => <span className="tm-mono">{s.source}</span> },
          { key: "st", header: "Status", render: s => <StatusBadge value={s.status} dimension="none"/> },
          { key: "r", header: "Records", align: "right", render: s => s.records.toLocaleString("en-IN") },
          { key: "n", header: "Note", render: s => <span className="tm-muted">{s.note ?? "—"}</span> },
        ]}/>}</ResourceView>
    </Panel>
    <Panel title="Engines · feed state" flush>
      <ResourceView resource={engines} label="Engines" isEmpty={d => d.engines.length === 0} empty={<EmptyState>No engines reported</EmptyState>}>{d =>
        <DataTable label="Engines" rows={d.engines} rowKey={e => e.id} columns={[
          { key: "e", header: "Engine", sticky: true, render: e => <StrategyChip label={e.label} family={e.family} version={e.version}/> },
          { key: "m", header: "Mode", render: e => e.mode ? <StatusBadge value={e.mode} tone={e.mode === "LIVE" ? "neg" : "info"}/> : <Val>{null}</Val> },
          { key: "f", header: "Feed / state", render: e => <span title={e.state_reason ?? undefined}><StatusBadge dimension="state" value={e.state} label="State"/></span> },
          { key: "r", header: "Reason", render: e => <span className="tm-muted">{e.state_reason ? title(e.state_reason) : "—"}</span> },
          { key: "u", header: "Last update", render: e => istDateTime(e.last_update) },
          { key: "d", header: "Last decision", render: e => e.last_decision ? `${title(e.last_decision.status)}${e.last_decision.primary_blocker ? ` · ${title(e.last_decision.primary_blocker)}` : ""}` : "—" },
        ]}/>}</ResourceView>
    </Panel>
    <ResourceView resource={sources} label="Charge model" skeletonRows={2}>{d => <ChargeModel sources={d}/>}</ResourceView>
  </div>;
}

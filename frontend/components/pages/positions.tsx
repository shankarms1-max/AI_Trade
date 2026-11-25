"use client";
import { useMemo } from "react";
import { useUrlState } from "@/components/filters-context";
import { useDrawerLinks } from "@/components/pages/drawers";
import { allColumns, closedColumns, legsExpander, openColumns, rowClass } from "@/components/pages/position-columns";
import { AlertBanner } from "@/components/terminal/alerts";
import { DataTable, Pager, TABLE_MAX } from "@/components/terminal/data-table";
import { EmptyState, LiveGate, ResourceView } from "@/components/terminal/states";
import { FilterSelect } from "@/components/terminal/filters";
import { PageTitle, Panel, TabPanel, Tabs } from "@/components/terminal/primitives";
import { LIVE_INTERVAL, useResource } from "@/hooks/use-resource";
import { entityParams, filterParams } from "@/lib/filters";
import { parse, urls } from "@/lib/portfolio-api";

export const POSITION_TABS = ["open", "closed", "unresolved", "all"] as const;
export type PositionTab = typeof POSITION_TABS[number];
export const PAGE_SIZE = 50;
const DIRECTIONS = ["BULL", "BEAR", "LONG", "SHORT"];
const EMPTY_TEXT: Record<PositionTab, string> = { open: "No open positions", closed: "No closed positions for these filters", unresolved: "Nothing unresolved: no position needs attention", all: "No positions for these filters" };

/** Query for GET /api/portfolio/positions: global filters + tab + direction + server pagination. Pure so tests can assert it. */
export function positionsQuery(filters: Parameters<typeof filterParams>[0], tab: PositionTab, direction: string | null, offset: number) {
  return { ...filterParams(filters, "all"), tab, direction, limit: PAGE_SIZE, offset };
}

/** `engine` pins the list to one strategy (used inside /strategies/[engineId]); otherwise the global filters apply. */
export function Positions({ engine }: { engine?: string } = {}) {
  const { filters: global, get, set } = useUrlState();
  const filters = engine ? { ...global, engine } : global;
  const { openPosition } = useDrawerLinks();
  const rawTab = get("tab");
  const tab: PositionTab = (POSITION_TABS as readonly string[]).includes(rawTab ?? "") ? rawTab as PositionTab : "open";
  const direction = get("direction");
  const offset = Math.max(0, Number(get("offset")) || 0);
  const selected = get("position");
  const live = filters.mode === "LIVE";
  const list = useResource(live ? null : urls.positions(positionsQuery(filters, tab, direction, offset)), parse.positions, LIVE_INTERVAL, { keepPrevious: tab });
  // Counts for every tab come from a tiny shared request so they stay visible while another tab loads.
  const counts = useResource(live ? null : urls.positions({ ...entityParams(filters), tab: "open", limit: 1, offset: 0 }), parse.positions);
  const alerts = useResource(urls.alerts(), parse.alerts);
  const tc = counts.data?.tab_counts ?? list.data?.tab_counts;
  const cols = useMemo(() => {
    const o = { onOpen: (p: { position_id: string }) => openPosition(p.position_id), showVersion: true };
    return tab === "closed" ? closedColumns(o) : tab === "all" ? allColumns(o) : openColumns(o);
  }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps
  return <div>
    {!engine && (<PageTitle title="Positions" note={`${filters.mode} · simulated paper positions · server-paginated (${PAGE_SIZE}/page)`}/>)}
    <LiveGate live={live}>
      <AlertBanner items={alerts.data?.items ?? []}/>
      <Tabs label="Position tabs" value={tab} onChange={t => set({ tab: t === "open" ? null : t, offset: null })} tabs={[
        { id: "open", label: "Open", count: tc?.open ?? null }, { id: "closed", label: "Closed", count: tc?.closed ?? null },
        { id: "unresolved", label: "Unresolved", count: tc?.unresolved ?? null, tone: tc && tc.unresolved > 0 ? "neg" : undefined },
        { id: "all", label: "All", count: tab === "all" ? list.data?.total ?? null : null },
      ]}/>
      <TabPanel id={tab}>
        <div className="tm-filter-row" style={{ padding: "0 0 8px", background: "none" }} role="group" aria-label="Position filters">
          <FilterSelect label="Direction" value={direction} options={DIRECTIONS} onChange={v => set({ direction: v, offset: null })}/>
          {(tab === "closed" || tab === "all") && !filters.range && <span className="tm-muted" style={{ fontSize: 11 }}>Date range: all (set a range in the header to narrow closed positions)</span>}
        </div>
        <Panel title={`${tab[0].toUpperCase()}${tab.slice(1)} positions`} flush actions={list.placeholder ? <span className="tm-muted">updating…</span> : null}>
          <ResourceView resource={list} label="Positions" isEmpty={d => d.items.length === 0} empty={<EmptyState>{EMPTY_TEXT[tab]}</EmptyState>}>{d => <>
            <DataTable maxHeight={TABLE_MAX} label={`${tab} positions`} rows={d.items} rowKey={p => p.position_id} columns={cols} rowClass={rowClass} expand={legsExpander} selectedKey={selected} onRowClick={p => openPosition(p.position_id)}/>
            <div style={{ padding: "0 8px 6px" }}><Pager total={d.total} limit={d.limit} offset={d.offset} onOffset={o => set({ offset: o ? String(o) : null })} noun="positions"/></div>
          </>}</ResourceView>
        </Panel>
      </TabPanel>
    </LiveGate>
  </div>;
}

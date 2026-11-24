"use client";
import { useMemo, type ReactNode } from "react";
import { defaultRangeFor, isDatedPath, useUrlState } from "@/components/filters-context";
import { RANGES, RANGE_LABEL, type RangeKey } from "@/lib/filters";
import { parse, urls } from "@/lib/portfolio-api";
import { STATIC_INTERVAL, useResource } from "@/hooks/use-resource";

export function DateRangeSelector({ value, from, to, onChange, disabled = false }: {
  value: RangeKey; from: string | null; to: string | null; onChange: (range: RangeKey, from: string | null, to: string | null) => void; disabled?: boolean;
}) {
  return <span className="tm-hitem" role="group" aria-label="Date range">
    <span className="tm-segment">
      {RANGES.map(r => <button key={r} type="button" className="tm-btn" aria-pressed={value === r} disabled={disabled} onClick={() => onChange(r, r === "custom" ? from : null, r === "custom" ? to : null)}>{RANGE_LABEL[r]}</button>)}
    </span>
    {value === "custom" && <>
      <label className="tm-field">From<input className="tm-input" type="date" value={from ?? ""} onChange={e => onChange("custom", e.target.value || null, to)} aria-label="From date"/></label>
      <label className="tm-field">To<input className="tm-input" type="date" value={to ?? ""} onChange={e => onChange("custom", from, e.target.value || null)} aria-label="To date"/></label>
    </>}
  </span>;
}
export function FilterSelect({ label, value, options, onChange, all = "All" }: { label: string; value: string | null; options: string[]; onChange: (value: string | null) => void; all?: string }) {
  const list = value && !options.includes(value) ? [value, ...options] : options;
  return <label className="tm-field">{label}
    <select className="tm-select" value={value ?? ""} onChange={e => onChange(e.target.value || null)} aria-label={label}>
      <option value="">{all}</option>{list.map(o => <option key={o} value={o}>{o}</option>)}
    </select>
  </label>;
}

/**
 * Global filters (strategy/engine, instrument, version, date range). Held in the URL query + React context, so navigation
 * (which carries these keys) never resets them. Options come from the shared /strategies resource, not extra requests.
 */
export function GlobalFilterBar({ trailing }: { trailing?: ReactNode }) {
  const { filters, set, pathname } = useUrlState();
  const strategies = useResource(urls.strategies(), parse.strategies, STATIC_INTERVAL);
  const items = useMemo(() => strategies.data?.items ?? [], [strategies.data]);
  const engines = items.map(i => i.engine_id);
  const labels = new Map(items.map(i => [i.engine_id, i.label]));
  const instruments = [...new Set(items.map(i => i.instrument))];
  const versions = [...new Set(items.map(i => i.version))];
  const dated = isDatedPath(pathname);
  const range = filters.range ?? defaultRangeFor(pathname);
  return <div className="tm-filter-row" role="search" aria-label="Global filters">
    <label className="tm-field">Strategy
      <select className="tm-select" value={filters.engine ?? ""} aria-label="Strategy" onChange={e => set({ engine: e.target.value || null })}>
        <option value="">All strategies</option>
        {(filters.engine && !engines.includes(filters.engine) ? [filters.engine, ...engines] : engines).map(id => <option key={id} value={id}>{labels.get(id) ?? id}</option>)}
      </select>
    </label>
    <FilterSelect label="Instrument" value={filters.instrument} options={instruments} onChange={v => set({ instrument: v })}/>
    <FilterSelect label="Version" value={filters.version} options={versions} onChange={v => set({ version: v })}/>
    {dated && pathname !== "/analytics" ? <DateRangeSelector value={range} from={filters.from} to={filters.to} onChange={(r, from, to) => set({ range: r, from, to })}/> : pathname === "/analytics" ? <span className="tm-hitem tm-muted">Date range: set on the page</span> : <span className="tm-hitem tm-muted" title="This page shows the current state, not a date range">Date: not applicable</span>}
    <span className="tm-spacer"/>
    {(filters.engine || filters.instrument || filters.version || filters.range) &&
      <button type="button" className="tm-btn" onClick={() => set({ engine: null, instrument: null, version: null, range: null, from: null, to: null })}>Clear filters</button>}
    {trailing}
  </div>;
}

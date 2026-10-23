"use client";
import { ResponsiveTable } from "@/components/responsive-table";
import { useMemo, useState, type ReactNode } from "react";
import { Icon } from "@/components/icons";
import { Empty } from "@/components/ui";
export interface DataColumn<T> { key: string; label: string; render: (row: T) => ReactNode; value: (row: T) => string | number | null; }
export function DataTable<T>({ label, rows, columns, rowKey, searchText, empty = "No records available", note }: {
  label: string; rows: T[]; columns: DataColumn<T>[]; rowKey: (row: T) => string;
  searchText: (row: T) => string; empty?: string; note?: string;
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: string; ascending: boolean } | null>(null);
  const filtered = useMemo(() => {
    const items = rows.filter(row => searchText(row).toLowerCase().includes(query.trim().toLowerCase()));
    const col = columns.find(c => c.key === sort?.key);
    if (col && sort) items.sort((a,b) => {
      const x = col.value(a), y = col.value(b);
      if (x == null) return y == null ? 0 : 1;
      if (y == null) return -1;
      const cmp = typeof x === "number" && typeof y === "number" ? x-y : String(x).localeCompare(String(y));
      return sort.ascending ? cmp : -cmp;
    });
    return items;
  }, [rows, columns, searchText, query, sort]);
  return <><div className="table-toolbar"><label className="search-field"><Icon name="search" size={15}/><input aria-label={`Search ${label}`} placeholder="Filter records…" value={query} onChange={e => setQuery(e.target.value)}/></label><label className="mobile-table-sort">Sort<select aria-label={`Sort ${label}`} value={sort?.key??""} onChange={e=>setSort(e.target.value?{key:e.target.value,ascending:true}:null)}><option value="">Source order</option>{columns.map(c=><option key={c.key} value={c.key}>{c.label}</option>)}</select></label><span>{filtered.length} loaded records{note ? ` · ${note}` : ""}</span></div>
    {filtered.length ? <div className="table-wrap"><ResponsiveTable aria-label={label}><thead><tr>{columns.map(col => <th key={col.key} aria-sort={sort?.key === col.key ? sort.ascending ? "ascending" : "descending" : "none"}><button onClick={() => setSort({ key: col.key, ascending: sort?.key !== col.key || !sort.ascending })}>{col.label}<span aria-hidden="true">{sort?.key === col.key ? sort.ascending ? "↑" : "↓" : "↕"}</span></button></th>)}</tr></thead><tbody>{filtered.map(row => <tr key={rowKey(row)}>{columns.map(col => <td key={col.key}>{col.render(row)}</td>)}</tr>)}</tbody></ResponsiveTable></div> : <Empty>{query ? "No records match this filter" : empty}</Empty>}
  </>;
}

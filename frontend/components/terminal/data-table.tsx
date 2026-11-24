"use client";
import { useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { Icon } from "@/components/icons";

export type Column<T> = {
  key: string; header: string; render: (row: T) => ReactNode;
  align?: "right"; sortKey?: string; sticky?: boolean; wrap?: boolean; title?: string;
};
export type SortState = { key: string; desc: boolean };

const INTERACTIVE = "a,button,input,select,summary,label,[role=button]";

/**
 * Dense table: sticky header, sticky first column, tabular right-aligned numbers, horizontal scroll confined to its own
 * wrapper, optional row expansion (state survives data refreshes because it is keyed by row key) and optional totals row.
 * Sorting is delegated to the API: a header is sortable only when the caller supplies `onSort` and the column a `sortKey`.
 */
export function DataTable<T>({ label, rows, columns, rowKey, onRowClick, rowClass, expand, sort, onSort, footer, footerLabel = "Totals", selectedKey, maxHeight, empty }: {
  label: string; rows: T[]; columns: Array<Column<T>>; rowKey: (row: T) => string; onRowClick?: (row: T) => void;
  rowClass?: (row: T) => string | undefined; expand?: (row: T) => ReactNode | null; sort?: SortState | null;
  onSort?: (key: string, desc: boolean) => void; footer?: Record<string, ReactNode>; footerLabel?: string;
  selectedKey?: string | null; maxHeight?: string; empty?: ReactNode;
}) {
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const toggle = (key: string) => setOpen(prev => { const next = new Set(prev); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const click = (row: T) => (event: MouseEvent) => { if (onRowClick && !(event.target as HTMLElement).closest(INTERACTIVE)) onRowClick(row); };
  const keydown = (row: T) => (event: KeyboardEvent) => { if (onRowClick && event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onRowClick(row); } };
  if (!rows.length && empty) return <>{empty}</>;
  return <div className="tm-table-wrap" style={maxHeight ? { ["--tm-table-max" as string]: maxHeight } : undefined} tabIndex={0} role="region" aria-label={`${label} (scrollable)`}>
    <table className="tm-table" aria-label={label}>
      <thead><tr>
        {columns.map((c, i) => {
          const sortable = !!(onSort && c.sortKey);
          const active = sortable && sort?.key === c.sortKey;
          return <th key={c.key} scope="col" className={`${c.align === "right" ? "tm-num " : ""}${i === 0 ? "sticky-first" : ""}`} title={c.title} aria-sort={active ? (sort!.desc ? "descending" : "ascending") : sortable ? "none" : undefined}>
            {sortable ? <button type="button" onClick={() => onSort!(c.sortKey!, active ? !sort!.desc : true)} aria-label={`Sort by ${c.header}`}>{c.header}<span aria-hidden="true">{active ? (sort!.desc ? "↓" : "↑") : "↕"}</span></button> : c.header}
          </th>;
        })}
      </tr></thead>
      <tbody>
        {rows.map(row => {
          const key = rowKey(row);
          const detail = expand?.(row) ?? null;
          const isOpen = detail != null && open.has(key);
          return [
            <tr key={key} data-clickable={onRowClick ? "true" : undefined} data-row-key={key} className={`${rowClass?.(row) ?? ""}${selectedKey === key ? " is-selected" : ""}`} tabIndex={onRowClick ? 0 : undefined} onClick={click(row)} onKeyDown={keydown(row)}>
              {columns.map((c, i) => <td key={c.key} className={`${c.align === "right" ? "tm-num " : ""}${c.wrap ? "wrap " : ""}${i === 0 ? "sticky-first" : ""}`}>
                {i === 0 && expand ? <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  {detail != null ? <button type="button" className="tm-expander" aria-expanded={isOpen} aria-label={`${isOpen ? "Collapse" : "Expand"} legs`} onClick={() => toggle(key)}><Icon name="chevron" size={12}/></button> : <span style={{ width: 18 }}/>}
                  {c.render(row)}
                </span> : c.render(row)}
              </td>)}
            </tr>,
            isOpen ? <tr key={`${key}:detail`} className="tm-subrow"><td colSpan={columns.length}>{detail}</td></tr> : null,
          ];
        })}
      </tbody>
      {footer && <tfoot><tr>{columns.map((c, i) => <td key={c.key} className={`${c.align === "right" ? "tm-num " : ""}${i === 0 ? "sticky-first" : ""}`}>{i === 0 ? (footer[c.key] ?? footerLabel) : footer[c.key] ?? null}</td>)}</tr></tfoot>}
    </table>
  </div>;
}

export function Pager({ total, limit, offset, onOffset, noun = "rows" }: { total: number; limit: number; offset: number; onOffset: (offset: number) => void; noun?: string }) {
  if (total <= 0) return null;
  const from = Math.min(total, offset + 1), to = Math.min(total, offset + limit);
  return <nav className="tm-pager" aria-label={`${noun} pagination`}>
    <span>{from}–{to} of {total} {noun}</span>
    <button type="button" className="tm-btn" disabled={offset <= 0} onClick={() => onOffset(Math.max(0, offset - limit))}>Previous</button>
    <button type="button" className="tm-btn" disabled={offset + limit >= total} onClick={() => onOffset(offset + limit)}>Next</button>
  </nav>;
}

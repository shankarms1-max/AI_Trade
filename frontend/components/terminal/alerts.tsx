"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { StatusBadge } from "@/components/terminal/primitives";
import { sinceText } from "@/lib/terminal-format";
import { withGlobalFilters } from "@/lib/filters";
import type { Alert } from "@/types/portfolio";

const positionHref = (alert: Alert) => alert.position_id ? `/positions?tab=unresolved&position=${encodeURIComponent(alert.position_id)}` : null;

export function AlertList({ items }: { items: Alert[] }) {
  return <ul style={{ listStyle: "none", margin: 0, padding: 0 }} aria-label="Alerts">
    {items.map((a, i) => {
      const href = positionHref(a);
      return <li key={`${a.code}-${a.position_id ?? i}`} className="tm-alert-item" data-severity={a.severity}>
        <div className="tm-alert-meta">
          <StatusBadge value={a.severity} tone={a.severity === "CRITICAL" ? "crit" : a.severity === "WARNING" ? "warn" : "info"} dot/>
          <b className="tm-mono">{a.code}</b>
          {a.engine && <span>{a.engine}</span>}
          <span className="tm-muted">since {sinceText(a.since)}</span>
          {href && <Link href={href} className="tm-link">View position</Link>}
        </div>
        <div>{a.message}</div>
        {a.action && <div className="tm-muted">Action: {a.action}</div>}
      </li>;
    })}
  </ul>;
}

/** Prominent red banner: shown for every CRITICAL alert (expired-not-settled, past forced exit, unresolved exposure, engine error). */
export function AlertBanner({ items, label = "CRITICAL" }: { items: Alert[]; label?: string }) {
  const critical = items.filter(a => a.severity === "CRITICAL");
  if (!critical.length) return null;
  return <div className="tm-banner" data-tone="crit" role="alert" aria-label={`${critical.length} critical alerts`}>
    <b>{critical.length} {label}</b>
    <div style={{ flex: 1, minWidth: 0 }}><AlertList items={critical}/></div>
  </div>;
}

/** Header control: critical count + compact popover from /api/portfolio/alerts (code, engine, message, since, action). */
export function AlertsPopover({ critical, warning, items, search }: { critical: number; warning: number; items: Alert[]; search: string }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc); document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const tone = critical > 0 ? "crit" : warning > 0 ? "warn" : "pos";
  return <span className="tm-popover-anchor" ref={root}>
    <button type="button" className="tm-btn" aria-haspopup="dialog" aria-expanded={open} aria-controls="alerts-popover" onClick={() => setOpen(o => !o)} data-alerts={critical}>
      <span className="tm-badge" data-tone={tone}>{critical} critical</span>
      {warning > 0 && <span className="tm-badge" data-tone="warn">{warning} warning</span>}
    </button>
    {open && <div id="alerts-popover" className="tm-popover" role="dialog" aria-label="Alerts">
      {items.length ? <AlertList items={items}/> : <div className="tm-muted">No active alerts.</div>}
      <div className="tm-caption"><Link className="tm-link" href={withGlobalFilters("/positions?tab=unresolved", search)} onClick={() => setOpen(false)}>Open unresolved positions</Link></div>
    </div>}
  </span>;
}

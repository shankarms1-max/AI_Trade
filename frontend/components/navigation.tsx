"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon, type IconName } from "@/components/icons";
import { useUrlState } from "@/components/filters-context";
import { withGlobalFilters } from "@/lib/filters";

type Item = [href: string, label: string, icon: IconName];
/** Primary navigation, in product order. */
export const primary: Item[] = [
  ["/", "Overview", "grid"], ["/positions", "Positions", "wallet"], ["/orders", "Orders", "layers"], ["/tradebook", "Tradebook", "history"],
  ["/strategies", "Strategies", "chart"], ["/analytics", "Analytics", "pulse"], ["/risk", "Risk", "shield"], ["/research", "Research", "search"],
  ["/system", "System Health", "clock"], ["/settings", "Settings", "menu"],
];
/** Technical / legacy pages: kept reachable, collapsed under More / Legacy. */
export const legacy: Item[] = [
  ["/engines", "Engines", "layers"], ["/mcx-futures", "MCX Futures", "flame"], ["/ast-futures", "AST Futures", "pulse"],
  ["/commodity-acts", "Commodity (legacy)", "gas"], ["/nifty", "NIFTY ACTS monitor", "chart"], ["/phase15", "Phase 15 monitor", "pulse"],
  ["/nifty/research", "Market research", "chart"], ["/history", "Research history", "history"], ["/shadow", "Legacy shadow portfolio", "wallet"],
];
const every = [...primary, ...legacy].map(([href]) => href);

/** Exact match first; otherwise the longest section prefix, so deep links such as /strategies/nifty-acts_v3 keep their section active. */
export function activeHref(pathname: string): string | null {
  if (every.includes(pathname)) return pathname;
  return every.filter(href => href !== "/" && pathname.startsWith(`${href}/`)).sort((a, b) => b.length - a.length)[0] ?? null;
}
export const pageLabel = (pathname: string) => [...primary, ...legacy].find(([href]) => href === activeHref(pathname))?.[1] ?? "Detail";

export function Navigation() {
  const pathname = usePathname();
  const { search } = useUrlState();
  const active = activeHref(pathname);
  const [drawer, setDrawer] = useState(false);
  const legacyActive = legacy.some(([href]) => href === active);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!drawer) return;
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setDrawer(false); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawer]);
  const links = (items: Item[], onNavigate?: () => void) => items.map(([href, label, icon]) =>
    <Link prefetch={false} href={withGlobalFilters(href, search)} key={href} className={`tm-navlink${active === href ? " is-active" : ""}`}
      aria-current={pathname === href ? "page" : active === href ? "location" : undefined} onClick={onNavigate}><Icon name={icon} size={15}/><span>{label}</span></Link>);
  return <>
    <a className="skip-link" href="#workspace-content">Skip to content</a>
    <aside className="tm-sidebar" aria-label="Workspace navigation">
      <Link href="/" className="tm-brand"><Icon name="chart" size={20}/><span>AI Trade<small>Paper trading terminal</small></span></Link>
      <nav className="tm-nav" aria-label="Primary">{links(primary)}</nav>
      <details className="tm-more" open={legacyActive || undefined}>
        <summary>More / Legacy</summary>
        <nav className="tm-nav" aria-label="Legacy">{links(legacy)}</nav>
      </details>
      <p className="tm-sidebar-note">Read-only · NSE + MCX<br/>No broker execution controls</p>
    </aside>
    <div className="tm-mobilebar"><Icon name="chart" size={16}/><span className="tm-muted">AI Trade /</span><strong>{pageLabel(pathname)}</strong></div>
    <nav className="tm-bottomnav" aria-label="Mobile primary">
      {links(primary.slice(0, 4))}
      <button type="button" className={`tm-navlink${active && !primary.slice(0, 4).some(([h]) => h === active) ? " is-active" : ""}`} aria-label="More navigation" aria-haspopup="dialog" aria-expanded={drawer} onClick={() => setDrawer(true)}><Icon name="menu" size={16}/><span>More</span></button>
    </nav>
    {drawer && <div className="tm-drawer-nav">
      <div className="tm-backdrop" onClick={() => setDrawer(false)} aria-hidden="true"/>
      <div className="tm-panel" role="dialog" aria-modal="true" aria-label="Navigation">
        <button ref={close} type="button" className="tm-btn" aria-label="Close navigation" onClick={() => setDrawer(false)}><Icon name="close" size={14}/>Close</button>
        <nav className="tm-nav" aria-label="All pages" style={{ marginTop: 8 }}>{links(primary, () => setDrawer(false))}</nav>
        <p className="tm-muted" style={{ margin: "10px 8px 2px", fontSize: 10.5, textTransform: "uppercase", letterSpacing: ".08em" }}>More / Legacy</p>
        <nav className="tm-nav" aria-label="Legacy pages">{links(legacy, () => setDrawer(false))}</nav>
        <p className="tm-sidebar-note">Read-only · Asia / Kolkata</p>
      </div>
    </div>}
  </>;
}

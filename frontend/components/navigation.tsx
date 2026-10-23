"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/hooks/use-polling";
import { Icon, type IconName } from "@/components/icons";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui";
const groups: Array<{ label: string; items: Array<[string, string, IconName]> }> = [
  { label: "Workspace", items: [["/", "Overview", "grid"], ["/nifty", "NIFTY strategies", "chart"], ["/commodity-acts", "Commodity ACTS", "layers"]] },
  { label: "Instruments", items: [["/commodity-acts/CRUDEOILM", "Crude Oil Mini", "flame"], ["/commodity-acts/NATGASMINI", "Natural Gas Mini", "gas"]] },
  { label: "Research & operations", items: [["/phase15", "Strategy monitor", "pulse"], ["/history", "Research history", "history"], ["/shadow", "Shadow portfolio", "wallet"], ["/system", "System health", "shield"]] },
];
export function Navigation() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const loader = useCallback(() => api.health(), []);
  const { data, error } = usePolling(loader, 30000);
  const status = error ? "UNKNOWN" : data?.status ?? "UNKNOWN";
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  const current = groups.flatMap(g => g.items).find(([url]) => url === pathname)?.[1] ?? "Research details";
  return <>
    <a className="skip-link" href="#workspace-content">Skip to content</a>
    {open && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setOpen(false)}/>}
    <aside className={`workspace-sidebar ${open ? "is-open" : ""}`} id="workspace-navigation">
      <Link href="/" className="workspace-brand" onClick={() => setOpen(false)}><span className="workspace-logo"><Icon name="chart" size={23}/></span><span>AI Trade<small>Research intelligence</small></span></Link>
      <div className="workspace-label"><span className="status-dot"/> Private workspace <span className="workspace-label-icon"><Icon name="shield" size={13}/></span></div>
      <nav aria-label="Primary">{groups.map(group => <div className="nav-group" key={group.label}><p>{group.label}</p>{group.items.map(([href, label, icon]) => {
        const active = pathname === href || (href === "/nifty" && pathname?.startsWith("/nifty/"));
        return <Link key={href} href={href} className={`workspace-navlink ${active ? "is-active" : ""}`} aria-current={active ? "page" : undefined} onClick={() => setOpen(false)}><Icon name={icon} size={18}/><span>{label}</span>{active && <i/>}</Link>;
      })}</div>)}</nav>
      <div className="sidebar-bottom"><div className="readonly-icon"><Icon name="shield" size={18}/></div><strong>Observe. Understand.</strong><p>Read-only analytics.<br/>No broker execution controls.</p><span className="sidebar-version">NSE + MCX · Asia / Kolkata</span></div>
    </aside>
    <header className="workspace-topbar"><div className="workspace-breadcrumb"><button className="icon-button mobile-menu" aria-label="Toggle navigation" aria-expanded={open} aria-controls="workspace-navigation" onClick={() => setOpen(!open)}><Icon name={open ? "close" : "menu"}/></button><span className="breadcrumb-root">Workspace</span><Icon name="chevron" size={13}/><strong>{current}</strong></div>
      <div className="workspace-tools"><span className="timezone-label"><Icon name="clock" size={14}/> IST</span><Link href="/system" aria-label={`System health: ${status}`}><Badge value={status}/></Link><span className="tool-divider"/><ThemeToggle/><span className="workspace-avatar" title="Private research workspace">AI</span></div>
    </header>
  </>;
}

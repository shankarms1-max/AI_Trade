"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/hooks/use-polling";
import { Icon, type IconName } from "@/components/icons";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui";
const primary: Array<[string,string,IconName]> = [["/","Overview","grid"],["/positions","Positions","wallet"],["/nifty","Strategies","chart"],["/trades","Trades","history"]];
const secondary: Array<[string,string,IconName]> = [["/engines","Engines","layers"],["/mcx-futures","MCX Futures","flame"],["/ast-futures","AST Futures","pulse"],["/commodity-acts","Commodity ACTS","layers"],["/commodity-acts/CRUDEOILM","Crude Oil Mini","flame"],["/commodity-acts/NATGASMINI","Natural Gas Mini","gas"],["/nifty/research","Market research","chart"],["/history","Research history","history"],["/shadow","Legacy shadow portfolio","wallet"],["/system","System health","shield"],["/phase15","Phase 15 monitor","pulse"]];
/** Exact match first; otherwise the longest section prefix, so deep links such as /mcx-futures/NATGASMINI keep their section active. */
export function activeHref(pathname: string): string | null {
  const all = [...primary, ...secondary].map(([href]) => href);
  if (all.includes(pathname)) return pathname;
  return all.filter(href => href !== "/" && pathname.startsWith(`${href}/`)).sort((a, b) => b.length - a.length)[0] ?? null;
}
export function Navigation() {
  const pathname = usePathname();
  const [open,setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const loader = useCallback((signal: AbortSignal)=>api.health(signal),[]);
  const {data,error} = usePolling(loader,30000);
  const status = error ? "UNKNOWN" : data?.status ?? "UNKNOWN";
  const active = activeHref(pathname);
  const current = [...primary,...secondary].find(([url])=>url===active)?.[1] ?? "Research detail";
  useEffect(()=>{
    const node=dialog.current;
    if(open)node?.showModal();else if(node?.open)node.close();
    if(!open)return;
    const previous=document.body.style.overflow;document.body.style.overflow="hidden";
    return ()=>{document.body.style.overflow=previous;};
  },[open]);
  const links=(items: typeof primary)=>items.map(([href,label,icon])=><Link prefetch={false} href={href} key={href} className={`workspace-navlink ${active===href?"is-active":""}`} aria-current={pathname===href?"page":active===href?"location":undefined} onClick={()=>setOpen(false)}><Icon name={icon} size={18}/><span>{label}</span></Link>);
  return <>
    <a className="skip-link" href="#workspace-content">Skip to content</a>
    <aside className="workspace-sidebar" aria-label="Workspace navigation">
      <Link href="/" className="workspace-brand"><Icon name="chart" size={22}/><span>AI Trade<small>Paper monitoring</small></span></Link>
      <nav aria-label="Primary">{links(primary)}<p className="nav-group-label">Research & markets</p>{links(secondary)}</nav>
      <p className="sidebar-note">Read-only · NSE + MCX<br/>No broker execution controls</p>
    </aside>
    <header className="workspace-topbar"><div className="workspace-breadcrumb"><span className="breadcrumb-root">AI Trade</span><span aria-hidden="true">/</span><strong>{current}</strong></div>
      <div className="workspace-tools"><Link href="/system" className="health-link" aria-label={`System health: ${status}`}><Badge value={status}/></Link><ThemeToggle/></div>
    </header>
    <nav className="mobile-primary" aria-label="Mobile primary">{links(primary)}<button className={`workspace-navlink ${secondary.some(([url])=>url===active)?"is-active":""}`} aria-label="More navigation" aria-haspopup="dialog" aria-expanded={open} onClick={()=>setOpen(true)}><Icon name="menu" size={18}/><span>More</span></button></nav>
    <dialog ref={dialog} className="navigation-dialog" aria-labelledby="more-title" onCancel={()=>setOpen(false)} onClose={()=>setOpen(false)}><header><h2 id="more-title">More</h2><button className="icon-button" aria-label="Close navigation" onClick={()=>setOpen(false)}><Icon name="close"/></button></header><nav aria-label="More destinations">{links(secondary)}</nav><p className="muted">Read-only monitoring · Asia / Kolkata</p></dialog>
  </>;
}

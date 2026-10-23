"use client";
import Link from "next/link";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/hooks/use-polling";
export function Navigation() { const loader=useCallback(()=>api.health(),[]);const{data,error}=usePolling(loader,30000);const status=error?"UNKNOWN":data?.status??"UNKNOWN";return <nav className="topnav" aria-label="Primary"><Link href="/" className="brand"><span className="brand-mark">AI</span><span>AI TRADE</span></Link><div className="navlinks"><Link className="navlink" href="/">Overview</Link><Link className="navlink" href="/nifty">NIFTY</Link><Link className="navlink" href="/commodity-acts">COMMODITY</Link><Link className="navlink" href="/phase15">Phase 15</Link><Link className="navlink" href="/history">Research History</Link><Link className="navlink" href="/shadow">Shadow Trades</Link><Link className="navlink" href="/system">System Health</Link></div><Link href="/system" className={`system-indicator ${status}`}>SYSTEM: {status}</Link><span className="read-only">READ ONLY</span></nav>; }

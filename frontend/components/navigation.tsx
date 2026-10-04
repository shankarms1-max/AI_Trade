"use client";
import Link from "next/link";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/hooks/use-polling";
export function Navigation() { const loader=useCallback(()=>api.health(),[]);const{data,error}=usePolling(loader,30000);const status=error?"UNKNOWN":data?.status??"UNKNOWN";return <nav className="topnav" aria-label="Primary"><Link href="/" className="brand"><span className="brand-mark">N</span><span>NIFTY RESEARCH</span></Link><div className="navlinks"><Link className="navlink" href="/">Dashboard</Link><Link className="navlink" href="/history">Research History</Link><Link className="navlink" href="/shadow">Shadow Trades</Link><Link className="navlink" href="/system">System Health</Link></div><Link href="/system" className={`system-indicator ${status}`}>SYSTEM: {status}</Link><span className="read-only">READ ONLY</span></nav>; }

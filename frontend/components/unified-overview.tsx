"use client";
import { useState } from "react";
import dynamic from "next/dynamic";
import { TradingWorkspace } from "@/components/trading-workspace";
const CrossMarketOverview=dynamic(()=>import("@/components/cross-market-overview").then(module=>module.CrossMarketOverview),{loading:()=> <p className="muted">Loading cross-market evidence…</p>});
export function UnifiedOverview(){const[open,setOpen]=useState(false);return <><TradingWorkspace/><details className="research-disclosure" onToggle={e=>setOpen(e.currentTarget.open)}><summary>Cross-market portfolio · NIFTY + Commodity</summary>{open&&<CrossMarketOverview/>}</details></>;}

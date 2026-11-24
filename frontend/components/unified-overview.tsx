"use client";
import { useState } from "react";
import dynamic from "next/dynamic";
import { TradingWorkspace } from "@/components/trading-workspace";
import { EnginesOverview } from "@/components/engines";
import { SectionBoundary } from "@/components/section-boundary";
const CrossMarketOverview=dynamic(()=>import("@/components/cross-market-overview").then(module=>module.CrossMarketOverview),{loading:()=> <p className="muted">Loading cross-market evidence…</p>});
export function UnifiedOverview() {
  const [open, setOpen] = useState(false);
  return <>
    <TradingWorkspace title="Trading overview" engineOverview={
      <SectionBoundary label="Engines overview"><EnginesOverview/></SectionBoundary>
    }/>
    <details className="research-disclosure" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Cross-market portfolio · NIFTY + Commodity</summary>
      {open && <CrossMarketOverview/>}
    </details>
  </>;
}

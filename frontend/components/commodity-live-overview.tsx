"use client";
import { useCallback, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { usePolling } from "@/hooks/use-polling";
import { McxFuturesOverview } from "@/components/mcx-futures";
import { CommodityOverview, CommodityJournal } from "@/components/commodity-acts";
import { CommodityPaper } from "@/components/commodity-paper";
import { SectionBoundary } from "@/components/section-boundary";

export function CommodityLiveOverview() {
  const [expanded, setExpanded] = useState(false);
  const loader = useCallback(async () => {
    const [research, paper] = await Promise.allSettled([api.commodityActsSummary(), api.commodityPaperSummary()]);
    return { research: research.status === "fulfilled" ? research.value : null,
      paper: paper.status === "fulfilled" ? paper.value : null };
  }, []);
  const { data } = usePolling(loader, 30000);
  const observed = data?.research?.instruments.some(row => row.observation_count > 0) ?? false;
  const exposure = data?.paper?.instruments?.some(row => (row.open_positions ?? 0) > 0 || (row.unresolved_exposure ?? 0) > 0) ?? false;
  const records = data?.paper?.instruments?.reduce((total, row) => total + (row.closed_trades ?? 0), 0) ?? 0;
  const showLegacy = expanded || exposure;
  return <>
    <h2 className="commodity-live-title">ACTS V3 MCX Futures — PAPER</h2>
    <McxFuturesOverview/>
    <details className="research-disclosure legacy-commodity" open={showLegacy} onToggle={event => setExpanded(event.currentTarget.open)}>
      <summary>Commodity ACTS Options (legacy) — NOT RUNNING</summary>
      <p>{data?.research && data.paper ? `No worker deployed · ${observed ? "historical observations available" : "no observations"} · historical records: ${records}` : "Legacy status unavailable"}</p>
      {showLegacy && <div className="legacy-commodity-panels">
        <SectionBoundary label="Legacy commodity paper"><CommodityPaper/></SectionBoundary>
        <SectionBoundary label="Legacy commodity research"><CommodityOverview/></SectionBoundary>
        <SectionBoundary label="Legacy commodity journal"><CommodityJournal/></SectionBoundary>
      </div>}
    </details>
    <p className="legacy-commodity-link"><Link href="/commodity-acts/CRUDEOILM">Full legacy options panels</Link></p>
  </>;
}

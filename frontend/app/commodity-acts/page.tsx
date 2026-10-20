import { CommodityJournal, CommodityOverview } from "@/components/commodity-acts";
import { SectionBoundary } from "@/components/section-boundary";

export default function CommodityActsPage() {
  return <main className="commodity-research"><div className="page-head"><div>
    <h1>Commodity ACTS Research</h1><p>RESEARCH / PAPER ONLY · NO BROKER ORDERS · NO P&amp;L YET</p>
  </div></div>
    <p className="execution-note">Stage: SCORE_TRANSFERABILITY. ACTS directional/scoring research only. Entry qualification is disabled until commodity execution geometry is validated.</p>
    <p className="execution-note">CRUDEOILM and NATGASMINI only. The selected future is the price reference; no cash-spot or India VIX proxy. Spread execution is intentionally disabled.</p>
    <SectionBoundary label="Commodity overview"><CommodityOverview/></SectionBoundary>
    <SectionBoundary label="Commodity journal"><CommodityJournal/></SectionBoundary>
  </main>;
}

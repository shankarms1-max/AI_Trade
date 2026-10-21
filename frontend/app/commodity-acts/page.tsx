import { CommodityJournal, CommodityOverview } from "@/components/commodity-acts";
import { SectionBoundary } from "@/components/section-boundary";
import { CommodityPaper } from "@/components/commodity-paper";

export default function CommodityActsPage() {
  return <main className="commodity-research"><div className="page-head"><div>
    <h1>Commodity ACTS Research and Paper</h1><p>RESEARCH / PAPER ONLY · NO BROKER ORDERS</p>
  </div></div>
    <p className="execution-note">V1 research context - Stage: SCORE_TRANSFERABILITY. ACTS directional/scoring research only. Entry qualification is disabled until commodity execution geometry is validated.</p>
    <p className="execution-note">CRUDEOILM and NATGASMINI only. The selected future is the price reference; no cash-spot or India VIX proxy. V1 remains score-only. V2 paper activation and its verified execution evidence are shown separately below.</p>
    <SectionBoundary label="Commodity overview"><CommodityOverview/></SectionBoundary>
    <SectionBoundary label="Commodity automated paper"><CommodityPaper/></SectionBoundary>
    <SectionBoundary label="Commodity journal"><CommodityJournal/></SectionBoundary>
  </main>;
}

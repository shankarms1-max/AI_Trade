import { CommodityJournal, CommodityOverview } from "@/components/commodity-acts";
import { SectionBoundary } from "@/components/section-boundary";
import { CommodityPaper } from "@/components/commodity-paper";

export default function CommodityActsPage() {
  return <main className="commodity-research"><div className="page-head"><div>
    <h1>Commodity ACTS Research and Paper</h1><p>RESEARCH / PAPER ONLY · NO BROKER ORDERS</p>
  </div></div>
    <p className="execution-note">V1 research context - Stage: SCORE_TRANSFERABILITY. ACTS directional/scoring research only. This V1 score-only panel does not activate single-short paper entries.</p>
    <p className="execution-note">CRUDEOILM and NATGASMINI only. The selected future is the price reference; no cash-spot or India VIX proxy. V1 remains score-only. Single-short paper activation and its dated execution evidence are shown separately below. New credit-spread entries are disabled; historical positions retain their original records.</p>
    <SectionBoundary label="Commodity overview"><CommodityOverview/></SectionBoundary>
    <SectionBoundary label="Commodity automated paper"><CommodityPaper/></SectionBoundary>
    <SectionBoundary label="Commodity journal"><CommodityJournal/></SectionBoundary>
  </main>;
}

import { CommodityJournal, CommodityOverview } from "@/components/commodity-acts";
import { SectionBoundary } from "@/components/section-boundary";
import { CommodityPaper } from "@/components/commodity-paper";
import { PageHeading } from "@/components/ui";

export default function CommodityActsPage() {
  return <main className="commodity-research"><PageHeading eyebrow="MCX · Two independent instruments" title="Commodity ACTS" description="Crude Oil Mini & Natural Gas Mini. Directional intelligence, observed-book execution and paper lifecycle research."/>
    <p className="command-banner">RESEARCH / PAPER ONLY · NO BROKER ORDERS</p>
    <p className="execution-note">Stage: DIRECTIONAL_CONTEXT_RESEARCH. ACTS direction is separate from Commodity single-option execution qualification. The context panel never places an entry.</p>
    <p className="execution-note">CRUDEOILM and NATGASMINI only. The selected future is the price reference; no cash-spot or India VIX proxy. Context remains observational. Single-short paper activation and its dated execution evidence are shown separately below. New credit-spread entries are disabled; historical positions retain their original records.</p>
    <SectionBoundary label="Commodity automated paper"><CommodityPaper/></SectionBoundary>
    <SectionBoundary label="Commodity overview"><CommodityOverview/></SectionBoundary>
    <SectionBoundary label="Commodity journal"><CommodityJournal/></SectionBoundary>
  </main>;
}

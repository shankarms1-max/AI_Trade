import { CommodityLiveOverview } from "@/components/commodity-live-overview";
import { SectionBoundary } from "@/components/section-boundary";
import { PageHeading } from "@/components/ui";

export default function CommodityActsPage() {
  return <main className="commodity-research mcx-futures">
    <PageHeading eyebrow="MCX · PAPER" title="Commodity (MCX)" description="ACTS V3 futures paper monitoring for Crude Oil Mini and Natural Gas Mini."/>
    <SectionBoundary label="Commodity overview"><CommodityLiveOverview/></SectionBoundary>
  </main>;
}

import { notFound } from "next/navigation";
import { CommodityOverview } from "@/components/commodity-acts";
import { CommodityPaper } from "@/components/commodity-paper";
import { SectionBoundary } from "@/components/section-boundary";

export default async function CommodityInstrumentPage({ params }: { params: Promise<{ instrument: string }> }) {
  const { instrument } = await params;
  if (instrument !== "CRUDEOILM" && instrument !== "NATGASMINI") notFound();
  return <><h1 className="phase15-overview-title">{instrument} · ACTS V2 PAPER</h1>
    <SectionBoundary label="Commodity paper lifecycle"><CommodityPaper initialInstrument={instrument}/></SectionBoundary>
    <SectionBoundary label="Commodity research evidence"><CommodityOverview instrument={instrument}/></SectionBoundary></>;
}

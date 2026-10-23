import { notFound } from "next/navigation";
import { CommodityOverview } from "@/components/commodity-acts";
import { CommodityPaper } from "@/components/commodity-paper";
import { SectionBoundary } from "@/components/section-boundary";
import { PageHeading } from "@/components/ui";

export default async function CommodityInstrumentPage({ params }: { params: Promise<{ instrument: string }> }) {
  const { instrument } = await params;
  if (instrument !== "CRUDEOILM" && instrument !== "NATGASMINI") notFound();
  return <><PageHeading eyebrow="MCX · ACTS V2 paper research" title={instrument === "CRUDEOILM" ? "Crude Oil Mini" : "Natural Gas Mini"} description={`${instrument} · Single-leg short options. Exact contracts, observed quotes and auditable execution evidence.`}/>
    <SectionBoundary label="Commodity paper lifecycle"><CommodityPaper initialInstrument={instrument}/></SectionBoundary>
    <SectionBoundary label="Commodity research evidence"><CommodityOverview instrument={instrument}/></SectionBoundary></>;
}

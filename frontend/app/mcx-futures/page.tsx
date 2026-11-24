import { McxFuturesOverview } from "@/components/mcx-futures";
import { SectionBoundary } from "@/components/section-boundary";
import { PageHeading } from "@/components/ui";

export default function McxFuturesPage() {
  return <main className="mcx-futures ast-futures">
    <PageHeading eyebrow="MCX · Futures paper engine" title="MCX Futures"
      description="Crude Oil Mini and Natural Gas Mini futures paper engine: contract, session, feed freshness, decisions, positions and risk. Times in Asia/Kolkata."/>
    <SectionBoundary label="MCX futures status"><McxFuturesOverview/></SectionBoundary>
  </main>;
}

import { EnginesTable } from "@/components/engines";
import { SectionBoundary } from "@/components/section-boundary";
import { PageHeading } from "@/components/ui";

export default function EnginesPage() {
  return <main className="engines-page">
    <PageHeading eyebrow="All engines · read-only" title="Engines"
      description="State, mode, exposure and latest decision for every ACTS, MCX futures, AST, parallel and research engine. Times in Asia/Kolkata."/>
    <p className="command-banner"><span>Read-only monitoring · No broker execution controls · No start, stop or kill actions</span></p>
    <SectionBoundary label="Engine status"><EnginesTable/></SectionBoundary>
  </main>;
}

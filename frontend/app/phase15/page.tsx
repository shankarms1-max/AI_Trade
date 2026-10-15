import { Phase15Monitor } from "@/components/phase15-monitor";
import { SectionBoundary } from "@/components/section-boundary";
import { Phase15Journal, Phase15Positions } from "@/components/phase15-paper-trades";

export default function Phase15Page() {
  return <main>
    <div className="page-head"><div><h1>Phase 15 Strategy Monitor</h1><p>Read-only strategy status · Asia/Kolkata</p></div></div>
    <h2 className="phase15-overview-title">OVERVIEW</h2>
    <SectionBoundary label="Phase 15 monitor"><Phase15Monitor/></SectionBoundary>
    <SectionBoundary label="Open paper positions"><Phase15Positions/></SectionBoundary>
    <SectionBoundary label="Paper trade journal"><Phase15Journal/></SectionBoundary>
  </main>;
}

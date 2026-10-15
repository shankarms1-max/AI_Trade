import { Phase15Monitor } from "@/components/phase15-monitor";
import { SectionBoundary } from "@/components/section-boundary";

export default function Phase15Page() {
  return <main>
    <div className="page-head"><div><h1>Phase 15 Strategy Monitor</h1><p>Read-only strategy status · Asia/Kolkata</p></div></div>
    <SectionBoundary label="Phase 15 monitor"><Phase15Monitor/></SectionBoundary>
  </main>;
}

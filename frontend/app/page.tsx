import { Dashboard } from "@/components/dashboard";
import { Phase15Monitor } from "@/components/phase15-monitor";
import { SectionBoundary } from "@/components/section-boundary";
import { UnifiedOverview } from "@/components/unified-overview";

export default function Page() {
  return <>
    <SectionBoundary label="Unified overview"><UnifiedOverview/></SectionBoundary>
    <SectionBoundary label="Phase 15 monitor"><Phase15Monitor/></SectionBoundary>
    <SectionBoundary label="Research dashboard"><Dashboard/></SectionBoundary>
  </>;
}

import { Dashboard } from "@/components/dashboard";
import { Phase15Monitor } from "@/components/phase15-monitor";
import { SectionBoundary } from "@/components/section-boundary";

export default function Page() {
  return <>
    <SectionBoundary label="Phase 15 monitor"><Phase15Monitor/></SectionBoundary>
    <SectionBoundary label="Research dashboard"><Dashboard/></SectionBoundary>
  </>;
}

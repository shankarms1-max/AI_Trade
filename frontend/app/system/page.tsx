import { SystemHealth } from "@/components/system-health";
import { SystemSections } from "@/components/pages/system-sections";
import { SectionBoundary } from "@/components/section-boundary";

/** Terminal sections (sources, engine feeds, charge model) on top; the existing operations console is kept below unchanged. */
export default function Page() {
  return <>
    <SectionBoundary label="System sections"><SystemSections/></SectionBoundary>
    <SystemHealth/>
  </>;
}

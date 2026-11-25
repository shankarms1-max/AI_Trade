import { Positions } from "@/components/pages/positions";
import { SectionBoundary } from "@/components/section-boundary";

/** Replaces the legacy Phase 15 positions view (its components remain for /phase15). */
export default function PositionsPage() { return <SectionBoundary label="Positions"><Positions/></SectionBoundary>; }

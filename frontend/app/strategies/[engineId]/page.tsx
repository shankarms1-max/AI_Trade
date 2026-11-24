import { StrategyDetail } from "@/components/pages/strategies";
import { SectionBoundary } from "@/components/section-boundary";

/** An engine id the API does not know answers 404 and the client renders app/not-found.tsx. */
export default async function StrategyDetailPage({ params }: { params: Promise<{ engineId: string }> }) {
  const { engineId } = await params;
  return <SectionBoundary label="Strategy"><StrategyDetail engineId={decodeURIComponent(engineId)}/></SectionBoundary>;
}

import { Dashboard } from "@/components/dashboard";
import { Phase15Monitor } from "@/components/phase15-monitor";
import { Phase15Positions, Phase15Journal } from "@/components/phase15-paper-trades";
import { SectionBoundary } from "@/components/section-boundary";
import { PageHeading } from "@/components/ui";

export default function NiftyPage() {
  return <><PageHeading eyebrow="NSE · Independent strategy research" title="NIFTY strategies" description="ACTS V2, ORB 15 / 30 / 60, EMA Pullback and Supertrend / RSI / Volume. Observed-book paper execution only."/>
    <SectionBoundary label="NIFTY strategy monitor"><Phase15Monitor workspace/></SectionBoundary>
    <SectionBoundary label="NIFTY paper positions"><Phase15Positions/><Phase15Journal/></SectionBoundary>
    <SectionBoundary label="NIFTY research"><details className="research-disclosure"><summary>Market intelligence · Alpha readiness, structure & research diagnostics</summary><Dashboard/></details></SectionBoundary></>;
}

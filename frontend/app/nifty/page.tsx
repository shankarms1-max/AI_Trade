import { Dashboard } from "@/components/dashboard";
import { Phase15Monitor } from "@/components/phase15-monitor";
import { Phase15Positions, Phase15Journal } from "@/components/phase15-paper-trades";
import { SectionBoundary } from "@/components/section-boundary";

export default function NiftyPage() {
  return <><h1 className="phase15-overview-title">NIFTY · ACTS · ORB 15 / 30 / 60 · EMA PULLBACK · SUPERTREND / RSI / VOLUME</h1>
    <SectionBoundary label="NIFTY strategy monitor"><Phase15Monitor/></SectionBoundary>
    <SectionBoundary label="NIFTY paper positions"><Phase15Positions/><Phase15Journal/></SectionBoundary>
    <SectionBoundary label="NIFTY research"><Dashboard/></SectionBoundary></>;
}

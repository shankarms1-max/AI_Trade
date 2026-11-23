import { AstFuturesMonitor, AstPaperBanner } from "@/components/ast-futures";
import { PageHeading } from "@/components/ui";

export default function AstFuturesPage() {
  return <main className="ast-futures">
    <PageHeading eyebrow="NIFTY FUTURES · AST 22 / 3.9" title="AST Futures"
      description="Direct NIFTY futures paper execution monitor using the frozen 22 / 3.9 SuperTrend policy and the 09:15–09:30 opening range."/>
    <AstPaperBanner/>
    <AstFuturesMonitor/>
  </main>;
}

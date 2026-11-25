"use client";
import Link from "next/link";
import { StatusBadge, PageTitle, type Tone } from "@/components/terminal/primitives";
import { ANALYTICS_INTERVAL, STATIC_INTERVAL, useResource } from "@/hooks/use-resource";
import { parse, urls } from "@/lib/portfolio-api";
import { istDateTime, int } from "@/lib/terminal-format";

type EvidenceClass = { id: string; name: string; status: string; tone: Tone; body: string; links?: Array<{ href: string; label: string }> };

/**
 * The evidence classes are kept apart on purpose: nothing here is summed, charted together or compared. Classes that have no
 * data in this deployment say so (INSUFFICIENT_DATA / NONE); no number is fabricated.
 */
export function Research() {
  const strategies = useResource(urls.strategies(), parse.strategies, STATIC_INTERVAL);
  const paper = useResource(urls.performance({ mode: "PAPER" }), parse.performance, ANALYTICS_INTERVAL);
  const mcx = (strategies.data?.items ?? []).filter(c => c.family === "MCX_FUTURES");
  const forward = paper.data?.overall;
  const classes: EvidenceClass[] = [
    { id: "historical-simulation", name: "Historical simulation", status: "SEPARATE PAGES", tone: "info", body: "Offline studies of historical data. Not paper-trading evidence: no figure from them appears on this page.",
      links: [{ href: "/nifty/research", label: "Market research" }, { href: "/history", label: "Research history" }, { href: "/shadow", label: "Legacy shadow portfolio" }] },
    { id: "synthetic", name: "Synthetic integration tests", status: "NOT PERFORMANCE EVIDENCE", tone: "na", body: "Synthetic fixtures prove the software contract (for example the P&L read model). Their numbers are never presented as results." },
    { id: "backtest", name: "Genuine historical backtest", status: "INSUFFICIENT DATA", tone: "warn", body: "No MCX historical data has been collected, so no genuine backtest of the MCX futures engines exists. None is shown rather than invented." },
    { id: "oos", name: "Out-of-sample validation", status: "INSUFFICIENT DATA", tone: "warn", body: "Walk-forward and out-of-sample validation need that history first. Not available for the MCX engines." },
    { id: "forward", name: "Forward paper", status: forward ? "ACCUMULATING" : "NO TRADES", tone: forward ? "pos" : "na",
      body: forward ? `${int(forward.trades)} closed paper trades from ${istDateTime(forward.first_exit)} to ${istDateTime(forward.last_exit)}. Simulated fills against observed bid/ask, charges applied by the backend; the figures live in the Tradebook and Analytics, not here.` : "No closed paper trades recorded yet.",
      links: [{ href: "/tradebook?mode=PAPER", label: "Tradebook (PAPER)" }, { href: "/analytics", label: "Analytics" }] },
    { id: "live", name: "Live execution", status: "NONE", tone: "na", body: "No live execution path exists in this deployment. There are no live trades and no live P&L." },
  ];
  return <div>
    <PageTitle title="Research" note="Evidence classes are kept separate. They are never blended into one chart or number."/>
    <div className="tm-evidence">
      {classes.map(c => <article key={c.id} aria-label={c.name}>
        <h3>{c.name}<StatusBadge value={c.status} tone={c.tone}/></h3>
        <p>{c.body}</p>
        {c.links && <p>{c.links.map((l, i) => <span key={l.href}>{i > 0 ? " · " : ""}<Link className="tm-link" href={l.href}>{l.label}</Link></span>)}</p>}
      </article>)}
    </div>
    <h2 className="tm-caption" style={{ marginTop: 14 }}>MCX engine calibration (deployed configuration)</h2>
    <div className="tm-evidence">
      {mcx.map(c => <article key={c.engine_id} aria-label={`${c.label} calibration`}>
        <h3>{c.label}<StatusBadge value={c.dimensions.calibration ?? "UNKNOWN"} tone="warn"/></h3>
        <p>Parameters are a pre-registered design choice under test. They have not been validated on historical data, and no result is claimed for them.</p>
        <p><Link className="tm-link" href={`/strategies/${c.engine_id}?view=configuration`}>Effective configuration</Link></p>
      </article>)}
      {mcx.length === 0 && <article><p className="tm-muted">No MCX engine reported.</p></article>}
    </div>
  </div>;
}

"use client";
import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { useUrlState } from "@/components/filters-context";
import { ThemeToggle } from "@/components/theme-toggle";
import { AlertsPopover } from "@/components/terminal/alerts";
import { GlobalFilterBar } from "@/components/terminal/filters";
import { StatusBadge, type Tone } from "@/components/terminal/primitives";
import { pageLabel } from "@/components/navigation";
import { ANALYTICS_INTERVAL, LIVE_INTERVAL, useResource } from "@/hooks/use-resource";
import { parse, urls } from "@/lib/portfolio-api";
import { istClock, title } from "@/lib/terminal-format";
import type { Header } from "@/types/portfolio";

const CONNECTIVITY: Record<string, { text: string; tone: Tone }> = {
  CONNECTED_INFERRED: { text: "Connected", tone: "pos" },
  DISCONNECTED_INFERRED: { text: "Disconnected", tone: "neg" },
  NOT_APPLICABLE_MARKETS_CLOSED: { text: "N/A · markets closed", tone: "na" },
  UNKNOWN: { text: "Unknown", tone: "na" },
};

function Clock() {
  const [now, setNow] = useState<string | null>(null);
  useEffect(() => {
    const tick = () => setNow(istClock(new Date().toISOString()));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return <span className="tm-clock" role="timer" aria-label="Current time, Asia/Kolkata" suppressHydrationWarning>{now ?? "--:--:--"}<small>IST</small></span>;
}

function Freshness({ header }: { header: Header }) {
  const f = header.data_freshness;
  const stale = f.stale_feeds > 0;
  return <span className="tm-hitem">
    <StatusBadge value={`${f.fresh_feeds} fresh · ${f.stale_feeds} stale`} tone={stale ? "warn" : f.fresh_feeds > 0 ? "pos" : "na"} label="Feeds"
      title={stale ? `Stale feeds: ${f.stale.join(", ")}` : `${f.fresh_feeds} of ${f.feeds} feeds fresh`}/>
    {stale && <span className="tm-muted">stale: {f.stale.join(", ")}</span>}
  </span>;
}

export function GlobalHeader() {
  const pathname = usePathname();
  const { filters, set, search } = useUrlState();
  const header = useResource(urls.header(), parse.header, LIVE_INTERVAL);
  const alerts = useResource(urls.alerts(), parse.alerts, LIVE_INTERVAL);
  useResource(urls.sources(), parse.sources, ANALYTICS_INTERVAL * 5); // shared with System Health / Settings: one request
  const h = header.data;
  const conn = h ? CONNECTIVITY[h.broker_connectivity.state] ?? { text: title(h.broker_connectivity.state), tone: "na" as Tone } : null;
  const critical = alerts.data?.critical ?? h?.critical_alerts ?? 0;
  const warning = alerts.data?.warning ?? h?.warning_alerts ?? 0;
  return <header className="tm-header" aria-label="Terminal status">
    <div className="tm-status-row">
      <strong style={{ fontSize: 13 }} aria-label="Current page">{pageLabel(pathname)}</strong>
      <Clock/>
      {h ? <>
        <span className="tm-hitem">
          <StatusBadge dimension="market" value={h.nse.status} label="NSE"/>
          <StatusBadge dimension="market" value={h.mcx.status} label="MCX" title={h.mcx.window ? `MCX window ${h.mcx.window}` : undefined}/>
        </span>
        <span className="tm-hitem">
          <StatusBadge value={conn!.text} tone={conn!.tone} label="Broker (inferred)" title={`${conn!.text}. Inferred: ${h.broker_connectivity.basis}`}/>
        </span>
        <Freshness header={h}/>
      </> : <span className="tm-hitem">{header.failure ? <StatusBadge value="DISCONNECTED" tone="warn" label="API"/> : <span className="tm-muted">Loading status…</span>}</span>}
      {header.staleSince != null && <span className="tm-hitem" role="status"><StatusBadge value="STALE" tone="warn" label="Header"/><span className="tm-muted">since {istClock(new Date(header.staleSince).toISOString())} IST</span></span>}
      <span className="tm-spacer"/>
      <AlertsPopover critical={critical} warning={warning} items={alerts.data?.items ?? []} search={search}/>
      <label className="tm-field">Mode
        <select className="tm-select" aria-label="Mode" value={filters.mode} onChange={e => set({ mode: e.target.value === "LIVE" ? "LIVE" : null })}>
          <option value="PAPER">PAPER</option><option value="LIVE">LIVE</option>
        </select>
      </label>
      {filters.mode === "LIVE" && <StatusBadge value="NO LIVE ENGINES" tone="warn" title="No live execution path exists; LIVE totals are never mixed with PAPER"/>}
      <ThemeToggle/>
    </div>
    <GlobalFilterBar/>
  </header>;
}

"use client";
import Link from "next/link";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { completeSum, observationState } from "@/lib/monitoring";
import { dateIST, integer, money, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Metric, PageHeading, Panel } from "@/components/ui";
import { Icon } from "@/components/icons";
import { PortfolioEvidence } from "@/components/portfolio-evidence";

export function UnifiedOverview() {
  const loader = useCallback(async () => {
    const [nifty, commodity, health] = await Promise.allSettled([api.phase15Summary(), api.commodityPaperSummary(), api.health()]);
    return { nifty: nifty.status === "fulfilled" ? nifty.value : null,
      commodity: commodity.status === "fulfilled" ? commodity.value : null,
      health: health.status === "fulfilled" ? health.value : null };
  }, []);
  const { data, error } = usePolling(loader, 15000);
  const nifty = data?.nifty;
  const commodities = data?.commodity?.instruments;
  const completeCommodity = commodities?.length === 2 && ["CRUDEOILM", "NATGASMINI"].every(id => commodities.some(r => r.instrument_id === id)) ? commodities : null;
  const total = (n: number | null | undefined, field: "open_positions" | "trades_today" | "realized_gross_today" | "unrealized_gross") =>
    completeCommodity && nifty ? completeSum([n, ...completeCommodity.map(row => field === "unrealized_gross" && row.open_positions !== 0 && row.observation_freshness !== "FRESH" ? null : row[field])]) : null;
  return <>
    <PageHeading eyebrow="Unified intelligence · NSE + MCX" title="Trading overview" description="A clearer view of your strategies, positions and paper performance." action={<><span className="button-secondary"><Icon name="clock" size={14}/>{data?.health?.current_time_ist ? dateIST(data.health.current_time_ist) : "Asia / Kolkata"}</span><Link className="button-primary" href="/nifty">Explore strategies<Icon name="arrow" size={14}/></Link></>}/>
    {error && <p role="alert">MONITORING UNAVAILABLE</p>}
    <div className="command-kpis">
      <Metric label="Today's realized gross" value={money(total(nifty?.totals.realized_gross_pnl_today, "realized_gross_today"))} detail="Combined NIFTY + Commodity · INR"/>
      <Metric label="Unrealized gross" value={money(total(nifty?.totals.unrealized_gross_pnl, "unrealized_gross"))} detail="Fresh, observed marks only"/>
      <Metric label="Open paper positions" value={integer(total(nifty?.totals.open_positions, "open_positions"))} detail="Across both independent servers"/>
      <Metric label="Today's simulated entries" value={integer(total(nifty?.totals.trades_today, "trades_today"))} detail="Paper fills · not broker orders"/>
    </div>
    <p className="command-banner"><Icon name="shield" size={15}/><span>Independent databases. Read-only monitoring. Combined net P&amp;L and capital efficiency remain unavailable without verified costs and margin.</span></p>
    <div className="section-title"><div><h2>Markets at a glance</h2><p>Performance, signal state and evidence freshness</p></div><Link href="/system">System health<Icon name="arrow" size={13}/></Link></div>
    <div className="source-grid">
      <article className="source-card" aria-label="NIFTY monitoring status"><header><span className="instrument-icon"><Icon name="chart"/></span><div><h3><Link href="/nifty">NIFTY</Link></h3><small>NSE · Six paper strategies</small></div><Link href="/nifty" aria-label="Open NIFTY workspace"><Icon name="arrow" size={16}/></Link></header>
        <div className="source-status"><Badge value={nifty ? "AVAILABLE" : "UNAVAILABLE"}/><Badge value={nifty?.mode ?? "UNAVAILABLE"}/></div>
        <div className="source-stats"><div><small>Realized gross today</small><strong>{money(nifty?.totals.realized_gross_pnl_today)}</strong></div><div><small>Open positions</small><strong>{integer(nifty?.totals.open_positions)}</strong></div></div>
        <div className="source-detail"><p>Paper strategies: {integer(nifty?.strategies?.length)} · Open: {integer(nifty?.totals.open_positions)} · Entries today: {integer(nifty?.totals.trades_today)}</p><span>Broker/data status: {data?.health?.components?.market_data ?? "UNAVAILABLE"}</span><span>Worker process state: UNAVAILABLE. Never inferred from activity.</span></div>
        <Link href="/nifty" className="source-link">ACTS, ORB, EMA & Supertrend<Icon name="arrow" size={13}/></Link>
      </article>
      {(["CRUDEOILM", "NATGASMINI"] as const).map(instrument => {
        const row = commodities?.find(item => item.instrument_id === instrument);
        return <article className="source-card" key={instrument} aria-label={`${instrument} monitoring status`}><header><span className={`instrument-icon ${instrument === "CRUDEOILM" ? "crude" : "gas"}`}><Icon name={instrument === "CRUDEOILM" ? "flame" : "gas"}/></span><div><h3><Link href={`/commodity-acts/${instrument}`}>{instrument}</Link></h3><small>MCX · ACTS V2 single short</small></div><Link href={`/commodity-acts/${instrument}`} aria-label={`Open ${instrument} workspace`}><Icon name="arrow" size={16}/></Link></header>
          <div className="source-status"><Badge value={row?.observation_freshness ?? "UNAVAILABLE"}/><Badge value={row?.market_open == null ? "SESSION_UNAVAILABLE" : row.market_open ? "MARKET_OPEN" : "MARKET_CLOSED"}/></div>
          <div className="source-stats"><div><small>Realized gross today</small><strong>{money(row?.realized_gross_today)}</strong></div><div><small>Open positions</small><strong>{integer(row?.open_positions)}</strong></div></div>
          <div className="source-detail"><span>Paper activation: {row?.paper_enabled == null ? "UNAVAILABLE" : row.paper_enabled ? "ENABLED" : "DISABLED"} · Process: {row?.worker_state ?? "UNAVAILABLE"}</span><span>Future / option expiry: {row?.latest?.future_expiry ?? "UNAVAILABLE"} / {row?.latest?.option_expiry ?? "UNAVAILABLE"}</span><span>Observation: {row?.latest?.observed_at ? timeIST(row.latest.observed_at) : "UNAVAILABLE"} · Unresolved: {integer(row?.unresolved_exposure)}</span></div>
          <Link href={`/commodity-acts/${instrument}`} className="source-link">View signal & execution evidence<Icon name="arrow" size={13}/></Link>
        </article>;
      })}
    </div>
    <PortfolioEvidence nifty={nifty} commodities={commodities}/>
    <Panel title="STRATEGY & DATA HEALTH"><div className="overview-health-grid">
      <div className="health-tile"><span>NIFTY API / database</span><Badge value={data?.health?.components?.database ?? "UNAVAILABLE"}/><p>Actual API component status</p></div>
      <div className="health-tile"><span>NIFTY market session</span><Badge value={data?.health?.market_session_state ?? "UNAVAILABLE"}/><p>Market-closed is not a data failure</p></div>
      {(["CRUDEOILM", "NATGASMINI"] as const).map(id => { const r = commodities?.find(row => row.instrument_id === id); return <div key={id} className="health-tile"><span>{id} observations</span><Badge value={r?.observation_freshness ?? "UNAVAILABLE"}/><p>{r?.latest?.observed_at ? `${dateIST(r.latest.observed_at)} · ${timeIST(r.latest.observed_at)}` : "No observed timestamp"}</p></div>; })}
    </div></Panel>
    <p className="execution-note">Partial or missing records leave combined metrics unavailable. Paper P&amp;L is descriptive research, not evidence of profitability. Stale marks are never substituted with LTP.</p>
    <span className="sr-only">NIFTY signal freshness: {(nifty?.strategies ?? []).map(row => `${row.display_name}: ${observationState(row.latest_timestamp)}`).join("; ")}</span>
  </>;
}

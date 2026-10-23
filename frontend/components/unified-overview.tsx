"use client";
import Link from "next/link";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { completeSum, observationState } from "@/lib/monitoring";
import { integer, money, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Metric, Panel } from "@/components/ui";

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
  // Missing a server, instrument, metric or current mark never becomes zero.
  const completeCommodity = commodities?.length === 2 && ["CRUDEOILM", "NATGASMINI"].every(id => commodities.some(r => r.instrument_id === id)) ? commodities : null;
  const total = (n: number | null | undefined, field: "open_positions" | "trades_today" | "realized_gross_today" | "unrealized_gross") =>
    completeCommodity && nifty ? completeSum([n, ...completeCommodity.map(row => row[field])]) : null;
  return <Panel title="AI TRADE · UNIFIED PAPER OVERVIEW" className="unified-overview">
    <p className="muted">Independent NIFTY and Commodity databases · Read-only monitoring · No broker order controls</p>
    {error && <p role="alert">MONITORING UNAVAILABLE</p>}
    <div className="phase15-metrics">
      <Metric label="Open paper positions" value={integer(total(nifty?.totals.open_positions, "open_positions"))}/>
      <Metric label="Today's simulated entries" value={integer(total(nifty?.totals.trades_today, "trades_today"))}/>
      <Metric label="Today's realized gross" value={money(total(nifty?.totals.realized_gross_pnl_today, "realized_gross_today"))}/>
      <Metric label="Fresh unrealized gross" value={money(total(nifty?.totals.unrealized_gross_pnl, "unrealized_gross"))}/>
      <Metric label="Combined net / capital efficiency" value="UNAVAILABLE" detail="No inferred costs or broker margin"/>
    </div>
    <div className="unified-sources">
      <section aria-label="NIFTY monitoring status"><h3><Link href="/nifty">NIFTY</Link></h3>
        <Badge value={nifty ? "AVAILABLE" : "UNAVAILABLE"}/>
        <p>API / database: {data?.health?.status ?? "UNAVAILABLE"}. Broker/data status: {data?.health?.components?.market_data ?? "UNAVAILABLE"}.</p>
        <p>Worker process state: UNAVAILABLE. Never inferred from historical trades or decisions.</p>
        <p>Paper strategies: {integer(nifty?.strategies?.length)} · Open: {integer(nifty?.totals.open_positions)} · Entries today: {integer(nifty?.totals.trades_today)}</p>
        <p>Realized gross today: {money(nifty?.totals.realized_gross_pnl_today)} · Unrealized gross: {money(nifty?.totals.unrealized_gross_pnl)}</p>
        <ul>{nifty?.strategies?.map(row => <li key={row.strategy_id}>{row.display_name}: {row.latest_status ?? "UNAVAILABLE"} ·
          {observationState(row.latest_timestamp)} · {row.latest_blocker ?? "No blocker reported"}</li>)}</ul>
      </section>
      <section aria-label="Commodity monitoring status"><h3><Link href="/commodity-acts">COMMODITY</Link></h3>
        <Badge value={commodities ? "AVAILABLE" : "UNAVAILABLE"}/>
        <p>Worker activation, process liveness, quote freshness and entry eligibility are distinct.</p>
        {["CRUDEOILM", "NATGASMINI"].map(instrument => {
          const row = commodities?.find(item => item.instrument_id === instrument);
          return <div key={instrument} className="unified-instrument"><h4><Link href={`/commodity-acts/${instrument}`}>{instrument}</Link></h4>
            <p>Paper activation: {row?.paper_enabled == null ? "UNAVAILABLE" : row.paper_enabled ? "ENABLED" : "DISABLED"} · Process: {row?.worker_state ?? "UNAVAILABLE"}</p>
            <p>Observation: {row?.observation_freshness ?? "UNAVAILABLE"} · Session: {row?.market_open == null ? "UNAVAILABLE" : row.market_open ? "OPEN" : "CLOSED"}</p>
            <p>Open: {integer(row?.open_positions)} · Entries today: {integer(row?.trades_today)} · Realized gross today: {money(row?.realized_gross_today)}</p>
            <p>Future / option expiry: {row?.latest?.future_expiry ?? "UNAVAILABLE"} / {row?.latest?.option_expiry ?? "UNAVAILABLE"}</p>
            <p>Latest decision: {row?.latest?.decision?.status ?? "UNAVAILABLE"} · Score: {row?.latest?.decision?.opportunity_score ?? "UNAVAILABLE"}</p>
            <p>Alerts: {[...(row?.latest?.risk_blocks ?? []), ...(row?.latest?.decision?.blockers ?? [])].join(", ") || "No alerts available"}</p>
            <p>Observation time: {row?.latest?.observed_at ? timeIST(row.latest.observed_at) : "UNAVAILABLE"} IST · Unresolved exposure: {integer(row?.unresolved_exposure)}</p>
          </div>;
        })}
      </section>
    </div>
    <p className="execution-note">Partial or missing records leave combined metrics unavailable. Paper P&amp;L is descriptive research, not evidence of profitability. Stale marks are never substituted with LTP.</p>
  </Panel>;
}

"use client";
import { MetricTile, PnL, Tiles, Unavailable, Val } from "@/components/terminal/primitives";
import { int, pct, rupees } from "@/lib/terminal-format";
import type { Risk, Summary } from "@/types/portfolio";

const shortLabel = (label: string) => label.replace(/^ACTS V3 /, "").replace(/ Futures$/, "").replace(/ \(retired\)/, " (ret.)").replace(/^NIFTY ACTS /, "NIFTY ");
const cost = (v: number | null) => <Val>{v == null ? null : rupees(v)}</Val>;

/** Top-of-page metrics. Every number is the API's; partial totals are labelled next to the total, never hidden. */
export function Metrics({ summary, risk, riskFailed }: { summary: Summary; risk: Risk | null; riskFailed: boolean }) {
  const { pnl, counts } = summary;
  const excluded = pnl.excluded_open_positions, unpriced = pnl.excluded_unpriced_closed + (pnl.excluded_unpriced_exposure ?? 0);
  const partialNote = pnl.partial ? `PARTIAL · excludes ${excluded} open position${excluded === 1 ? "" : "s"} without an executable mark${unpriced ? ` and ${unpriced} expired/closed without a price` : ""}` : null;
  const nothingPriced = excluded > 0 && excluded >= counts.open_positions;
  const budgets = (risk?.engines ?? []).filter(e => e.max_entries_per_day != null || e.remaining_daily_loss_budget != null || e.open_positions > 0 || e.daily_loss_limit != null);
  return <>
    {pnl.partial && <div className="tm-banner" data-tone="warn" role="status" aria-label="Partial totals"><b>PARTIAL TOTALS</b>
      <div>{pnl.note ?? partialNote} <span className="tm-muted">Excluded exposure is not counted as zero: the true total is unknown until it is priced or settled.</span></div></div>}
    <Tiles lead>
      <MetricTile primary label="Total net P&L" value={<PnL value={pnl.total_net_pnl}/>} warnNote={pnl.partial} note={partialNote ?? <>Net liquidation <PnL value={pnl.net_liquidation_pnl}/></>}/>
      <MetricTile primary label="Realized net" value={<PnL value={pnl.realized_net}/>} note={<>Gross <PnL value={pnl.realized_gross}/></>}/>
      <MetricTile primary label="Unrealized" value={<PnL value={nothingPriced ? null : pnl.unrealized_gross} reason={nothingPriced || pnl.unrealized_gross == null ? "no executable marks" : null}/>} warnNote={excluded > 0} note={excluded > 0 ? `${excluded} open position${excluded === 1 ? "" : "s"} excluded (no fresh executable mark)` : "gross, executable marks only"}/>
      <MetricTile primary label="Open positions" value={int(counts.open_positions)} warnNote={counts.unresolved_positions > 0} note={counts.unresolved_positions > 0 ? `${counts.unresolved_positions} UNRESOLVED / needs attention` : risk ? `defined risk ${rupees(risk.exposure.defined_risk_total, 0)}` : null}/>
    </Tiles>
    <Tiles>
      <MetricTile label="Realized gross" value={<PnL value={pnl.realized_gross}/>}/>
      <MetricTile label="Incurred charges" value={cost(pnl.incurred_charges)} note={pnl.incurred_charges_detail ? `${pnl.incurred_charges_detail.orders ?? "—"} orders · brokerage assumption unverified` : null}/>
      <MetricTile label="Est. exit charges" value={cost(pnl.estimated_exit_charges)} note="estimate for open positions, not incurred"/>
      <MetricTile label="Executed trades" value={int(counts.executed_trades)} note={`${counts.closed_trades} closed · ${counts.winning_trades} winning`}/>
      <MetricTile label="Pending orders" value={int(counts.pending_orders)}/>
      <MetricTile label="Rejected orders" value={int(counts.rejected_orders)} note={`${counts.orders_in_window} orders in window`}/>
      <MetricTile wide label="Remaining daily risk budget" value={riskFailed ? <Unavailable reason="risk data unavailable"/> : budgets.length ? <span style={{ fontSize: 11.5, fontWeight: 500, display: "grid", gap: 1 }}>{budgets.map(e => <span key={e.engine_id} title={e.limits_source ?? undefined} style={{ display: "flex", justifyContent: "space-between", gap: 12, whiteSpace: "nowrap" }}><span>{shortLabel(e.label)}</span> <b><Val>{e.remaining_daily_loss_budget == null ? null : rupees(e.remaining_daily_loss_budget, 0)}</Val></b></span>)}</span> : <Val reason="no per-engine limits">{null}</Val>} note="per engine · daily loss limit minus realized loss"/>
      <MetricTile label="Risk utilisation" value={risk?.exposure.margin_utilization == null ? <Unavailable/> : pct(risk.exposure.margin_utilization * 100)} note={risk?.exposure.margin_utilization == null ? (risk?.exposure.capital_note ?? "margin / capital not configured") : "of allocated capital"}/>
    </Tiles>
  </>;
}

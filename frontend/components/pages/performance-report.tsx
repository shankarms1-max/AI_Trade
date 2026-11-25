"use client";
import { BarsByBucket, LineSeries } from "@/components/terminal/charts";
import { DataTable, type Column } from "@/components/terminal/data-table";
import { EmptyState } from "@/components/terminal/states";
import { MetricTile, Panel, PnL, Tiles, Unavailable, Val } from "@/components/terminal/primitives";
import { calendarDate, duration, int, istDateTime, pctFraction, ratio, rupees, title } from "@/lib/terminal-format";
import type { Bucket, PerfStats, Performance, StrategyPerf } from "@/types/portfolio";

const SMALL_SAMPLE = 30;
const day = (d: string) => calendarDate(d).replace(/ \d{4}$/, "");

function Kpis({ o }: { o: PerfStats }) {
  const none = o.trades === 0;
  return <>
    <Tiles lead>
      <MetricTile primary label="Cumulative net P&L" value={<PnL value={o.net_pnl}/>} note={`${int(o.trades)} closed trades · after charges`}/>
      <MetricTile label="Win rate" value={none ? <Val>{null}</Val> : pctFraction(o.win_rate)} note={`${int(o.wins)} W / ${int(o.losses)} L`}/>
      <MetricTile label="Expectancy / trade" value={<PnL value={o.expectancy_net}/>} note="net"/>
      <MetricTile label="Profit factor" value={<Val reason="needs at least one losing trade">{o.profit_factor == null ? null : ratio(o.profit_factor)}</Val>}/>
      <MetricTile label="Max drawdown" value={<span className="tm-pnl">{rupees(o.max_drawdown)}</span>} note="peak to trough, realized"/>
      <MetricTile label="Current drawdown" value={<span className="tm-pnl">{rupees(o.current_drawdown)}</span>}/>
    </Tiles>
    <Tiles>
      <MetricTile label="Avg hold" value={duration(o.avg_holding_seconds)}/>
      <MetricTile label="Largest win" value={<PnL value={o.largest_win}/>}/>
      <MetricTile label="Largest loss" value={<PnL value={o.largest_loss}/>}/>
      <MetricTile label="Max consecutive wins" value={int(o.max_consecutive_wins)}/>
      <MetricTile label="Max consecutive losses" value={int(o.max_consecutive_losses)}/>
      <MetricTile label="Gross profit" value={<PnL value={o.gross_profit}/>}/>
      <MetricTile label="Gross loss" value={<PnL value={o.gross_loss}/>}/>
      <MetricTile label="Costs (charges)" value={<span className="tm-pnl">{rupees(o.charges)}</span>} note="brokerage assumption unverified"/>
    </Tiles>
  </>;
}

const bucketCols = (label: string): Array<Column<Bucket>> => [
  { key: "b", header: label, render: b => b.bucket }, { key: "t", header: "Trades", align: "right", render: b => int(b.trades) },
  { key: "n", header: "Net P&L", align: "right", render: b => <PnL value={b.net_pnl}/> }, { key: "w", header: "Win rate", align: "right", render: b => pctFraction(b.win_rate) },
];
function BucketPanel({ heading, label, data, note }: { heading: string; label: string; data: Bucket[]; note?: string }) {
  return <Panel title={heading}>{data.length ? <>
    <BarsByBucket label={`${heading}: net P&L by bucket`} data={data.map(b => ({ name: b.bucket, value: b.net_pnl }))}/>
    <DataTable label={heading} rows={data} rowKey={b => b.bucket} columns={bucketCols(label)}/>
    {note && <p className="tm-caption">{note}</p>}
  </> : <EmptyState>No data</EmptyState>}</Panel>;
}

const stratCols: Array<Column<StrategyPerf>> = [
  { key: "s", header: "Strategy", sticky: true, render: s => <b>{s.label}</b> },
  { key: "p", header: "Sample period", render: s => <span title="First to last exit in this sample">{istDateTime(s.first_exit)} → {istDateTime(s.last_exit)}</span> },
  { key: "t", header: "Trades", align: "right", render: s => int(s.trades) },
  { key: "w", header: "Win rate", align: "right", render: s => pctFraction(s.win_rate) },
  { key: "e", header: "Expectancy", align: "right", render: s => <PnL value={s.expectancy_net}/> },
  { key: "pf", header: "PF", align: "right", render: s => <Val>{s.profit_factor == null ? null : ratio(s.profit_factor)}</Val> },
  { key: "dd", header: "Max DD", align: "right", render: s => rupees(s.max_drawdown) },
  { key: "n2d", header: "Net / max DD", align: "right", render: s => <Val reason="no drawdown">{s.net_to_max_drawdown == null ? null : ratio(s.net_to_max_drawdown)}</Val> },
  { key: "net", header: "Net P&L", align: "right", render: s => <PnL value={s.net_pnl}/> },
  { key: "c", header: "Charges", align: "right", render: s => rupees(s.charges) },
];

/** Everything computed by GET /api/portfolio/performance. Only real figures; unavailable metrics are stated, not invented. */
export function PerformanceReport({ perf, compare = true }: { perf: Performance; compare?: boolean }) {
  const o = perf.overall;
  const daily = perf.daily.map(d => ({ x: day(d.date), cumulative: d.cum_net, net: d.net }));
  const unavailable = Object.entries(perf.unavailable);
  if (o.trades === 0) return <EmptyState hint="No closed trades in this window/filter.">No closed trades</EmptyState>;
  return <div>
    {o.trades < SMALL_SAMPLE && <div className="tm-banner" data-tone="warn" role="status"><b>SMALL SAMPLE</b><div>{o.trades} closed trades. Ratios, win rate and expectancy are anecdotal at this size; do not read them as a track record.</div></div>}
    {perf.excluded_trades_without_exit_price > 0 && <div className="tm-banner" data-tone="warn" role="status"><b>EXCLUDED</b><div>{perf.excluded_trades_without_exit_price} trade(s) without a recorded exit price are excluded from these statistics (not counted as zero).</div></div>}
    <Kpis o={o}/>
    <div className="tm-grid c2">
      <Panel title="Cumulative net P&L (by session date)">
        {daily.length ? <LineSeries label="Cumulative net P&L by session" data={daily} xKey="x" series={[{ key: "cumulative", name: "Cumulative net", color: "var(--tm-chart-1)" }]}/> : <EmptyState>No daily data</EmptyState>}
      </Panel>
      <Panel title="Daily net P&L (IST session date)">
        {daily.length ? <BarsByBucket label="Daily net P&L" data={daily.map(d => ({ name: d.x, value: d.net }))}/> : <EmptyState>No daily data</EmptyState>}
      </Panel>
    </div>
    <div className="tm-grid c2">
      <Panel title="Monthly net P&L">{perf.monthly.length ? <BarsByBucket label="Monthly net P&L" data={perf.monthly.map(m => ({ name: m.month, value: m.net_pnl }))}/> : <EmptyState>No monthly data</EmptyState>}</Panel>
      <Panel title="Long / short" flush>
        <DataTable label="Long and short" rows={perf.by_direction} rowKey={d => d.direction} columns={[
          { key: "d", header: "Direction", render: d => d.direction }, { key: "t", header: "Trades", align: "right", render: d => int(d.trades) },
          { key: "w", header: "Win rate", align: "right", render: d => pctFraction(d.win_rate) }, { key: "e", header: "Expectancy", align: "right", render: d => <PnL value={d.expectancy_net}/> },
          { key: "n", header: "Net P&L", align: "right", render: d => <PnL value={d.net_pnl}/> },
        ]}/>
      </Panel>
    </div>
    {compare && <Panel title="Strategy comparison" flush>
      <DataTable label="Strategy comparison" rows={perf.by_strategy} rowKey={s => s.engine_id} columns={stratCols}/>
      {perf.comparison_note && <p className="tm-caption" style={{ padding: "0 10px" }} data-testid="comparison-note">{perf.comparison_note}</p>}
    </Panel>}
    <div className="tm-grid c2">
      <BucketPanel heading="Exit-reason contribution" label="Exit reason" data={perf.exit_reason_contribution.map(b => ({ ...b, bucket: title(b.bucket) }))}/>
      <BucketPanel heading="Entry score vs outcome" label="Score bucket" data={perf.entry_score_vs_outcome}/>
      <BucketPanel heading="Time of day (IST entry hour)" label="Hour" data={perf.time_of_day}/>
      <BucketPanel heading="Holding duration" label="Held" data={perf.holding_duration}/>
    </div>
    <div className="tm-grid c2">
      {perf.excursions.trades_with_data > 0 && <Panel title="MAE / MFE">
        <Tiles>
          <MetricTile label="Avg MAE (worst open loss)" value={<span className="tm-pnl">{rupees(perf.excursions.avg_mae)}</span>}/>
          <MetricTile label="Avg MFE (best open gain)" value={<span className="tm-pnl">{rupees(perf.excursions.avg_mfe)}</span>}/>
          <MetricTile label="Trades with excursion data" value={int(perf.excursions.trades_with_data)} note={`of ${int(o.trades)}`}/>
        </Tiles>
      </Panel>}
      <Panel title="Cost sensitivity (net P&L at brokerage per order)" flush>
        <DataTable label="Cost sensitivity" rows={Object.entries(perf.cost_sensitivity_net_pnl)} rowKey={([k]) => k} columns={[
          { key: "k", header: "Scenario", render: ([k]) => `₹${k.replace(/^brokerage_/, "").replace(/_per_order$/, "")} per order` }, { key: "v", header: "Net P&L", align: "right", render: ([, v]) => <PnL value={v}/> },
        ]}/>
        <p className="tm-caption" style={{ padding: "0 10px" }}>Brokerage is a scenario, not a verified account plan.</p>
      </Panel>
      <Panel title="Opportunity rejections" flush>
        <DataTable label="Opportunity rejections" rows={perf.opportunity_rejections} rowKey={r => r.reason} empty={<EmptyState>None recorded</EmptyState>} columns={[
          { key: "r", header: "Reason", render: r => title(r.reason) }, { key: "c", header: "Count", align: "right", render: r => int(r.count) },
        ]}/>
      </Panel>
      {unavailable.length > 0 && <Panel title="Unavailable metrics">
        <ul className="tm-list" style={{ listStyle: "none", paddingLeft: 0 }}>{unavailable.map(([k, reason]) => <li key={k}><Unavailable label={`${title(k)} UNAVAILABLE`} reason={reason.replace(/^UNAVAILABLE:\s*/, "")}/></li>)}</ul>
      </Panel>}
    </div>
  </div>;
}

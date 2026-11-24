"use client";
import { useUrlState } from "@/components/filters-context";
import { useDrawerLinks } from "@/components/pages/drawers";
import { shortContract } from "@/components/pages/position-columns";
import { DataTable, Pager, TABLE_MAX, type Column } from "@/components/terminal/data-table";
import { FilterSelect } from "@/components/terminal/filters";
import { EmptyState, LiveGate, ResourceView } from "@/components/terminal/states";
import { MetricTile, PageTitle, Panel, PnL, StatusBadge, StrategyChip, Tiles, Val } from "@/components/terminal/primitives";
import { LIVE_INTERVAL, ANALYTICS_INTERVAL, useResource } from "@/hooks/use-resource";
import { filterParams, type Filters } from "@/lib/filters";
import { parse, urls, type Params } from "@/lib/portfolio-api";
import { duration, int, istDateTime, num, pct, rupees, title } from "@/lib/terminal-format";
import type { Trade } from "@/types/portfolio";

export const TRADE_PAGE_SIZE = 50;
const DIRECTIONS = ["LONG", "SHORT", "BULL", "BEAR"];
const RESULTS = ["win", "loss"];
export type TradeLocal = { result: string | null; exit_reason: string | null; direction: string | null };
/** Filters shared by the table and the CSV export. */
export const tradeFilterParams = (filters: Filters, local: TradeLocal): Params => ({ ...filterParams(filters, "all"), result: local.result, exit_reason: local.exit_reason, direction: local.direction });
/** GET /api/portfolio/trades query: filters + API sort + server pagination. */
export const tradesQuery = (filters: Filters, local: TradeLocal, sort: string, desc: boolean, offset: number): Params =>
  ({ ...tradeFilterParams(filters, local), sort, desc, limit: TRADE_PAGE_SIZE, offset });
/** The CSV export is a plain link carrying exactly the active filters (no sort/page: the file is the whole filtered book). */
export const tradesCsvHref = (filters: Filters, local: TradeLocal) => urls.tradesCsv(tradeFilterParams(filters, local));

export function Tradebook() {
  const { filters, get, set } = useUrlState();
  const { openPosition } = useDrawerLinks();
  const local: TradeLocal = { result: get("result"), exit_reason: get("exit_reason"), direction: get("direction") };
  const sort = get("sort") ?? "exit_at", desc = get("desc") !== "false";
  const offset = Math.max(0, Number(get("offset")) || 0);
  const live = filters.mode === "LIVE";
  const trades = useResource(live ? null : urls.trades(tradesQuery(filters, local, sort, desc, offset)), parse.trades, LIVE_INTERVAL, { keepPrevious: "trades" });
  const perf = useResource(live ? null : urls.performance(filterParams(filters, "all")), parse.performance, ANALYTICS_INTERVAL);
  const reasons = (perf.data?.exit_reason_contribution ?? []).map(b => b.bucket);
  const columns: Array<Column<Trade>> = [
    { key: "id", header: "Trade", sticky: true, sortKey: "trade_id", render: t => <button type="button" className="tm-link tm-mono" onClick={() => openPosition(t.position_id)} aria-label={`Open trade ${t.trade_id}`}>{t.trade_id.slice(0, 10)}</button> },
    { key: "strategy", header: "Strategy", sortKey: "strategy", render: t => <StrategyChip label={t.strategy}/> },
    { key: "version", header: "Version", sortKey: "version", render: t => t.version },
    { key: "instrument", header: "Instrument", sortKey: "instrument", render: t => t.instrument },
    { key: "contract", header: "Contract", sortKey: "contract", render: t => <span className="tm-mono" title={t.contract ?? undefined}>{t.contract ? shortContract(t.contract) : "—"}</span> },
    { key: "direction", header: "Dir", sortKey: "direction", render: t => t.direction ?? "—" },
    { key: "mode", header: "Mode", sortKey: "mode", render: t => <StatusBadge value={t.mode} tone="info"/> },
    { key: "entry_at", header: "Entry time", sortKey: "entry_at", render: t => istDateTime(t.entry_at) },
    { key: "entry_price", header: "Entry", align: "right", sortKey: "entry_price", render: t => num(t.entry_price) },
    { key: "exit_at", header: "Exit time", sortKey: "exit_at", render: t => istDateTime(t.exit_at) },
    { key: "exit_price", header: "Exit", align: "right", sortKey: "exit_price", render: t => <Val reason="no exit price recorded">{t.exit_price == null ? null : num(t.exit_price)}</Val> },
    { key: "hold", header: "Held", align: "right", sortKey: "holding_seconds", render: t => duration(t.holding_seconds) },
    { key: "lots", header: "Lots", align: "right", sortKey: "lots", render: t => int(t.lots) },
    { key: "qty", header: "Qty", align: "right", sortKey: "quantity", render: t => int(t.quantity) },
    { key: "gross", header: "Gross", align: "right", sortKey: "gross_pnl", render: t => <PnL value={t.gross_pnl} reason={t.exit_price == null ? "no exit price" : null}/> },
    { key: "charges", header: "Charges", align: "right", sortKey: "charges", render: t => <PnL value={t.charges} signed={false} className="pnl-zero"/> },
    { key: "net", header: "Net", align: "right", sortKey: "net_pnl", render: t => <PnL value={t.net_pnl} reason={t.exit_price == null ? "no exit price" : null}/> },
    { key: "ror", header: "% of def. risk", align: "right", title: "Net return on defined risk", render: t => pct(t.return_on_defined_risk_pct) },
    { key: "entry_reason", header: "Entry reason", sortKey: "entry_reason", render: t => title(t.entry_reason) },
    { key: "exit_reason", header: "Exit reason", sortKey: "exit_reason", render: t => title(t.exit_reason) },
    { key: "score", header: "Entry score", align: "right", sortKey: "entry_score", render: t => <Val>{t.entry_score == null ? null : num(t.entry_score, 1)}</Val> },
  ];
  return <div>
    <PageTitle title="Tradebook" note={`${filters.mode} · closed trades · sorted by the API`} actions={
      <a className="tm-btn" href={tradesCsvHref(filters, local)} download="tradebook.csv" data-testid="csv-export">Export CSV</a>}/>
    <LiveGate live={live}>
      <div className="tm-filter-row" style={{ padding: "0 0 8px", background: "none" }} role="group" aria-label="Trade filters">
        <FilterSelect label="Direction" value={local.direction} options={DIRECTIONS} onChange={v => set({ direction: v, offset: null })}/>
        <FilterSelect label="Result" value={local.result} options={RESULTS} onChange={v => set({ result: v, offset: null })}/>
        <FilterSelect label="Exit reason" value={local.exit_reason} options={reasons} onChange={v => set({ exit_reason: v, offset: null })}/>
        <span className="tm-muted" style={{ fontSize: 11 }}>Strategy, instrument, version, mode and date range are set in the header.</span>
      </div>
      <Panel title="Trades" flush actions={trades.placeholder ? <span className="tm-muted">updating…</span> : null}>
        <ResourceView resource={trades} label="Tradebook" isEmpty={d => d.items.length === 0} empty={<EmptyState>No closed trades match these filters</EmptyState>}>{d => <>
          <div style={{ padding: 8 }}><Tiles>
            <MetricTile label="Trades (filtered)" value={int(d.totals.trades)}/>
            <MetricTile label="Gross P&L" value={<PnL value={d.totals.gross_pnl}/>}/>
            <MetricTile label="Charges" value={<span className="tm-pnl">{rupees(d.totals.charges)}</span>}/>
            <MetricTile primary label="Net P&L" value={<PnL value={d.totals.net_pnl}/>} note="all filtered trades, not just this page"/>
          </Tiles></div>
          <DataTable maxHeight={TABLE_MAX} label="Trades" rows={d.items} rowKey={t => t.position_id} columns={columns} onRowClick={t => openPosition(t.position_id)} sort={{ key: sort, desc }}
            onSort={(key, nextDesc) => set({ sort: key === "exit_at" ? null : key, desc: nextDesc ? null : "false", offset: null })}
            footerLabel={`Totals · all ${d.totals.trades} filtered trades`}
            footer={{ gross: <PnL value={d.totals.gross_pnl}/>, charges: <span>{rupees(d.totals.charges)}</span>, net: <PnL value={d.totals.net_pnl}/> }}/>
          <div style={{ padding: "0 8px 6px" }}><Pager total={d.total} limit={d.limit} offset={d.offset} onOffset={o => set({ offset: o ? String(o) : null })} noun="trades"/></div>
        </>}</ResourceView>
      </Panel>
    </LiveGate>
  </div>;
}

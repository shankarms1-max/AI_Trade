"use client";
import { useUrlState } from "@/components/filters-context";
import { SIMULATED, useDrawerLinks } from "@/components/pages/drawers";
import { DataTable, Pager, TABLE_MAX, type Column } from "@/components/terminal/data-table";
import { FilterSelect } from "@/components/terminal/filters";
import { EmptyState, LiveGate, ResourceView } from "@/components/terminal/states";
import { PageTitle, Panel, StatusBadge, StrategyChip, Val } from "@/components/terminal/primitives";
import { LIVE_INTERVAL, useResource } from "@/hooks/use-resource";
import { filterParams } from "@/lib/filters";
import { parse, urls } from "@/lib/portfolio-api";
import { int, istDateTime, num, title } from "@/lib/terminal-format";
import type { Order } from "@/types/portfolio";

export const ORDER_PAGE_SIZE = 50;
const INTENTS = ["ENTRY", "EXIT"];
/** Query for GET /api/portfolio/orders (pure: asserted in tests). */
export const ordersQuery = (filters: Parameters<typeof filterParams>[0], status: string | null, intent: string | null, offset: number) =>
  ({ ...filterParams(filters, "all"), status, intent, limit: ORDER_PAGE_SIZE, offset });

export function Orders() {
  const { filters, get, set } = useUrlState();
  const { openOrder, openPosition } = useDrawerLinks();
  const status = get("status"), intent = get("intent");
  const offset = Math.max(0, Number(get("offset")) || 0);
  const live = filters.mode === "LIVE";
  const list = useResource(live ? null : urls.orders(ordersQuery(filters, status, intent, offset)), parse.orders, LIVE_INTERVAL, { keepPrevious: "orders" });
  // status chips must not collapse to the selected status: counts come from the request without the status filter
  const counts = useResource(live ? null : urls.orders({ ...ordersQuery(filters, null, intent, 0), limit: 1 }), parse.orders);
  const chips = counts.data?.status_counts ?? list.data?.status_counts ?? {};
  const columns: Array<Column<Order>> = [
    { key: "id", header: "Order id", sticky: true, render: o => <button type="button" className="tm-link tm-mono" onClick={() => openOrder(o)} aria-label={`Open order ${o.order_id}`}>{o.order_id}</button> },
    { key: "broker", header: "Broker id", render: o => <span className="tm-muted" title="Paper order: no broker order exists">{o.broker_order_id ?? SIMULATED}</span> },
    { key: "pos", header: "Position", render: o => <button type="button" className="tm-link" onClick={() => openPosition(o.position_id)} aria-label={`Open position ${o.position_id}`}>{o.trade_id ?? o.position_id}</button> },
    { key: "strategy", header: "Strategy", render: o => <StrategyChip label={o.strategy_label}/> },
    { key: "version", header: "Version", render: o => o.strategy_version ?? "—" },
    { key: "contract", header: "Contract", render: o => <span className="tm-mono">{o.contract ?? "—"}</span> },
    { key: "side", header: "Side", render: o => o.side },
    { key: "intent", header: "Intent", render: o => o.intent },
    { key: "type", header: "Type", render: o => <span title={o.order_type}>{title(o.order_type).replace("SIMULATED ", "sim. ").toLowerCase()}</span> },
    { key: "rq", header: "Req. qty", align: "right", render: o => int(o.requested_quantity) },
    { key: "fq", header: "Filled", align: "right", render: o => int(o.filled_quantity) },
    { key: "remq", header: "Remaining", align: "right", render: o => int(o.remaining_quantity) },
    { key: "rp", header: "Req. price", align: "right", render: o => <Val reason="market-style simulated order">{o.requested_price == null ? null : num(o.requested_price)}</Val> },
    { key: "ap", header: "Avg fill", align: "right", render: o => <Val reason="not filled">{o.avg_fill_price == null ? null : num(o.avg_fill_price)}</Val> },
    { key: "trig", header: "Trigger", align: "right", render: o => <Val>{o.trigger_price == null ? null : num(o.trigger_price)}</Val> },
    { key: "sub", header: "Submitted", render: o => istDateTime(o.submitted_at) },
    { key: "upd", header: "Last update", render: o => istDateTime(o.last_update_at) },
    { key: "status", header: "Status", render: o => <span><StatusBadge dimension="order" value={o.status}/> <small className="tm-muted">{o.simulated ? "simulated" : ""}</small></span> },
    { key: "why", header: "Rejection reason", render: o => <Val>{o.rejection_reason ? title(o.rejection_reason) : null}</Val> },
  ];
  return <div>
    <PageTitle title="Orders" note={`${filters.mode} · simulated paper orders reconstructed from the journals · broker order id is n/a`}/>
    <LiveGate live={live}>
      <div className="tm-filter-row" style={{ padding: "0 0 8px", background: "none" }} role="group" aria-label="Order filters">
        <FilterSelect label="Status" value={status} options={Object.keys(chips)} onChange={v => set({ status: v, offset: null })}/>
        <FilterSelect label="Intent" value={intent} options={INTENTS} onChange={v => set({ intent: v, offset: null })}/>
        <span className="tm-segment" role="group" aria-label="Status counts">
          {Object.entries(chips).map(([s, n]) => <button key={s} type="button" className="tm-btn" aria-pressed={status === s} onClick={() => set({ status: status === s ? null : s, offset: null })}>{title(s)} <b>{n}</b></button>)}
        </span>
      </div>
      <Panel title="Orders" flush actions={list.data?.note ? <span className="tm-muted">{list.data.note}</span> : null}>
        <ResourceView resource={list} label="Orders" isEmpty={d => d.items.length === 0} empty={<EmptyState>No orders match these filters</EmptyState>}>{d => <>
          <DataTable maxHeight={TABLE_MAX} label="Orders" rows={d.items} rowKey={o => o.order_id} columns={columns} onRowClick={openOrder} selectedKey={get("order")} rowClass={o => o.status === "REJECTED" ? "is-warn" : undefined}/>
          <div style={{ padding: "0 8px 6px" }}><Pager total={d.total} limit={d.limit} offset={d.offset} onOffset={o => set({ offset: o ? String(o) : null })} noun="orders"/></div>
        </>}</ResourceView>
      </Panel>
    </LiveGate>
  </div>;
}

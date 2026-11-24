"use client";
import { useUrlState } from "@/components/filters-context";
import { DetailDrawer } from "@/components/terminal/drawer";
import { DataTable } from "@/components/terminal/data-table";
import { LineSeries } from "@/components/terminal/charts";
import { ResourceView } from "@/components/terminal/states";
import { ExpandableLegs } from "@/components/terminal/legs";
import { FlagBadges, KV, MarkBadge, Panel, PnL, StatusBadge, StrategyChip, Val } from "@/components/terminal/primitives";
import { LIVE_INTERVAL, useResource } from "@/hooks/use-resource";
import { parse, urls } from "@/lib/portfolio-api";
import { calendarDate, duration, int, istClock, istDateTime, num, rupees, title } from "@/lib/terminal-format";
import { parseTimestamp } from "@/lib/format";
import type { Order, PositionDetail } from "@/types/portfolio";

export const SIMULATED = "n/a (simulated)";
const COMPONENT_ORDER = ["brokerage", "stt", "ctt", "exchange", "sebi_fee", "stamp_duty", "gst"];

function ChargesTable({ calc }: { calc: PositionDetail["pnl_calculation"] }) {
  const { entry, exit, incurred } = calc.charges;
  const keys = [...new Set([...COMPONENT_ORDER, ...Object.keys(incurred?.items ?? {}), ...Object.keys(entry?.items ?? {}), ...Object.keys(exit?.items ?? {})])]
    .filter(k => incurred?.items[k] != null || entry?.items[k] != null || exit?.items[k] != null || calc.charge_status[k] != null);
  return <div className="tm-table-wrap"><table className="tm-table" aria-label="Charges breakdown">
    <thead><tr><th>Component</th><th className="tm-num">Entry</th><th className="tm-num">Exit</th><th className="tm-num">Incurred</th><th>Status</th></tr></thead>
    <tbody>
      {keys.map(k => <tr key={k}>
        <td>{title(k)}</td><td className="tm-num">{entry?.items[k] == null ? "—" : num(entry.items[k])}</td>
        <td className="tm-num">{exit?.items[k] == null ? "—" : num(exit.items[k])}</td>
        <td className="tm-num">{incurred?.items[k] == null ? "—" : num(incurred.items[k])}</td>
        <td className="wrap" style={{ whiteSpace: "normal" }}><StatusBadge value={(calc.charge_status[k] ?? "UNKNOWN").split(":")[0].split(" ")[0]} title={calc.charge_status[k]}/> <small className="tm-muted">{calc.charge_status[k]}</small></td>
      </tr>)}
    </tbody>
    <tfoot><tr><td>Total</td><td className="tm-num">{entry ? num(entry.total) : "—"}</td><td className="tm-num">{exit ? num(exit.total) : "—"}</td><td className="tm-num">{incurred ? num(incurred.total) : "—"}</td><td/></tr></tfoot>
  </table></div>;
}

export function OrdersTable({ orders, label, onOpenOrder }: { orders: Order[]; label: string; onOpenOrder?: (o: Order) => void }) {
  return <DataTable label={label} rows={orders} rowKey={o => o.order_id} columns={[
    { key: "id", header: "Order", render: o => onOpenOrder ? <button type="button" className="tm-link tm-mono" onClick={() => onOpenOrder(o)}>{o.order_id}</button> : <span className="tm-mono">{o.order_id}</span> },
    { key: "side", header: "Side", render: o => `${o.side} · ${o.intent}` },
    { key: "qty", header: "Filled / req.", align: "right", render: o => `${int(o.filled_quantity)} / ${int(o.requested_quantity)}` },
    { key: "px", header: "Avg fill", align: "right", render: o => <Val reason="not filled">{o.avg_fill_price == null ? null : num(o.avg_fill_price)}</Val> },
    { key: "status", header: "Status", render: o => <span><StatusBadge dimension="order" value={o.status}/> <small className="tm-muted">simulated</small></span> },
    { key: "when", header: "Submitted", render: o => istDateTime(o.submitted_at) },
    { key: "why", header: "Reason", render: o => title(o.rejection_reason) },
  ]}/>;
}

export function PositionDetailView({ detail }: { detail: PositionDetail }) {
  const p = detail.position, calc = detail.pnl_calculation, ev = detail.entry_evidence;
  const marks = detail.marks.filter(m => m.price != null).map(m => ({ x: istClock(m.t).slice(0, 5), t: parseTimestamp(m.t) ?? 0, price: m.price }));
  return <div>
    {(p.flags.length > 0 || p.status === "UNRESOLVED") && <div className="tm-banner" data-tone="crit" role="alert"><b>UNRESOLVED</b><div>{p.unresolved_reason ?? "This position needs attention."} <FlagBadges flags={p.flags}/> It is excluded from P&amp;L totals (not treated as zero).</div></div>}
    <Panel title="Position">
      <KV items={[
        ["Strategy", <StrategyChip key="s" label={p.strategy_label} family={p.family} version={p.strategy_version}/>],
        ["Status", <span key="st"><StatusBadge dimension="position" value={p.status}/> <FlagBadges flags={p.flags}/></span>],
        ["Mode", <span key="m"><StatusBadge value={p.mode} tone="info"/> <small className="tm-muted">simulated</small></span>],
        ["Instrument · expiry", `${p.instrument} · ${calendarDate(p.expiry)}`], ["Direction", p.direction ?? "—"],
        ["Lots · qty", `${int(p.lots)} · ${int(p.quantity)} (lot ${int(p.lot_size)})`],
        ["Entry", `${istDateTime(p.entry_at)} @ ${num(p.entry_price)}`],
        ["Exit", p.exit_at ? `${istDateTime(p.exit_at)} @ ${p.exit_price == null ? "—" : num(p.exit_price)}` : "—"],
        ["Executable mark", <span key="mk"><Val reason={p.mark.reason}>{p.mark.price == null ? null : num(p.mark.price)}</Val> <MarkBadge basis={p.mark.basis}/></span>],
        ["Stop · trailing · target", `${p.stop?.level == null ? "—" : num(p.stop.level)} · ${p.trailing_stop?.level == null ? "—" : num(p.trailing_stop.level)} · ${p.target?.level == null ? "—" : num(p.target.level)}`],
        ["Stop rule", p.stop?.rule ?? "—"],
        ["Unrealized gross", <PnL key="u" value={p.unrealized_gross} reason={p.mark.reason}/>], ["Est. net liquidation", <PnL key="n" value={p.est_net_liquidation} reason={p.mark.reason}/>],
        ["Realized gross", <PnL key="g" value={p.realized_gross}/>], ["Realized net", <PnL key="rn" value={p.realized_net}/>],
        ["Defined max loss", rupees(p.defined_max_loss)], ["Held", duration(p.holding_seconds)],
        ["Entry reason", title(p.entry_reason)], ["Exit reason", title(p.exit_reason)],
      ]}/>
    </Panel>
    <Panel title="Legs"><ExpandableLegs position={p}/></Panel>
    <Panel title="Timeline (journal events)">
      {detail.timeline.length ? <ol className="tm-timeline" aria-label="Journal events">{detail.timeline.map((e, i) => <li key={`${e.sequence}-${i}`}><b>{title(e.type)}</b> <span className="tm-muted">#{e.sequence ?? "—"} · {istDateTime(e.at)}{e.state ? ` · ${title(e.state)}` : ""}</span>{e.reason ? <div className="tm-muted">{title(e.reason)}</div> : null}</li>)}</ol> : <div className="tm-muted">No journal events recorded for this position.</div>}
    </Panel>
    <Panel title="Entry score and cycle">
      <KV items={[["Score", ev.score == null ? "—" : `${num(ev.score, 1)}${ev.score_band ? ` · ${title(ev.score_band)}` : ""}`], ["Cycle id", <span key="c" className="tm-mono">{ev.cycle_id ?? "—"}</span>], ["Re-arm reason", title(ev.rearm_reason)], ["Entry reason", title(ev.entry_reason)]]}/>
      {ev.score_components ? <KV items={Object.entries(ev.score_components).map(([k, v]) => [title(k), num(v, 1)] as [string, string])}/> : <div className="tm-muted">Score components not recorded for this engine.</div>}
    </Panel>
    <Panel title={`Recorded marks (${detail.mark_count})`}>
      {marks.length >= 2 ? <LineSeries label="Recorded marks over time" data={marks} series={[{ key: "price", name: "Mark", color: "var(--tm-chart-1)" }]} format={v => num(v)}/> : marks.length === 1 ? <div>One mark recorded: {num(marks[0].price)} at {marks[0].x} IST. No mark series to chart.</div> : <div className="tm-muted">No mark data recorded.</div>}
    </Panel>
    <Panel title="Orders (simulated)" flush><OrdersTable orders={detail.orders} label="Position orders"/></Panel>
    <Panel title="Fills (simulated)" flush>
      <DataTable label="Position fills" rows={detail.fills} rowKey={f => f.fill_id} empty={<div className="tm-state">No fills</div>} columns={[
        { key: "t", header: "Time", render: f => istDateTime(f.at) }, { key: "c", header: "Contract", render: f => <span className="tm-mono">{f.contract ?? "—"}</span> },
        { key: "s", header: "Side", render: f => `${f.side} · ${f.intent}` }, { key: "q", header: "Qty", align: "right", render: f => int(f.quantity) }, { key: "p", header: "Price", align: "right", render: f => num(f.price) },
      ]}/>
    </Panel>
    <Panel title="P&L calculation">
      <p className="tm-caption" style={{ marginTop: 0 }}>Formula: <span className="tm-mono">{calc.formula}</span> · quantity {int(calc.quantity)}. Computed by the backend; this page only displays it.</p>
      <KV items={[
        ["Recomputed gross (from fills)", <PnL key="a" value={calc.realized_gross}/>], ["Engine-recorded gross", <PnL key="b" value={calc.engine_recorded_gross} reason="not recorded"/>],
        ["Incurred charges", calc.charges.incurred ? rupees(calc.charges.incurred.total) : "—"], ["Realized net", <PnL key="c" value={calc.realized_net}/>],
        ["Engine-recorded net", <PnL key="d" value={calc.engine_recorded_net} reason="not recorded"/>],
      ]}/>
      <ChargesTable calc={calc}/>
    </Panel>
    <Panel title="Provenance">
      <KV items={[
        ["Journal table", <span key="t" className="tm-mono">{p.journal?.table ?? "—"}</span>], ["Journal id", <span key="i" className="tm-mono">{p.journal?.id ?? "—"}</span>],
        ["Sequences", p.journal ? `${p.journal.first_sequence ?? "—"} → ${p.journal.last_sequence ?? "—"}${p.journal.events != null ? ` (${p.journal.events} events)` : ""}` : "—"],
        ["Source", p.provenance?.source ?? "—"], ["P&L basis", title(p.provenance?.pnl_basis)], ["Position id", <span key="p" className="tm-mono">{p.position_id}</span>],
      ]}/>
    </Panel>
  </div>;
}

function PositionDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const detail = useResource(urls.position(id), parse.position, LIVE_INTERVAL);
  const p = detail.data?.position;
  return <DetailDrawer open title={p ? <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>{p.contract}</span> : "Position"} subtitle={p ? `${p.strategy_label} · ${p.status} · simulated paper position` : id} onClose={onClose}>
    <ResourceView resource={detail} label="Position detail">{d => <PositionDetailView detail={d}/>}</ResourceView>
  </DetailDrawer>;
}

/** Order id = "<position id>:<intent>" (backend convention); used only when a deep link carries no ?position=. */
export const positionIdOf = (orderId: string) => orderId.replace(/:(ENTRY|EXIT)$/, "");

function OrderDrawer({ orderId, positionId, onClose, onPosition }: { orderId: string; positionId: string; onClose: () => void; onPosition: () => void }) {
  const detail = useResource(urls.position(positionId), parse.position, LIVE_INTERVAL);
  return <DetailDrawer open title={<span className="tm-mono">{orderId}</span>} subtitle="Simulated paper order lifecycle" onClose={onClose}>
    <ResourceView resource={detail} label="Order lifecycle">{d => {
      const order = d.orders.find(o => o.order_id === orderId);
      const fills = d.fills.filter(f => f.order_id === orderId);
      if (!order) return <div className="tm-state" data-tone="warn" role="alert"><strong>Order not found on this position</strong></div>;
      return <div>
        <Panel title="Order">
          <KV items={[
            ["Internal order id", <span key="a" className="tm-mono">{order.order_id}</span>], ["Broker order id", <span key="b" className="tm-muted">{order.broker_order_id ?? SIMULATED}</span>],
            ["Status", <span key="c"><StatusBadge dimension="order" value={order.status}/> <small className="tm-muted">simulated</small></span>], ["Mode", order.mode],
            ["Contract", order.contract ?? "—"], ["Side · intent", `${order.side} · ${order.intent}`], ["Type", title(order.order_type)],
            ["Requested qty", int(order.requested_quantity)], ["Filled qty", int(order.filled_quantity)], ["Remaining qty", int(order.remaining_quantity)],
            ["Requested price", <Val key="d" reason="market-style simulated order">{order.requested_price == null ? null : num(order.requested_price)}</Val>], ["Avg fill price", <Val key="e" reason="not filled">{order.avg_fill_price == null ? null : num(order.avg_fill_price)}</Val>],
            ["Trigger price", <Val key="f">{order.trigger_price == null ? null : num(order.trigger_price)}</Val>], ["Submitted", istDateTime(order.submitted_at)], ["Last update", istDateTime(order.last_update_at)],
            ["Rejection reason", title(order.rejection_reason)],
          ]}/>
          <p className="tm-caption">Paper orders are reconstructed from journal events and filled against the next observed bid/ask. No broker order exists.</p>
        </Panel>
        <Panel title="Lifecycle fills" flush><DataTable label="Order fills" rows={fills} rowKey={f => f.fill_id} empty={<div className="tm-state">No fill: this order was not executed.</div>} columns={[
          { key: "t", header: "Time", render: f => istDateTime(f.at) }, { key: "q", header: "Qty", align: "right", render: f => int(f.quantity) }, { key: "p", header: "Price", align: "right", render: f => num(f.price) }, { key: "s", header: "Simulated", render: f => f.simulated ? "yes" : "no" },
        ]}/></Panel>
        <Panel title="Position"><KV items={[["Position", <button key="p" type="button" className="tm-link" onClick={onPosition}>{d.position.contract}</button>], ["Position status", <StatusBadge key="s" dimension="position" value={d.position.status}/>]]}/></Panel>
        <Panel title="Position timeline">{d.timeline.length ? <ol className="tm-timeline">{d.timeline.map((e, i) => <li key={i}><b>{title(e.type)}</b> <span className="tm-muted">#{e.sequence ?? "—"} · {istDateTime(e.at)}</span></li>)}</ol> : <div className="tm-muted">No journal events.</div>}</Panel>
      </div>;
    }}</ResourceView>
  </DetailDrawer>;
}

/** Mounted once in the layout: ?position=<id> opens the position drawer, ?order=<id>[&position=<id>] the order lifecycle drawer. */
export function DrawerHost() {
  const { get, set } = useUrlState();
  const orderId = get("order"), positionId = get("position");
  const close = () => set({ order: null, position: null });
  if (orderId) return <OrderDrawer orderId={orderId} positionId={positionId ?? positionIdOf(orderId)} onClose={close} onPosition={() => set({ order: null, position: positionId ?? positionIdOf(orderId) }, { push: true })}/>;
  if (positionId) return <PositionDrawer id={positionId} onClose={close}/>;
  return null;
}
export function useDrawerLinks() {
  const { set } = useUrlState();
  return { openPosition: (id: string) => set({ position: id, order: null }, { push: true }), openOrder: (o: Order) => set({ order: o.order_id, position: o.position_id }, { push: true }) };
}

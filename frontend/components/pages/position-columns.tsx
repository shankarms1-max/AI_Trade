"use client";
import { PnL, StatusBadge, FlagBadges, MarkBadge, StrategyChip, Val } from "@/components/terminal/primitives";
import { ExpandableLegs } from "@/components/terminal/legs";
import type { Column } from "@/components/terminal/data-table";
import { calendarDate, duration, int, istDateTime, num, title } from "@/lib/terminal-format";
import type { Position } from "@/types/portfolio";

export const isAttention = (p: Position) => p.flags.length > 0 || p.status === "UNRESOLVED" || p.status === "EXPIRED_UNPRICED";
export const rowClass = (p: Position) => isAttention(p) && (p.status === "OPEN" || p.status === "UNRESOLVED") ? "is-critical" : p.status === "OPEN" && p.mark.basis !== "EXECUTABLE_BID_ASK" ? "is-warn" : undefined;
/** The expiry has its own column: drop the redundant " · exp YYYY-MM-DD" suffix from the label (full text stays in the tooltip). */
export const shortContract = (contract: string) => contract.replace(/ · exp \d{4}-\d{2}-\d{2}$/, "");
export const legsExpander = (p: Position) => p.legs.length > 1 || p.structure === "CREDIT_SPREAD" ? <ExpandableLegs position={p}/> : null;

const level = (l: Position["stop"]) => l?.level == null ? null : <span title={l.rule ?? l.basis ?? undefined}>{num(l.level)}</span>;

export function MarkCell({ p }: { p: Position }) {
  const m = p.mark;
  return <span style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-end", gap: 1 }}>
    <span><Val reason={m.reason ?? "no executable quote"}>{m.price == null ? null : num(m.price)}</Val> <MarkBadge basis={m.basis}/></span>
    {m.price == null && m.last_known_price != null && <small className="tm-muted" title="Last known price: not executable, excluded from P&L">last known {num(m.last_known_price)}</small>}
    <small className="tm-muted" title={m.at ?? undefined}>{m.at ? `quote ${istDateTime(m.at)}${m.age_seconds != null ? ` · ${duration(m.age_seconds)} old` : ""}` : "no quote time"}{m.reason ? ` · ${title(m.reason)}` : ""}</small>
  </span>;
}

type Opts = { onOpen: (p: Position) => void; compact?: boolean; showVersion?: boolean };
const strategyCol = (o: Opts): Column<Position> => ({ key: "strategy", header: "Strategy", sticky: true, render: p => <StrategyChip label={p.strategy_label} family={p.family} version={o.compact ? null : undefined}/> });
const contractCol = (o: Opts): Column<Position> => ({ key: "contract", header: "Contract", render: p => <button type="button" className="tm-link" title={p.contract} onClick={() => o.onOpen(p)} aria-label={`Open position ${p.contract}`}>{shortContract(p.contract)}</button> });
const statusCol: Column<Position> = { key: "status", header: "Status", render: p => <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}><StatusBadge dimension="position" value={p.status} label="Position"/><FlagBadges flags={p.flags}/></span> };

/** Open / unresolved positions: current executable mark + basis, stops, unrealized and estimated net liquidation. */
export function openColumns(o: Opts): Array<Column<Position>> {
  const cols: Array<Column<Position>> = [
    strategyCol(o),
    ...(o.showVersion ? [{ key: "version", header: "Version", render: (p: Position) => p.strategy_version }] : []),
    { key: "instrument", header: "Instrument", render: p => p.instrument },
    contractCol(o),
    { key: "expiry", header: "Expiry", render: p => calendarDate(p.expiry) },
    { key: "direction", header: "Dir", render: p => p.direction ?? "—" },
    { key: "mode", header: "Mode", render: p => <StatusBadge value={p.mode} tone="info"/> },
    { key: "lots", header: "Lots", align: "right", render: p => int(p.lots) },
    { key: "qty", header: "Qty", align: "right", render: p => int(p.quantity) },
    { key: "entry_at", header: "Entry time", render: p => istDateTime(p.entry_at) },
    { key: "entry", header: "Avg entry", align: "right", title: "Net credit per unit (spreads) or entry price (futures)", render: p => num(p.entry_price) },
    { key: "mark", header: "Executable mark", align: "right", render: p => <MarkCell p={p}/> },
    { key: "ltp", header: "Last traded", align: "right", title: "No last-traded price is recorded per position", render: () => <span className="tm-dash" title="n/a: no last-traded price recorded">n/a</span> },
    { key: "stop", header: "Stop", align: "right", render: p => <Val>{level(p.stop)}</Val> },
    { key: "trail", header: "Trailing", align: "right", render: p => <Val>{level(p.trailing_stop)}</Val> },
    { key: "target", header: "Target", align: "right", render: p => <Val>{level(p.target)}</Val> },
    { key: "upnl", header: "Unrealized gross", align: "right", render: p => <PnL value={p.unrealized_gross} reason={p.mark.reason ?? (p.mark.basis !== "EXECUTABLE_BID_ASK" ? "no executable mark" : null)}/> },
    { key: "netliq", header: "Est. net liq.", align: "right", title: "Estimated net liquidation value after estimated exit charges", render: p => <PnL value={p.est_net_liquidation} reason={p.mark.reason ?? "no executable mark"}/> },
    { key: "hold", header: "Held", align: "right", render: p => duration(p.holding_seconds) },
    statusCol,
  ];
  if (!o.compact) return cols;
  const keep = new Set(["strategy", "contract", "direction", "qty", "entry", "mark", "stop", "upnl", "netliq", "hold", "status"]);
  return cols.filter(c => keep.has(c.key));
}

/** Closed positions: exit, gross, charges, net and reasons. */
export function closedColumns(o: Opts): Array<Column<Position>> {
  return [
    strategyCol(o),
    { key: "version", header: "Version", render: p => p.strategy_version },
    { key: "instrument", header: "Instrument", render: p => p.instrument },
    contractCol(o),
    { key: "expiry", header: "Expiry", render: p => calendarDate(p.expiry) },
    { key: "direction", header: "Dir", render: p => p.direction ?? "—" },
    { key: "mode", header: "Mode", render: p => <StatusBadge value={p.mode} tone="info"/> },
    { key: "lots", header: "Lots", align: "right", render: p => int(p.lots) },
    { key: "qty", header: "Qty", align: "right", render: p => int(p.quantity) },
    { key: "entry_at", header: "Entry time", render: p => istDateTime(p.entry_at) },
    { key: "entry", header: "Avg entry", align: "right", render: p => num(p.entry_price) },
    { key: "exit_at", header: "Exit time", render: p => istDateTime(p.exit_at) },
    { key: "exit", header: "Exit price", align: "right", render: p => <Val reason="no exit price recorded">{p.exit_price == null ? null : num(p.exit_price)}</Val> },
    { key: "gross", header: "Gross", align: "right", render: p => <PnL value={p.realized_gross} reason={p.exit_price == null ? "no exit price" : null}/> },
    { key: "charges", header: "Charges", align: "right", render: p => <PnL value={p.charges.incurred?.total ?? null} signed={false}/> },
    { key: "net", header: "Net", align: "right", render: p => <PnL value={p.realized_net} reason={p.exit_price == null ? "no exit price" : null}/> },
    { key: "hold", header: "Held", align: "right", render: p => duration(p.holding_seconds) },
    { key: "entry_reason", header: "Entry reason", render: p => title(p.entry_reason) },
    { key: "exit_reason", header: "Exit reason", render: p => title(p.exit_reason) },
    statusCol,
  ];
}
/** "All" tab mixes states: show both live and realized figures. */
export function allColumns(o: Opts): Array<Column<Position>> {
  const open = openColumns(o).filter(c => !["stop", "trail", "target", "hold", "ltp", "status"].includes(c.key));
  const closed = closedColumns(o).filter(c => ["exit_at", "exit", "gross", "charges", "net", "hold", "exit_reason", "status"].includes(c.key));
  return [...open, ...closed];
}

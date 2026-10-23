import type { Phase15PaperTrade } from "@/types";
import type { PaperRecord } from "@/types/commodity-paper";
export interface PortfolioTrade {
  id: string; source: string; strategy: string; state: string;
  timestamp: string | null; exitTimestamp: string | null; contract: string | null;
  realized: number | null; unrealized: number | null; reason: string | null;
}
const numeric = (value: number | null | undefined) => typeof value === "number" && Number.isFinite(value) ? value : null;
export function awareTimestamp(value: string | null | undefined): string | null {
  return value && /(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value)) ? value : null;
}
export function niftyTrade(trade: Phase15PaperTrade): PortfolioTrade {
  return { id: `NIFTY:${trade.strategy_id}:${trade.trade_id}`, source: "NIFTY", strategy: trade.display_name,
    state: trade.state, timestamp: awareTimestamp(trade.entry_timestamp),
    exitTimestamp: awareTimestamp(trade.exit_timestamp),
    contract: trade.short_strike == null || trade.hedge_strike == null ? null : `${trade.short_strike} / ${trade.hedge_strike}`,
    realized: trade.state === "CLOSED" ? numeric(trade.realized_gross_pnl) : null,
    unrealized: trade.state === "OPEN" && trade.mark_available ? numeric(trade.current_unrealized_gross_pnl) : null,
    reason: trade.terminal_status ?? trade.exit_reason ?? trade.rejection_reason ?? trade.unresolved_reason };
}
export function commodityTrade(source: string, trade: PaperRecord, index: number): PortfolioTrade {
  return { id: `${source}:${trade.id ?? index}`, source, strategy: trade.strategy ?? "ACTS V2",
    state: trade.state ?? "UNAVAILABLE", timestamp: awareTimestamp(trade.entry_timestamp),
    exitTimestamp: awareTimestamp(trade.exit_timestamp), contract: trade.candidate?.short_leg?.trading_symbol ?? null,
    realized: trade.state === "CLOSED" ? numeric(trade.gross_realized_pnl) : null,
    unrealized: trade.state === "OPEN" ? numeric(trade.unrealized_gross) : null,
    reason: trade.exit_reason ?? trade.rejection_reason ?? trade.unresolved_reason ?? trade.reason ?? null };
}
export function realizedPath(rows: PortfolioTrade[]) {
  const closed = rows.filter(row => row.state === "CLOSED");
  const priced = closed.filter(row => row.realized != null && row.exitTimestamp != null);
  const excluded = closed.length - priced.length;
  let cumulative = 0;
  const points = [...priced].sort((a,b) => Date.parse(a.exitTimestamp!) - Date.parse(b.exitTimestamp!) || a.id.localeCompare(b.id)).map(row => {
    cumulative += row.realized!;
    return { id: row.id, timestamp: row.exitTimestamp!, gross: cumulative, source: row.source };
  });
  if (!points.every(row => Number.isFinite(row.gross))) return { status: "INCOMPLETE" as const, points: [], excluded: closed.length };
  return { status: points.length ? excluded ? "PARTIAL" as const : "AVAILABLE" as const : excluded ? "INCOMPLETE" as const : "EMPTY" as const, points, excluded };
}

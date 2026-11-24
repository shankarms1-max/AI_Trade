"use client";
import { StatusBadge, type Dimension } from "@/components/terminal/primitives";
import { title } from "@/lib/terminal-format";
import type { Dict, StrategyCard } from "@/types/portfolio";

export const str = (d: Dict | null | undefined, key: string): string | null => { const v = d?.[key]; return typeof v === "string" ? v : null; };
export const numberOf = (d: Dict | null | undefined, key: string): number | null => { const v = d?.[key]; return typeof v === "number" && Number.isFinite(v) ? v : null; };
export const boolOf = (d: Dict | null | undefined, key: string): boolean | null => { const v = d?.[key]; return typeof v === "boolean" ? v : null; };
export const listOf = (d: Dict | null | undefined, key: string): string[] => { const v = d?.[key]; return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []; };

export type DimView = { dimension: Dimension; label: string; value: string; detail: string | null };
/**
 * The six independent status dimensions of a strategy. They are never merged into one status: enablement (is paper
 * execution on), market session, feed freshness, feature readiness/warm-up, signal state, position state.
 */
export function dimensionViews(card: StrategyCard): DimView[] {
  const d = card.dimensions;
  const feedValue = d.feed == null ? "UNAVAILABLE" : boolOf(d.feed, "stale") === true ? "STALE" : boolOf(d.feed, "stale") === false ? "FRESH" : (str(d.feed, "state") ?? "UNAVAILABLE");
  const ready = boolOf(d.readiness, "ready");
  const bars = numberOf(d.readiness, "bars_available"), need = numberOf(d.readiness, "bars_required");
  const open = numberOf(d.position, "open") ?? 0;
  return [
    { dimension: "enablement", label: "Execution", value: str(d.enablement, "paper_execution") ?? "UNKNOWN", detail: str(d.enablement, "live_execution") },
    { dimension: "session", label: "Session", value: str(d.session, "status") ?? "UNKNOWN", detail: str(d.session, "window") },
    { dimension: "feed", label: "Feed", value: feedValue, detail: numberOf(d.feed, "age_seconds") != null ? `quote age ${Math.round(numberOf(d.feed, "age_seconds")!)}s` : str(d.feed, "last_observation") },
    { dimension: "readiness", label: "Warm-up", value: d.readiness == null ? "NOT_APPLICABLE" : ready ? "READY" : "WARMING_UP", detail: bars != null && need != null ? `${bars}/${need} bars` : null },
    { dimension: "signal", label: "Signal", value: str(d.signal, "status") ?? "UNKNOWN", detail: str(d.signal, "direction") },
    { dimension: "position", label: "Position", value: card.unresolved > 0 ? "UNRESOLVED" : open > 0 ? "OPEN" : "FLAT", detail: open > 0 ? `${open} open` : null },
  ];
}
export function DimensionBadges({ card }: { card: StrategyCard }) {
  return <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
    {dimensionViews(card).map(v => <StatusBadge key={v.dimension} dimension={v.dimension} label={v.label} value={v.value} title={`${v.label}: ${title(v.value)}${v.detail ? ` (${v.detail})` : ""}`}/>)}
  </span>;
}
export const latestBlocker = (card: StrategyCard): string | null => {
  const s = card.dimensions.signal;
  return str(s, "primary_blocker") ?? listOf(s, "vetoes")[0] ?? listOf(s, "blockers")[0] ?? card.state_reason;
};

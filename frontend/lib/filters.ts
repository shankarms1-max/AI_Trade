import { istDateKey } from "@/lib/format";

/**
 * Global terminal filters. They live in the URL query (so deep links and reloads keep them) and are mirrored by a React
 * context. Date arithmetic here is calendar maths on IST session dates; it never touches money.
 */
export const RANGES = ["today", "prev", "7d", "30d", "mtd", "custom", "all"] as const;
export type RangeKey = typeof RANGES[number];
export const RANGE_LABEL: Record<RangeKey, string> = { today: "Today", prev: "Previous session", "7d": "7D", "30d": "30D", mtd: "MTD", custom: "Custom", all: "All" };
export const MODES = ["PAPER", "LIVE"] as const;
export type Mode = typeof MODES[number];
/** Query keys that belong to the global filter set (carried across navigation). */
export const GLOBAL_KEYS = ["mode", "engine", "instrument", "version", "range", "from", "to"] as const;

export type Filters = { mode: Mode; engine: string | null; instrument: string | null; version: string | null; range: RangeKey | null; from: string | null; to: string | null };
type Params = { get(key: string): string | null };

const DATE = /^\d{4}-\d{2}-\d{2}$/;
export function parseFilters(params: Params): Filters {
  const range = params.get("range");
  const from = params.get("from"), to = params.get("to");
  const clean = (key: string) => params.get(key) || null;
  return {
    mode: params.get("mode")?.toUpperCase() === "LIVE" ? "LIVE" : "PAPER",
    engine: clean("engine"), instrument: clean("instrument"), version: clean("version"),
    range: (RANGES as readonly string[]).includes(range ?? "") ? range as RangeKey : (from || to) ? "custom" : null,
    from: from && DATE.test(from) ? from : null, to: to && DATE.test(to) ? to : null,
  };
}

/** Today's IST calendar date (YYYY-MM-DD). */
export const istToday = (now: Date = new Date()) => istDateKey(now.toISOString()) as string;
const shift = (key: string, days: number) => { const d = new Date(`${key}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };
const weekday = (key: string) => new Date(`${key}T00:00:00Z`).getUTCDay();
/** Previous trading weekday (Mon-Fri). Exchange holidays are not known to the browser. */
export function previousSession(today: string): string {
  let day = shift(today, -1);
  while (weekday(day) === 0 || weekday(day) === 6) day = shift(day, -1);
  return day;
}
export type DateWindow = { from: string | null; to: string | null };
export function resolveRange(range: RangeKey, from: string | null, to: string | null, now: Date = new Date()): DateWindow {
  const today = istToday(now);
  switch (range) {
    case "today": return { from: today, to: today };
    case "prev": { const p = previousSession(today); return { from: p, to: p }; }
    case "7d": return { from: shift(today, -6), to: today };
    case "30d": return { from: shift(today, -29), to: today };
    case "mtd": return { from: `${today.slice(0, 8)}01`, to: today };
    case "custom": return { from, to };
    default: return { from: null, to: null };
  }
}
export const effectiveRange = (filters: Filters, fallback: RangeKey): RangeKey => filters.range ?? fallback;
export const windowOf = (filters: Filters, fallback: RangeKey, now?: Date): DateWindow => resolveRange(effectiveRange(filters, fallback), filters.from, filters.to, now);

export type ParamValue = string | number | boolean | null | undefined;
/** Stable (sorted) query string: identical inputs always give the identical URL, so the poll store dedupes by URL. */
export function queryString(params: Record<string, ParamValue>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "").sort(([a], [b]) => a.localeCompare(b));
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)]))}` : "";
}

/** Filter params understood by every /api/portfolio list endpoint. Mode is always sent: LIVE and PAPER are never mixed. */
export function filterParams(filters: Filters, fallbackRange: RangeKey, now?: Date): Record<string, ParamValue> {
  const { from, to } = windowOf(filters, fallbackRange, now);
  return { mode: filters.mode, engine: filters.engine, instrument: filters.instrument, version: filters.version, from, to };
}
/** Same filters, without date (open positions, risk, strategies are as-of-now). */
export const entityParams = (filters: Filters) => ({ mode: filters.mode, engine: filters.engine, instrument: filters.instrument, version: filters.version });

/** Carry the global filter keys of `search` onto an internal href. */
export function withGlobalFilters(href: string, search: string): string {
  const source = new URLSearchParams(search);
  const carried = new URLSearchParams();
  for (const key of GLOBAL_KEYS) { const v = source.get(key); if (v) carried.set(key, v); }
  const [path, existing = ""] = href.split("?");
  const merged = new URLSearchParams(existing);
  carried.forEach((v, k) => { if (!merged.has(k)) merged.set(k, v); });
  const text = merged.toString();
  return text ? `${path}?${text}` : path;
}

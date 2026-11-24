import { DASH, parseTimestamp } from "@/lib/format";

/** Display-only formatting for the terminal. Nothing here computes money; null/undefined/non-finite always render as an em dash. */
const IST = "Asia/Kolkata";
const finiteOrNull = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);
const nf = (digits: number) => new Intl.NumberFormat("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const formats = new Map<number, Intl.NumberFormat>();
const fmt = (digits: number) => { let f = formats.get(digits); if (!f) { f = nf(digits); formats.set(digits, f); } return f; };

export const MINUS = "−";
/** Plain decimal (prices, scores). */
export const num = (v: number | null | undefined, digits = 2) => finiteOrNull(v) ? fmt(digits).format(v).replace("-", MINUS) : DASH;
export const int = (v: number | null | undefined) => num(v, 0);
/** Currency amount without sign prefix for positives: ₹1,234.50 / −₹1,234.50. */
export const rupees = (v: number | null | undefined, digits = 2) => !finiteOrNull(v) ? DASH : `${v < 0 ? MINUS : ""}₹${fmt(digits).format(Math.abs(v))}`;
/** Signed currency: +₹1,234.50 / −₹1,234.50 / ₹0.00. */
export const signedRupees = (v: number | null | undefined, digits = 2) => !finiteOrNull(v) ? DASH : `${v > 0 ? "+" : v < 0 ? MINUS : ""}₹${fmt(digits).format(Math.abs(v))}`;
/** Fraction (0.75) as percent. */
export const pctFraction = (v: number | null | undefined, digits = 1) => finiteOrNull(v) ? `${fmt(digits).format(v * 100)}%` : DASH;
/** Value that is already a percent (72.34). */
export const pct = (v: number | null | undefined, digits = 1) => finiteOrNull(v) ? `${fmt(digits).format(v)}%` : DASH;
export const ratio = (v: number | null | undefined, digits = 2) => finiteOrNull(v) ? fmt(digits).format(v) : DASH;

export function duration(seconds: number | null | undefined): string {
  if (!finiteOrNull(seconds) || seconds < 0) return DASH;
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

const part = (ms: number, options: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-GB", { timeZone: IST, ...options }).format(new Date(ms));
/** "24 Nov 14:55:00" in Asia/Kolkata. Naive timestamps are UTC (lib/format.ts policy). */
export function istDateTime(value: string | null | undefined): string {
  const ms = parseTimestamp(value);
  return ms == null ? DASH : `${part(ms, { day: "2-digit", month: "short" })} ${part(ms, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}`;
}
export function istClock(value: string | null | undefined): string {
  const ms = parseTimestamp(value);
  return ms == null ? DASH : part(ms, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
/** Date-only values (YYYY-MM-DD, e.g. expiry) are calendar dates, not instants: no timezone shift. */
export function calendarDate(value: string | null | undefined): string {
  if (!value) return DASH;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!m) return istDateTime(value);
  return new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "2-digit", month: "short", year: "numeric" }).format(new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])));
}
/** "since" values are either an instant or a calendar date. */
export const sinceText = (value: string | null | undefined) => !value ? DASH : /^\d{4}-\d{2}-\d{2}$/.test(value) ? calendarDate(value) : istDateTime(value);
export function ageText(value: string | null | undefined, now = Date.now()): string {
  const ms = parseTimestamp(value);
  if (ms == null) return DASH;
  return `${duration(Math.max(0, (now - ms) / 1000))} ago`;
}
export const title = (value: string | null | undefined) => value ? value.replaceAll("_", " ") : DASH;
/** Sign class used by PnL/Delta cells. Unknown is neutral, never "zero". */
export const signOf = (v: number | null | undefined): "pos" | "neg" | "zero" | "na" => !finiteOrNull(v) ? "na" : v > 0 ? "pos" : v < 0 ? "neg" : "zero";

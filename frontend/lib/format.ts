const IST = "Asia/Kolkata";
export const DASH = "—";

/**
 * Timestamp policy: every value is displayed in Asia/Kolkata.
 * An ISO date-time WITHOUT an offset ("2026-11-24T04:00:00") is treated as UTC, never as
 * browser-local time (which is what `new Date()` would do). Values with `Z`/`±hh:mm` keep their offset.
 * Date-only values ("2026-11-24") are already parsed as UTC midnight by the platform.
 */
const NAIVE_DATE_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;
export function parseTimestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const text = value.trim();
  const ms = Date.parse(NAIVE_DATE_TIME.test(text) ? `${text.replace(" ", "T")}Z` : text);
  return Number.isFinite(ms) ? ms : null;
}
const istFormat = (value: string | null | undefined, options: Intl.DateTimeFormatOptions, locale = "en-GB") => {
  const ms = parseTimestamp(value);
  return ms == null ? null : new Intl.DateTimeFormat(locale, { timeZone: IST, ...options }).format(new Date(ms));
};

export const number = (value: number | null | undefined, digits = 2) => value == null ? "N/A" : new Intl.NumberFormat("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
export const integer = (value: number | null | undefined) => value == null ? "N/A" : new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(value);
export const money = (value: number | null | undefined) => value == null ? "N/A" : `${value < 0 ? "−" : ""}₹${number(Math.abs(value))}`;
export const percent = (value: number | null | undefined) => value == null ? "N/A" : `${number(value, 1)}%`;
export const timeIST = (value: string | null | undefined) => {
  const text = istFormat(value, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  return text ? `${text} IST` : "N/A";
};
export const dateIST = (value: string | null | undefined) => istFormat(value, { day: "2-digit", month: "short", year: "numeric" }) ?? "N/A";
/** IST calendar date key (YYYY-MM-DD) used to group sessions; null when the timestamp is unknown. */
export const istDateKey = (value: string | null | undefined) => istFormat(value, { year: "numeric", month: "2-digit", day: "2-digit" }, "en-CA");
export const ageSeconds = (value: string, now = Date.now()) => Math.max(0, Math.floor((now - (parseTimestamp(value) ?? now)) / 1000));
export const ageLabel = (seconds: number) => seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${Math.floor(seconds / 3600)}h`;
export const pretty = (value: string) => value.replaceAll("_", " ");

/** Unknown is never zero: null/undefined/non-finite render as an em dash. */
export function dash<T>(value: T | null | undefined, render: (value: T) => string = String): string {
  return value == null || (typeof value === "number" && !Number.isFinite(value)) ? DASH : render(value);
}
export const stampIST = (value: string | null | undefined) => parseTimestamp(value) == null ? DASH : `${dateIST(value)} · ${timeIST(value)}`;

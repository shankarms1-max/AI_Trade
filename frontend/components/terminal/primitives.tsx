"use client";
import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { DASH } from "@/lib/format";
import { signedRupees, rupees, signOf, title } from "@/lib/terminal-format";

export type Tone = "pos" | "neg" | "warn" | "info" | "na" | "neutral" | "crit";

/* ------------------------------------------------------------------ PnL */
/**
 * Signed, coloured, tabular money cell. The value always comes from the API. Unknown (null) renders an em dash,
 * never zero, with the reason (when the API gives one) as visible small text and as the tooltip.
 */
export function PnL({ value, reason, signed = true, digits = 2, className = "" }: { value: number | null | undefined; reason?: string | null; signed?: boolean; digits?: number; className?: string }) {
  const sign = signOf(value);
  if (sign === "na") {
    return <span className={`tm-pnl pnl-na ${className}`} title={reason ? `Unavailable: ${reason}` : "Unavailable"} aria-label={reason ? `unavailable, ${title(reason)}` : "unavailable"}>{DASH}{reason ? <small>{title(reason)}</small> : null}</span>;
  }
  return <span className={`tm-pnl pnl-${sign} ${className}`}>{signed ? signedRupees(value, digits) : rupees(value, digits)}</span>;
}
/** Plain number / text value with unknown rendered as an em dash (and optional reason tooltip). */
export function Val({ children, reason }: { children: ReactNode | null | undefined; reason?: string | null }) {
  if (children == null || children === "" || children === DASH) return <span className="tm-dash" title={reason ? `Unavailable: ${reason}` : "Unavailable"}>{DASH}</span>;
  return <>{children}</>;
}
export function Unavailable({ reason, label = "Unavailable" }: { reason?: string | null; label?: string }) {
  return <span><span className="tm-badge" data-tone="na">{label}</span>{reason ? <small className="tm-muted"> {reason}</small> : null}</span>;
}

/* ------------------------------------------------------------------ status badges */
export const DIMENSIONS = ["enablement", "session", "feed", "readiness", "signal", "position", "state", "mark", "order", "market"] as const;
export type Dimension = typeof DIMENSIONS[number] | "none";
const MAP: Record<string, Record<string, Tone>> = {
  enablement: { ENABLED: "pos", PAPER_ENABLED: "pos", OBSERVED_ACTIVE: "info", COLLECT_ONLY: "warn", DISABLED: "warn", UNKNOWN: "na", NO_RECORDS: "na", NOT_CONFIGURED: "na" },
  session: { OPEN: "pos", PRE_OPEN: "info", CLOSED: "na", CLOSED_OR_UNKNOWN: "na", HOLIDAY: "na", UNKNOWN: "na" },
  feed: { FRESH: "pos", RUNNING: "pos", STALE: "warn", DATA_STALE: "warn", MARKET_CLOSED: "na", UNAVAILABLE: "na", DATA_UNAVAILABLE: "warn", UNKNOWN: "na" },
  readiness: { READY: "pos", WARMING_UP: "warn", INSUFFICIENT_HISTORY: "warn", UNKNOWN: "na", NOT_APPLICABLE: "na" },
  signal: { NO_SETUP: "neutral", NO_ENTRY: "neutral", WATCH: "info", ENTER: "info", ENTRY: "info", BLOCKED: "warn", ENTRY_REJECTED: "warn", UNKNOWN: "na" },
  position: { FLAT: "neutral", OPEN: "info", UNRESOLVED: "crit", CLOSED: "neutral", PENDING: "warn", REJECTED: "warn", EXPIRED_UNPRICED: "neg" },
  state: { RUNNING: "pos", IDLE: "neutral", MARKET_CLOSED: "na", DATA_STALE: "warn", DATA_UNAVAILABLE: "warn", ERROR: "neg", DISABLED: "neutral", NOT_CONFIGURED: "na", UNKNOWN: "na" },
  mark: { EXECUTABLE: "pos", EXECUTABLE_BID_ASK: "pos", STALE: "warn", UNAVAILABLE: "neg" },
  order: { FILLED: "pos", REJECTED: "neg", EXPIRED: "warn", PENDING: "info", CANCELLED: "na", PARTIAL: "warn" },
  market: { OPEN: "pos", PRE_OPEN: "info", CLOSED: "na", HOLIDAY: "na" },
};
const FLAG_TONE = /EXPIRED_NOT_SETTLED|PAST_FORCED_EXIT|UNRESOLVED|CRITICAL|ENGINE_ERROR|ERROR/;
export function toneFor(dimension: Dimension, value: string): Tone {
  if (FLAG_TONE.test(value) && dimension !== "enablement") return "crit";
  return MAP[dimension]?.[value] ?? (/^(OK|HEALTHY|AVAILABLE|CONNECTED)/.test(value) ? "pos" : /(STALE|WARN|PARTIAL|UNVERIFIED|APPROX)/.test(value) ? "warn" : /(ERROR|FAIL|REJECT|DISCONNECTED)/.test(value) ? "neg" : /(NOT_CONFIGURED|UNAVAILABLE|UNKNOWN|NOT_NORMALISED|NA|NONE)/.test(value) ? "na" : "neutral");
}
/** One status dimension per badge: the small prefix names the dimension so different meanings never share one ambiguous chip. */
export function StatusBadge({ value, dimension = "none", label, tone, title: tooltip, dot = false }: { value: string | null | undefined; dimension?: Dimension; label?: string; tone?: Tone; title?: string; dot?: boolean }) {
  const text = value ?? "UNKNOWN";
  const resolved = tone ?? toneFor(dimension, text);
  return <span className="tm-badge" data-tone={resolved === "neutral" ? undefined : resolved} data-dimension={dimension} title={tooltip ?? (label ? `${label}: ${title(text)}` : undefined)}>
    {dot && <i aria-hidden="true"/>}{label && <span className="tm-dim">{label}</span>}{title(text)}
  </span>;
}
export const MarkBadge = ({ basis }: { basis: string }) => <StatusBadge dimension="mark" value={basis === "EXECUTABLE_BID_ASK" ? "EXECUTABLE" : basis}/>;
export function FlagBadges({ flags }: { flags: string[] }) {
  return <>{flags.map(f => <StatusBadge key={f} value={f} tone="crit" dot/>)}</>;
}

const FAMILY_COLOR: Record<string, string> = { ACTS: "var(--tm-chart-1)", MCX_FUTURES: "var(--tm-chart-2)", AST: "#a78bfa", COMMODITY: "var(--tm-muted)" };
/** Strategy label with a family colour bar (ACTS blue, MCX futures amber, AST violet, legacy grey). */
export function StrategyChip({ label, family, version }: { label: string; family?: string | null; version?: string | null }) {
  return <span className="tm-chip" style={{ "--chip": FAMILY_COLOR[family ?? ""] ?? "var(--tm-muted)" } as React.CSSProperties} title={version ? `${label} · ${version}` : label}>{label}{version ? <small>{version}</small> : null}</span>;
}

/* ------------------------------------------------------------------ tiles / panels */
export function MetricTile({ label, value, note, primary = false, warnNote = false, wide = false }: { label: string; value: ReactNode; note?: ReactNode; primary?: boolean; warnNote?: boolean; wide?: boolean }) {
  return <div className={`tm-tile${primary ? " primary" : ""}${wide ? " wide" : ""}`} role="group" aria-label={label}>
    <div className="tm-tile-label">{label}</div>
    <div className="tm-tile-value">{value}</div>
    {note != null && note !== false && <div className={`tm-tile-note${warnNote ? " warn" : ""}`}>{note}</div>}
  </div>;
}
export const Tiles = ({ children, lead = false }: { children: ReactNode; lead?: boolean }) => <div className={`tm-tiles${lead ? " lead" : ""}`}>{children}</div>;
export function Panel({ title: heading, actions, children, flush = false, id }: { title: string; actions?: ReactNode; children: ReactNode; flush?: boolean; id?: string }) {
  return <section className="tm-panel-box" aria-label={heading} id={id}><header><h2>{heading}</h2><span className="tm-spacer"/>{actions}</header><div className={`tm-panel-body${flush ? " flush" : ""}`}>{children}</div></section>;
}
export function PageTitle({ title: heading, note, actions }: { title: string; note?: ReactNode; actions?: ReactNode }) {
  return <div className="tm-page-title"><h1>{heading}</h1>{note ? <p>{note}</p> : null}<span className="tm-spacer"/>{actions}</div>;
}
export function KV({ items }: { items: Array<[string, ReactNode]> }) {
  return <dl className="tm-kv">{items.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}
export const Mono = ({ children, title: t }: { children: ReactNode; title?: string }) => <span className="tm-mono" title={t}>{children}</span>;

/* ------------------------------------------------------------------ tabs */
export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: Array<{ id: T; label: string; count?: number | null; tone?: "neg" }>; value: T; onChange: (id: T) => void; label: string }) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const key = (event: KeyboardEvent, index: number) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = (index + step + tabs.length) % tabs.length;
    refs.current[next]?.focus();
    onChange(tabs[next].id);
  };
  return <div className="tm-tabs" role="tablist" aria-label={label}>
    {tabs.map((t, i) => <button key={t.id} ref={el => { refs.current[i] = el; }} type="button" role="tab" id={`tab-${t.id}`} className="tm-tab" aria-selected={value === t.id} aria-controls={`panel-${t.id}`} tabIndex={value === t.id ? 0 : -1} onClick={() => onChange(t.id)} onKeyDown={e => key(e, i)}>
      {t.label}{t.count != null && <span className="tm-count" data-tone={t.tone}>{t.count}</span>}
    </button>)}
  </div>;
}
export const TabPanel = ({ id, children }: { id: string; children: ReactNode }) => <div role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`}>{children}</div>;

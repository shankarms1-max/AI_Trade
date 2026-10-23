import type { ReactNode } from "react";
import { Icon } from "@/components/icons";
export function Panel({ title, action, children, className = "" }: { title: string; action?: ReactNode; children: ReactNode; className?: string }) { return <section className={`panel ${className}`}><header className="panel-head"><h2 className="panel-title">{title}</h2>{action}</header><div className="panel-body">{children}</div></section>; }
export function statusTone(value: string): string {
  if (/^(ENTRY_REJECTED|BLOCKED)$/.test(value)) return "warn";
  if (/^(WATCH|ENTER|PENDING_PAPER_ENTRY)$/.test(value)) return "accent";
  if (/^(HEALTHY|FRESH|AVAILABLE|READY|APPROVED|SUCCESS|FILLED|ENABLED|RUNNING|CONFIRMED|VALID|ACTIVE_PAPER_RESEARCH)$/.test(value)) return "good";
  if (/ERROR|FAILED|UNHEALTHY|REJECTED|KILL_SWITCH|UNRESOLVED/.test(value)) return "bad";
  if (/STALE|WARMING|PENDING|DEGRADED|UNPRICED|BLOCKED|CUTOFF/.test(value)) return "warn";
  if (/PAPER|GROSS|BULL|BEAR|OPEN/.test(value)) return "accent";
  return "neutral";
}
export function Badge({ value }: { value: string }) { return <span className={`badge ${value.replace(/[^A-Za-z0-9_-]/g, "-")}`} data-tone={statusTone(value)}><i aria-hidden="true"/>{value.replaceAll("_", " ")}</span>; }
export function Metric({ label, value, detail, tone = "" }: { label: string; value: ReactNode; detail?: ReactNode; tone?: string }) { return <div className="metric"><div className="metric-label">{label}</div><div className={`metric-value ${tone}`}>{value}</div>{detail != null && <div className="metric-detail">{detail}</div>}</div>; }
export function Empty({ children }: { children: ReactNode }) { return <div className="empty"><span className="empty-symbol" aria-hidden="true"><Icon name="layers" size={22}/></span><span>{children}</span></div>; }
export function SectionError({ children = "SECTION DATA UNAVAILABLE" }: { children?: ReactNode }) { return <div className="section-error">{children}</div>; }
export function PageHeading({ eyebrow = "Research workspace", title, description, action }: { eyebrow?: string; title: string; description: string; action?: ReactNode }) {
  return <div className="page-heading"><div><p className="eyebrow"><span/>{eyebrow}</p><h1>{title}</h1><p className="page-description">{description}</p></div>{action && <div className="page-heading-action">{action}</div>}</div>;
}

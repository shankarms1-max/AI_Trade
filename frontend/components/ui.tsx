import type { ReactNode } from "react";
export function Panel({ title, action, children, className = "" }: { title: string; action?: ReactNode; children: ReactNode; className?: string }) { return <section className={`panel ${className}`}><header className="panel-head"><h2 className="panel-title">{title}</h2>{action}</header><div className="panel-body">{children}</div></section>; }
export function Badge({ value }: { value: string }) { return <span className={`badge ${value}`}>{value.replaceAll("_", " ")}</span>; }
export function Metric({ label, value, detail, tone = "" }: { label: string; value: ReactNode; detail?: ReactNode; tone?: string }) { return <div className="metric"><div className="metric-label">{label}</div><div className={`metric-value ${tone}`}>{value}</div>{detail != null && <div className="metric-detail">{detail}</div>}</div>; }
export function Empty({ children }: { children: ReactNode }) { return <div className="empty">{children}</div>; }
export function SectionError({ children = "SECTION DATA UNAVAILABLE" }: { children?: ReactNode }) { return <div className="section-error">{children}</div>; }

"use client";
import type { ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, ComposedChart, Line, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis, Cell } from "recharts";
import { istClock, signedRupees } from "@/lib/terminal-format";
import { parseTimestamp } from "@/lib/format";
import type { Equity } from "@/types/portfolio";

type Row = { name?: string | number; value?: number | string | null; color?: string; dataKey?: string | number };
/** Shared tooltip: label + one row per series; unknown values render an em dash. */
export function ChartTooltip({ active, payload, label, format = (v: number) => signedRupees(v), labelFormat }: { active?: boolean; payload?: Row[]; label?: string | number; format?: (v: number) => string; labelFormat?: (l: string | number) => string }) {
  if (!active || !payload?.length) return null;
  return <div className="tm-tooltip" role="tooltip">
    <div><b>{labelFormat && label != null ? labelFormat(label) : label}</b></div>
    {payload.map((p, i) => <div key={`${p.dataKey ?? p.name}-${i}`}><span style={{ color: p.color }}>{p.name}</span><span>{typeof p.value === "number" ? format(p.value) : "—"}</span></div>)}
  </div>;
}
const AXIS = { stroke: "var(--tm-chart-grid)", tick: { fill: "var(--tm-muted)", fontSize: 10.5 }, tickLine: false } as const;
const compact = (v: number) => new Intl.NumberFormat("en-IN", { notation: "compact", maximumFractionDigits: 1 }).format(v);

export const ChartFrame = ({ label, children, small = false }: { label: string; children: ReactNode; small?: boolean }) =>
  <div className={`tm-chart${small ? " sm" : ""}`} role="img" aria-label={label}><ResponsiveContainer width="100%" height="100%" minWidth={0}>{children as React.ReactElement}</ResponsiveContainer></div>;

/**
 * Intraday P&L from /api/portfolio/equity: realized net, unrealized, combined net, zero line, ENTRY/EXIT markers.
 * Real points only, straight segments (no smoothing, no area fill). A `partial` minute (an open position had no
 * executable mark) is drawn as a dashed gap segment instead of a solid line, and flagged in the tooltip.
 */
export function IntradayChart({ equity }: { equity: Equity }) {
  const pts = equity.points.map(p => ({ ts: parseTimestamp(p.t) ?? 0, t: p.t, partial: p.partial, realized: p.realized_net, unrealized: p.unrealized_gross, combined: p.combined_net, combinedSolid: p.partial ? null : p.combined_net, combinedPartial: p.partial ? p.combined_net : null }));
  // dashed bridge: a partial minute is connected to its neighbours by the dashed series
  const data = pts.map((p, i) => ({ ...p, combinedPartial: p.partial || pts[i - 1]?.partial || pts[i + 1]?.partial ? p.combined : null }));
  const markers = equity.markers.map(m => ({ ...m, ts: parseTimestamp(m.t) ?? 0 }));
  const at = (ts: number) => data.reduce<typeof data[number] | null>((best, p) => !best || Math.abs(p.ts - ts) < Math.abs(best.ts - ts) ? p : best, null);
  return <ChartFrame label="Intraday P&L: realized net, unrealized and combined net, with entry and exit markers">
    <ComposedChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
      <CartesianGrid stroke="var(--tm-chart-grid)" vertical={false}/>
      <XAxis dataKey="ts" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => istClock(new Date(v).toISOString()).slice(0, 5)} {...AXIS} minTickGap={36}/>
      <YAxis {...AXIS} width={54} tickFormatter={compact} domain={["auto", "auto"]}/>
      <ReferenceLine y={0} stroke="var(--tm-border-strong)" strokeWidth={1}/>
      <Tooltip content={<ChartTooltip labelFormat={l => `${istClock(new Date(Number(l)).toISOString())} IST`}/>}/>
      <Line name="Realized net" type="linear" dataKey="realized" stroke="var(--tm-chart-1)" strokeWidth={1.5} dot={false} connectNulls={false} isAnimationActive={false}/>
      <Line name="Unrealized" type="linear" dataKey="unrealized" stroke="var(--tm-chart-2)" strokeWidth={1.5} dot={false} connectNulls={false} isAnimationActive={false}/>
      <Line name="Combined net" type="linear" dataKey="combinedSolid" stroke="var(--tm-chart-3)" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false}/>
      <Line name="Combined net (partial)" type="linear" dataKey="combinedPartial" stroke="var(--tm-chart-3)" strokeWidth={1.5} strokeDasharray="4 3" dot={false} connectNulls={false} isAnimationActive={false}/>
      {markers.map((m, i) => { const p = at(m.ts); return p && p.combined != null ? <ReferenceDot key={`${m.t}-${i}`} x={p.ts} y={p.combined} r={4} fill={m.type === "ENTRY" ? "var(--tm-info)" : "var(--tm-warn)"} stroke="var(--tm-surface)" ifOverflow="hidden" aria-label={`${m.type} ${m.direction ?? ""}`}/> : null; })}
    </ComposedChart>
  </ChartFrame>;
}

export function BarsByBucket({ data, label, format = (v: number) => signedRupees(v, 0) }: { data: Array<{ name: string; value: number | null }>; label: string; format?: (v: number) => string }) {
  return <ChartFrame label={label} small>
    <BarChart data={data} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
      <CartesianGrid stroke="var(--tm-chart-grid)" vertical={false}/>
      <XAxis dataKey="name" {...AXIS} interval="preserveStartEnd" minTickGap={14}/>
      <YAxis {...AXIS} width={50} tickFormatter={compact}/>
      <ReferenceLine y={0} stroke="var(--tm-border-strong)"/>
      <Tooltip content={<ChartTooltip format={format}/>} cursor={{ fill: "var(--tm-hover)" }}/>
      <Bar name="Net P&L" dataKey="value" isAnimationActive={false}>{data.map((d, i) => <Cell key={`${d.name}-${i}`} fill={(d.value ?? 0) >= 0 ? "var(--tm-pos)" : "var(--tm-neg)"}/>)}</Bar>
    </BarChart>
  </ChartFrame>;
}
export function LineSeries({ data, series, label, xKey = "x", format }: { data: Array<Record<string, number | string | null>>; series: Array<{ key: string; name: string; color: string }>; label: string; xKey?: string; format?: (v: number) => string }) {
  return <ChartFrame label={label} small>
    <ComposedChart data={data} margin={{ top: 6, right: 10, left: 0, bottom: 0 }}>
      <CartesianGrid stroke="var(--tm-chart-grid)" vertical={false}/>
      <XAxis dataKey={xKey} {...AXIS} interval="preserveStartEnd" minTickGap={24}/>
      <YAxis {...AXIS} width={54} tickFormatter={compact}/>
      <ReferenceLine y={0} stroke="var(--tm-border-strong)"/>
      <Tooltip content={<ChartTooltip format={format}/>}/>
      {series.map(s => <Line key={s.key} name={s.name} type="linear" dataKey={s.key} stroke={s.color} strokeWidth={1.75} dot={data.length < 40 ? { r: 2 } : false} connectNulls={false} isAnimationActive={false}/>)}
    </ComposedChart>
  </ChartFrame>;
}

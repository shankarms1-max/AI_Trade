"use client";
import Link from "next/link";
import { notFound } from "next/navigation";
import { useUrlState } from "@/components/filters-context";
import { dimensionViews, latestBlocker, listOf, numberOf, str, boolOf } from "@/components/pages/dimensions";
import { Orders } from "@/components/pages/orders";
import { PerformanceReport } from "@/components/pages/performance-report";
import { Positions } from "@/components/pages/positions";
import { StrategyTable } from "@/components/pages/overview";
import { Tradebook } from "@/components/pages/tradebook";
import { EmptyState, LiveGate, ResourceView } from "@/components/terminal/states";
import { KV, MetricTile, PageTitle, Panel, PnL, StatusBadge, StrategyChip, TabPanel, Tabs, Tiles, Unavailable, Val } from "@/components/terminal/primitives";
import { LIVE_INTERVAL, useResource } from "@/hooks/use-resource";
import { parse, urls } from "@/lib/portfolio-api";
import { istDateTime, int, num, rupees, title } from "@/lib/terminal-format";
import type { Dict, StrategyDetail as Detail } from "@/types/portfolio";

export function Strategies() {
  const { filters } = useUrlState();
  const live = filters.mode === "LIVE";
  const list = useResource(live ? null : urls.strategies(), parse.strategies);
  const cards = (list.data?.items ?? []).filter(c => (!filters.engine || c.engine_id === filters.engine) && (!filters.instrument || c.instrument === filters.instrument) && (!filters.version || c.version === filters.version));
  return <div>
    <PageTitle title="Strategies" note="Each status dimension is separate: execution enablement, session, feed, warm-up, signal and position. Today's P&L is the API's."/>
    <LiveGate live={live}>
      <Panel title="Strategies" flush>
        <ResourceView resource={list} label="Strategies" isEmpty={() => cards.length === 0} empty={<EmptyState>No strategy matches the filters</EmptyState>}>{() => <StrategyTable cards={cards}/>}</ResourceView>
      </Panel>
    </LiveGate>
  </div>;
}

/* ------------------------------------------------------------------ detail */
const TABS = ["overview", "positions", "orders", "trades", "signals", "performance", "configuration", "diagnostics"] as const;
type DetailTab = typeof TABS[number];

function Block({ heading, children }: { heading: string; children: React.ReactNode }) { return <Panel title={heading}>{children}</Panel>; }

function OverviewTab({ d }: { d: Detail }) {
  const dims = d.dimensions;
  const views = dimensionViews(d);
  const view = (k: string) => views.find(v => v.dimension === k)!;
  const risk = useResource(urls.risk({ engine: d.engine_id, mode: "PAPER" }), parse.risk);
  const budget = risk.data?.engines.find(e => e.engine_id === d.engine_id);
  const ready = dims.readiness;
  const bars = numberOf(ready, "bars_available"), need = numberOf(ready, "bars_required");
  const blocking = listOf(ready, "blocking");
  const feed = dims.feed;
  const blocker = latestBlocker(d);
  return <div>
    <Tiles>
      <MetricTile primary label="Today net P&L" value={<PnL value={d.today.total_net}/>} warnNote={d.today.partial} note={d.today.partial ? "PARTIAL · an open position has no executable mark" : `${int(d.today.trades)} trades`}/>
      <MetricTile label="Realized net" value={<PnL value={d.today.realized_net}/>}/>
      <MetricTile label="Unrealized" value={<PnL value={d.today.unrealized_gross} reason={d.today.partial ? "open mark excluded" : null}/>}/>
      <MetricTile label="Open positions" value={int(d.today.open_positions)} warnNote={d.unresolved > 0} note={d.unresolved > 0 ? `${d.unresolved} UNRESOLVED` : null}/>
      <MetricTile label="Remaining risk budget" value={budget?.remaining_daily_loss_budget == null ? <Unavailable/> : rupees(budget.remaining_daily_loss_budget, 0)} note={budget?.daily_loss_limit != null ? `limit ${rupees(budget.daily_loss_limit, 0)} · ${budget.limits_source ?? ""}` : "no deployed limit"}/>
      <MetricTile label="Latest blocker" value={<Val>{blocker ? <span style={{ fontSize: 12 }}>{title(blocker)}</span> : null}</Val>} note="why the engine is not entering"/>
    </Tiles>
    <div className="tm-grid c3">
      <Block heading="1 · Enablement"><KV items={[
        ["Paper execution", <StatusBadge key="a" dimension="enablement" value={view("enablement").value} label="Execution"/>],
        ["Live execution", <span key="b" className="tm-muted">{str(dims.enablement, "live_execution") ?? "—"}</span>],
        ["Operational state", <span key="c" title={d.state_reason ?? undefined}><StatusBadge dimension="state" value={d.operational_state} label="State"/>{d.state_reason ? <small className="tm-muted"> {title(d.state_reason)}</small> : null}</span>],
      ]}/></Block>
      <Block heading="2 · Market session"><KV items={[
        ["Session", <StatusBadge key="a" dimension="session" value={view("session").value} label="Session"/>], ["Window", str(dims.session, "window") ?? "—"],
        ["Last entry", istDateTime(str(dims.session, "last_entry_time"))], ["Forced exit", istDateTime(str(dims.session, "forced_exit_time"))], ["Next open", istDateTime(str(dims.session, "next_open"))],
      ]}/></Block>
      <Block heading="3 · Feed freshness">{feed == null ? <Unavailable reason="no feed record"/> : <KV items={[
        ["Feed", <StatusBadge key="a" dimension="feed" value={view("feed").value} label="Feed"/>],
        ["Last quote", istDateTime(str(feed, "last_quote_at") ?? str(feed, "last_observation"))], ["Quote age", numberOf(feed, "age_seconds") == null ? "—" : `${Math.round(numberOf(feed, "age_seconds")!)} s`],
        ["Bid / ask / LTP", `${numberOf(feed, "bid") == null ? "—" : num(numberOf(feed, "bid"))} / ${numberOf(feed, "ask") == null ? "—" : num(numberOf(feed, "ask"))} / ${numberOf(feed, "ltp") == null ? "—" : num(numberOf(feed, "ltp"))}`],
        ["Spread (ticks)", numberOf(feed, "spread_ticks") == null ? "—" : num(numberOf(feed, "spread_ticks"), 1)],
      ]}/>}</Block>
      <Block heading="4 · Feature readiness / warm-up">{ready == null ? <div className="tm-muted">No warm-up feature set is recorded for this engine.</div> : <>
        <StatusBadge dimension="readiness" value={view("readiness").value} label="Warm-up"/>
        {bars != null && need != null && <div style={{ margin: "8px 0" }}><div className="tm-bar" data-tone={bars >= need ? "pos" : "warn"} role="progressbar" aria-label="Bars available versus required" aria-valuemin={0} aria-valuemax={need} aria-valuenow={Math.min(bars, need)}><i style={{ width: `${Math.min(100, (bars / need) * 100)}%` }}/></div>
          <small className="tm-muted">{bars} / {need} bars ({numberOf(ready, "timeframe_minutes") ?? "—"} min timeframe)</small></div>}
        {blocking.length ? <div>Blocking features: {blocking.map(b => <StatusBadge key={b} value={b} tone="warn"/>)}</div> : <div className="tm-muted">No blocking features.</div>}
      </>}</Block>
      <Block heading="5 · Signal state"><KV items={[
        ["Signal", <StatusBadge key="a" dimension="signal" value={view("signal").value} label="Signal"/>], ["Direction", str(dims.signal, "direction") ?? "—"],
        ["Score", numberOf(dims.signal, "score") == null ? "—" : num(numberOf(dims.signal, "score"), 1)], ["Market state", title(str(dims.signal, "market_state"))], ["Cycle", `${str(dims.signal, "cycle_id")?.slice(0, 12) ?? "—"} · ${title(str(dims.signal, "cycle_state"))}`],
      ]}/></Block>
      <Block heading="6 · Position state"><KV items={[
        ["Position", <StatusBadge key="a" dimension="position" value={view("position").value} label="Position"/>], ["Open", int(d.today.open_positions)], ["Unresolved", int(d.unresolved)],
        ["Last update", istDateTime(d.last_update)],
      ]}/>{(d.retired) && <p className="tm-caption"><StatusBadge value="RETIRED" tone="na"/> This strategy version is retired; history only.</p>}</Block>
    </div>
  </div>;
}

function ComponentBars({ components, max }: { components: Record<string, number>; max: Record<string, number> | null }) {
  return <div>{Object.entries(components).map(([k, v]) => {
    const m = max?.[k];
    return <div key={k} style={{ display: "grid", gridTemplateColumns: "110px 1fr 90px", gap: 8, alignItems: "center", padding: "2px 0" }}>
      <span>{title(k)}</span>
      {m != null && m > 0 ? <span className="tm-bar" role="progressbar" aria-label={`${k} ${v} of ${m}`} aria-valuemin={0} aria-valuemax={m} aria-valuenow={v}><i style={{ width: `${Math.max(0, Math.min(100, (v / m) * 100))}%` }}/></span> : <span className="tm-muted">no maximum recorded</span>}
      <span className="tm-num">{num(v, 1)}{m != null ? ` / ${num(m, 0)}` : ""}</span>
    </div>;
  })}</div>;
}
const numRecord = (d: Dict | null | undefined, key: string): Record<string, number> | null => {
  const v = d?.[key];
  if (v == null || typeof v !== "object" || Array.isArray(v)) return null;
  const out: Record<string, number> = {};
  for (const [k, x] of Object.entries(v as Dict)) if (typeof x === "number" && Number.isFinite(x)) out[k] = x;
  return Object.keys(out).length ? out : null;
};

function SignalsTab({ d }: { d: Detail }) {
  const s = d.dimensions.signal;
  const cfg = d.config;
  // floor comes from the EFFECTIVE config only: NIFTY policy `signal_min_score`, MCX `entry_score_floor`
  const floor = numberOf(cfg.effective, "signal_min_score") ?? numberOf(cfg.parameters, "entry_score_floor");
  const floorName = numberOf(cfg.effective, "signal_min_score") != null ? "signal_min_score (stored policy)" : "entry_score_floor (deployed config)";
  const score = numberOf(s, "score");
  const components = numRecord(s, "components"), max = numRecord(s, "component_max");
  const vetoes = [...listOf(s, "vetoes"), ...listOf(s, "blockers")];
  const unavailable = Object.entries((s?.unavailable_inputs ?? {}) as Dict).filter(([, v]) => Array.isArray(v) && v.length > 0);
  if (!s) return <EmptyState hint="This engine has not recorded a signal evaluation.">No signal recorded</EmptyState>;
  return <div>
    <Panel title="Score vs floor">
      <Tiles>
        <MetricTile primary label="Latest score" value={score == null ? <Val>{null}</Val> : num(score, 1)} note={str(s, "score_band") ? title(str(s, "score_band")) : null}/>
        <MetricTile label="Entry floor" value={floor == null ? <Unavailable reason="not in effective config"/> : num(floor, 0)} note={floor == null ? null : floorName}/>
        <MetricTile label="Status" value={<StatusBadge dimension="signal" value={str(s, "status") ?? "UNKNOWN"}/>} note={`${str(s, "direction") ?? "no direction"} · ${title(str(s, "market_state"))}`}/>
        <MetricTile label="Evaluated at" value={istDateTime(str(s, "at") ?? d.last_update)} note={boolOf(s, "warm") === false ? "features not warm" : null}/>
      </Tiles>
      {score != null && floor != null && <div className="tm-bar" role="img" aria-label={`Score ${score} against floor ${floor}`} data-tone={score >= floor ? "pos" : "warn"}><i style={{ width: `${Math.min(100, score)}%` }}/></div>}
    </Panel>
    <div className="tm-grid c2">
      <Panel title="Score components">{components ? <ComponentBars components={components} max={max}/> : <div className="tm-muted">Components not recorded.</div>}</Panel>
      <Panel title="Vetoes and blockers">{vetoes.length ? <ul className="tm-list">{vetoes.map(v => <li key={v}><StatusBadge value={v} tone="warn"/></li>)}</ul> : <div className="tm-muted">None recorded.</div>}
        <KV items={[["Primary blocker", title(str(s, "primary_blocker"))]]}/></Panel>
    </div>
    <div className="tm-grid c2">
      <Panel title="Opportunity cycle"><KV items={[["Cycle id", <span key="a" className="tm-mono">{str(s, "cycle_id") ?? "—"}</span>], ["Cycle state", title(str(s, "cycle_state"))], ["Re-arm reason", title(str(s, "rearm_reason"))]]}/></Panel>
      <Panel title="Unavailable inputs">{unavailable.length ? <ul className="tm-list">{unavailable.map(([k, v]) => <li key={k}><b>{title(k)}</b>: {(v as string[]).join(", ")}</li>)}</ul> : <div className="tm-muted">All inputs available.</div>}</Panel>
    </div>
  </div>;
}

function Param({ k, v }: { k: string; v: unknown }) {
  if (v == null) return <div><dt>{title(k)}</dt><dd className="tm-dash" title="not recorded">—</dd></div>;
  if (typeof v === "object") return <div style={{ gridColumn: "1 / -1" }}><dt>{title(k)}</dt><dd><details className="tm-details"><summary>{Array.isArray(v) ? `${v.length} item(s)` : `${Object.keys(v as object).length} field(s)`}</summary><pre className="tm-pre">{JSON.stringify(v, null, 2)}</pre></details></dd></div>;
  return <div><dt>{title(k)}</dt><dd>{typeof v === "number" ? num(v, Number.isInteger(v) ? 0 : 2) : String(v)}</dd></div>;
}
function ParamList({ data }: { data: Dict | null }) {
  if (!data) return <div className="tm-muted">Not recorded.</div>;
  return <dl className="tm-kv">{Object.entries(data).map(([k, v]) => <Param key={k} k={k} v={v}/>)}</dl>;
}
function ConfigTab({ d }: { d: Detail }) {
  const c = d.config;
  if (!c.available) return <EmptyState hint={c.note ?? c.error ?? undefined}>Effective configuration unavailable</EmptyState>;
  return <div>
    <Panel title="Provenance of these parameters"><KV items={[
      ["Source", c.source ?? "—"], ["Policy hash", <span key="a" className="tm-mono">{c.policy_hash ?? c.config_id ?? "—"}</span>], ["Stored on trade", <span key="b" className="tm-mono">{c.from_trade ?? "—"}</span>],
      ["Decided at", istDateTime(c.decided_at)],
      ...(c.config_id ? [["Config id", <span key="c" className="tm-mono">{c.config_id}</span>], ["Persisted worker hash", <span key="d" className="tm-mono">{c.persisted_config_hash ?? "none persisted"}</span>],
        ["Matches running worker", <StatusBadge key="e" value={c.matches_running_worker ? "MATCH" : "NO MATCH"} tone={c.matches_running_worker ? "pos" : "warn"} title="Deployed config hash compared with the hash the running worker persisted"/>],
        ["Calibration", <StatusBadge key="f" value={c.calibration_status ?? "UNKNOWN"} tone="warn"/>]] as Array<[string, React.ReactNode]> : []),
    ]}/>{c.note && <p className="tm-caption">{c.note}</p>}</Panel>
    {c.effective && <Panel title="Effective parameters (what actually ran)"><ParamList data={c.effective}/></Panel>}
    {c.parameters && <Panel title={c.config_id ? "Deployed parameters" : "Policy parameters"}><ParamList data={c.parameters}/></Panel>}
    {c.exit_policy && <Panel title="Exit policy"><ParamList data={c.exit_policy}/></Panel>}
    {c.entry_policy && <Panel title="Entry policy"><ParamList data={c.entry_policy}/></Panel>}
    {c.opportunity_cycle_policy && <Panel title="Opportunity cycle policy"><ParamList data={c.opportunity_cycle_policy}/></Panel>}
  </div>;
}
function DiagnosticsTab({ d }: { d: Detail }) {
  const block = (name: string, value: unknown) => <details className="tm-details" key={name}><summary>{name}</summary><pre className="tm-pre">{JSON.stringify(value, null, 2)}</pre></details>;
  return <Panel title="Technical evidence (raw API payloads)">
    <p className="tm-caption" style={{ marginTop: 0 }}>Untouched dimension objects as returned by /api/portfolio/strategies/{d.engine_id}. For debugging; not interpreted.</p>
    {block("dimensions (raw)", d.dimensions_raw)}{block("config (raw)", d.config)}
    {block("state", { operational_state: d.operational_state, state_reason: d.state_reason, last_update: d.last_update, retired: d.retired })}
  </Panel>;
}

export function StrategyDetail({ engineId }: { engineId: string }) {
  const { get, set } = useUrlState();
  const detail = useResource(urls.strategy(engineId), parse.strategy, LIVE_INTERVAL, { keepPrevious: engineId });
  const raw = get("view");
  const tab: DetailTab = (TABS as readonly string[]).includes(raw ?? "") ? raw as DetailTab : "overview";
  if (!detail.data && detail.failure?.status === 404) notFound();
  const d = detail.data;
  return <div>
    <div className="tm-page-title"><h1>{d ? <StrategyChip label={d.label} family={d.family} version={d.version}/> : "Strategy"}</h1>
      {d && <><StatusBadge dimension="state" value={d.operational_state} label="State"/>{d.unresolved > 0 && <StatusBadge value={`${d.unresolved} UNRESOLVED`} tone="crit" dot/>}<StatusBadge value={d.mode} tone="info"/></>}
      <span className="tm-spacer"/><Link className="tm-link" href="/strategies">All strategies</Link></div>
    <ResourceView resource={detail} label="Strategy" skeletonRows={5}>{data => <>
      <Tabs label="Strategy sections" value={tab} onChange={t => set({ view: t === "overview" ? null : t, offset: null, tab: null })} tabs={TABS.map(t => ({ id: t, label: title(t).replace(/^./, c => c.toUpperCase()) }))}/>
      <TabPanel id={tab}>
        {tab === "overview" && <OverviewTab d={data}/>}
        {tab === "positions" && <Positions engine={engineId}/>}
        {tab === "orders" && <Orders engine={engineId}/>}
        {tab === "trades" && <Tradebook engine={engineId}/>}
        {tab === "signals" && <SignalsTab d={data}/>}
        {tab === "performance" && <PerformanceReport perf={data.performance} compare={false}/>}
        {tab === "configuration" && <ConfigTab d={data}/>}
        {tab === "diagnostics" && <DiagnosticsTab d={data}/>}
      </TabPanel>
    </>}</ResourceView>
  </div>;
}

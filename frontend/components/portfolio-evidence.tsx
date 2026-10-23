"use client";
import { useCallback } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import { usePolling } from "@/hooks/use-polling";
import { awareTimestamp, commodityTrade, niftyTrade, realizedPath, type PortfolioTrade } from "@/lib/portfolio";
import { dateIST, money, timeIST } from "@/lib/format";
import { Badge, Empty, Panel } from "@/components/ui";
import { Icon } from "@/components/icons";
import { DataTable, type DataColumn } from "@/components/data-table";
import type { Phase15Summary } from "@/types";
import type { PaperInstrumentStatus } from "@/types/commodity-paper";

const columns: DataColumn<PortfolioTrade>[] = [
  { key: "source", label: "Market / strategy", value: row => row.source, render: row => <><strong>{row.source}</strong><small>{row.strategy}</small></> },
  { key: "contract", label: "Position", value: row => row.contract, render: row => <>{row.contract ?? "Unavailable"}</> },
  { key: "state", label: "State", value: row => row.state, render: row => <Badge value={row.state}/> },
  { key: "timestamp", label: "Entry · IST", value: row => row.timestamp, render: row => row.timestamp ? <>{timeIST(row.timestamp)}<small>{dateIST(row.timestamp)}</small></> : "Unavailable" },
  { key: "unrealized", label: "Unrealized gross", value: row => row.unrealized, render: row => <span className={row.unrealized == null ? "muted" : row.unrealized < 0 ? "negative" : row.unrealized > 0 ? "positive" : ""}>{money(row.unrealized)}</span> },
  { key: "detail", label: "Details", value: row => row.id, render: row => <details className="phase15-research"><summary>View evidence</summary><p>ID: {row.id}</p><p>Reason: {row.reason ?? "Unavailable"}</p><p>No inferred prices, depth or net costs.</p></details> },
];
const searchText = (row: PortfolioTrade) => `${row.source} ${row.strategy} ${row.contract} ${row.state}`;
const rowKey = (row: PortfolioTrade) => row.id;

export function PortfolioEvidence({ nifty, commodities }: { nifty: Phase15Summary | null | undefined; commodities: PaperInstrumentStatus[] | null | undefined }) {
  const loader = useCallback(async () => {
    const values = await Promise.allSettled([api.phase15Positions(), api.phase15Journal({ date: "all", limit: 100 }),
      api.commodityPaperPage("CRUDEOILM", "positions"), api.commodityPaperPage("NATGASMINI", "positions")]);
    const [positions, journal, crude, gas] = values;
    const positionsValid = positions.status === "fulfilled" && positions.value && Array.isArray(positions.value.items);
    const journalValid = journal.status === "fulfilled" && journal.value && Array.isArray(journal.value.items);
    const open = positionsValid ? positions.value.items.map(niftyTrade) : [];
    const history = journalValid ? journal.value.items.map(niftyTrade) : [];
    for (const [source, page] of [["CRUDEOILM", crude], ["NATGASMINI", gas]] as const) {
      if (page.status === "fulfilled" && page.value && Array.isArray(page.value.items)) {
        const rows = (page.value.items ?? []).map((row, index) => commodityTrade(source, row, index));
        open.push(...rows.filter(row => row.state === "OPEN" || row.state === "PENDING_PAPER_ENTRY" || row.state === "UNRESOLVED_EXPOSURE"));
        history.push(...rows);
      }
    }
    return { open, history, available: Boolean(positionsValid && journalValid && [crude,gas].every(v => v.status === "fulfilled" && v.value && Array.isArray(v.value.items))),
      partial: values.some(v => v.status === "fulfilled" && v.value?.has_more) };
  }, []);
  const { data, error } = usePolling(loader, 20000);
  const path = data?.available && !error ? realizedPath(data.history) : null;
  const observations = [
    ...(nifty?.strategies ?? []).map(row => ({ id: row.strategy_id, label: row.display_name, at: awareTimestamp(row.latest_timestamp), status: row.latest_status, reason: row.latest_blocker })),
    ...(commodities ?? []).map(row => ({ id: row.instrument_id, label: row.instrument_id, at: awareTimestamp(row.latest?.observed_at), status: row.latest?.decision?.status, reason: [...(row.latest?.risk_blocks ?? []), ...(row.latest?.decision?.blockers ?? [])].filter((v,i,a) => a.indexOf(v) === i).join(" · ") })),
  ].filter(row => row.at).sort((a,b) => Date.parse(b.at!) - Date.parse(a.at!) || a.id.localeCompare(b.id)).slice(0,6);
  return <>
    <div className="command-grid">
      <Panel title="REALIZED P&L PATH" action={<Badge value={data?.available ? "GROSS_ONLY" : "UNAVAILABLE"}/>}>
        <div className="source-legend"><span><i/> Priced, loaded paper exits</span><span>No assumed starting capital</span></div>
        {path && path.points.length > 0 ? <div className="chart" aria-label="Cumulative realized gross of loaded priced closed trades"><ResponsiveContainer width="100%" height="100%"><AreaChart data={path.points} margin={{ top: 25, right: 6, bottom: 0, left: 4 }}><defs><linearGradient id="portfolio-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--accent)" stopOpacity={.2}/><stop offset="100%" stopColor="var(--accent)" stopOpacity={0}/></linearGradient></defs><CartesianGrid vertical={false} stroke="var(--border)"/><XAxis dataKey="timestamp" tickFormatter={v => dateIST(v).slice(0,6)} minTickGap={35}/><YAxis tickFormatter={v => Intl.NumberFormat("en-IN", { notation: "compact" }).format(v)} width={58}/><Tooltip labelFormatter={v => `${dateIST(String(v))} · ${timeIST(String(v))}`} formatter={v => [money(typeof v === "number" ? v : null), "Priced subset · cumulative gross"]} contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }}/><Area dataKey="gross" type="linear" stroke="var(--accent)" strokeWidth={2.5} fill="url(#portfolio-fill)" connectNulls={false} isAnimationActive={false}/></AreaChart></ResponsiveContainer></div> : <div className="chart-empty"><Icon name="chart" size={29}/><strong>{!data ? "Loading stored trade evidence" : !data.available || error ? "History unavailable" : path?.status === "INCOMPLETE" ? "Complete exit evidence required" : "Your research curve starts with a closed trade"}</strong><span>Only stored, priced exits are plotted. No synthetic equity curve or assumed capital.</span></div>}
        {Boolean(path?.excluded) && <p className="execution-note">{path!.excluded} unpriced closed records excluded. This is a priced subset, not complete strategy or portfolio performance.</p>}
        <div className="chart-caption"><span>{data?.partial ? "Partial history · additional pages available in journals" : "Loaded priced exits · cumulative realized gross"}</span><span>Not account equity or a profitability claim</span></div>
      </Panel>
      <Panel title="RECENT SIGNAL ACTIVITY" action={<Icon name="pulse" size={16}/>}>
        {observations.length ? <ul className="activity-list">{observations.map(row => <li key={row.id}><span className="activity-dot"/><div className="activity-body"><strong>{row.label}</strong><p>{row.status?.replaceAll("_", " ") ?? "Status unavailable"}</p><small>{row.reason?.replaceAll("_", " ") || "No blocker reported"}</small><p>{dateIST(row.at)} · {timeIST(row.at)}</p></div></li>)}</ul> : <Empty>No captured signal activity</Empty>}
        <p className="metric-detail">Stored observations, not proof of worker liveness.</p>
      </Panel>
    </div>
    <div className="section-title"><div><h2>Open positions & exposure</h2><p>Exact paper records across NIFTY, Crude Oil and Natural Gas</p></div><Badge value="PAPER"/></div>
    <Panel title="PORTFOLIO POSITIONS"><DataTable label="Unified paper positions" rows={error ? (data?.open ?? []).map(row => ({...row, unrealized: null})) : data?.open ?? []} columns={columns} rowKey={rowKey} searchText={searchText} empty={!data ? "Loading positions" : !data.available ? "Some position sources are unavailable" : "No open paper positions"} note={error ? "refresh unavailable · marks withheld" : data?.partial ? "partial pages" : "stored paper evidence"}/>
      {!data?.available && data && <p className="warning">Partial availability. Missing sources are not treated as flat.</p>}
    </Panel>
  </>;
}
